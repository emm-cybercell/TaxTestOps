"""测试用例生成器。

核心原则：**模型提议，代码校验，人决策。**

生成流程
--------
::

    OpenAPI 规范 ──► 规则推导（确定性基线）──┐
                                            ├──► 候选用例 ──► 校验器 ──► 可用用例
    需求文本   ──► 模型提议（可选，联网）  ──┘                  │
                                                              └──► 拒绝记录（附原因）

校验器检查什么
--------------
1. **结构合法**：字段、类型、取值范围符合用例定义模型；
2. **路径存在**：``path`` 必须出现在 OpenAPI 文档里，方法必须已声明；
3. **期望状态码合理**：必须是该操作声明过的响应码之一（或通用错误码）；
4. **断言可求值**：操作符在白名单内，路径语法合法。

通过校验但含推导假设的候选会被标记 ``needs_review=True``，不自动启用——
「结构合法」不等于「语义正确」，后者需要人看一眼。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from tax_service.ai.provider import LlmProvider, ProviderError, RuleBasedProvider
from tax_service.ai.schemas import Assertion, CaseGenerationReport, GeneratedCase

logger = logging.getLogger(__name__)

VALID_OPERATORS = frozenset(
    {"eq", "ne", "gt", "ge", "lt", "le", "contains", "in", "startswith"}
)

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

GENERIC_ERROR_STATUS = frozenset({400, 401, 403, 404, 409, 422, 429, 500, 503})

MAX_RULE_CASES_PER_OPERATION = 10

SYSTEM_PROMPT = (
    "你是一名资深测试开发工程师。请针对给定的接口生成测试用例，"
    "只输出 JSON，不要输出解释文字。"
)

REVIEW_CONFIDENCE_THRESHOLD = 0.75
"""低于该置信度的候选标记为待评审。"""


class CaseValidationError(ValueError):
    """候选用例未通过校验。"""


def _schema_ref_to_name(ref: str) -> str:
    """从 ``#/components/schemas/Xxx`` 中取出 ``Xxx``。"""
    return ref.rsplit("/", 1)[-1]


def _set_nested(field_path: str, value: Any) -> dict[str, Any]:
    """把点号路径的字段写成嵌套字典。

    ``("person.children_num", 2)`` → ``{"person": {"children_num": 2}}``

    生成的 payload 需要按契约的嵌套结构组织，而约束字段路径（来自
    :meth:`TestCaseGenerator._iter_constraint_fields`）是扁平的点号形式。
    """
    parts = field_path.split(".")
    root: dict[str, Any] = {}
    node = root
    for part in parts[:-1]:
        node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    return root


def resolve_local_ref(document: dict[str, Any], node: Any) -> Any:
    """展开一层 ``$ref``（仅支持文档内部引用）。"""
    if not isinstance(node, dict) or "$ref" not in node:
        return node

    pointer = str(node["$ref"])
    if not pointer.startswith("#/"):
        return node

    target: Any = document
    for part in pointer[2:].split("/"):
        if not isinstance(target, dict) or part not in target:
            return node
        target = target.get(part)
    return target


class TestCaseGenerator:
    """基于 OpenAPI 规范或需求文本生成测试用例。"""

    def __init__(
        self,
        provider: LlmProvider | None = None,
        document: dict[str, Any] | None = None,
        *,
        max_cases_per_operation: int = MAX_RULE_CASES_PER_OPERATION,
    ) -> None:
        self.document = document or {}
        self.provider = provider or RuleBasedProvider(self.document)
        self.max_cases_per_operation = max_cases_per_operation

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #

    @property
    def source(self) -> str:
        """当前生效的生成来源。"""
        return "rule" if isinstance(self.provider, RuleBasedProvider) else "llm"

    def from_openapi(self, path: str, method: str) -> CaseGenerationReport:
        """为某个接口生成用例。

        :param path: OpenAPI 路径模板，如 ``/api/v1/tax/calculate``。
        :param method: HTTP 方法（大小写不敏感）。
        """
        operation = self._lookup_operation(path, method)
        target = f"{method.upper()} {path}"

        if self.source == "llm":
            candidates, rejected = self._generate_with_model(target, operation)
        else:
            candidates, rejected = self._generate_with_rules(target, operation), []

        accepted: list[GeneratedCase] = []
        for candidate in candidates:
            try:
                accepted.append(self._validate(candidate, path, method))
            except CaseValidationError as exc:
                name = str(candidate.get("name", "<未命名>"))
                rejected.append({"name": name, "reason": str(exc)})

        return CaseGenerationReport(
            source=self.source,  # type: ignore[arg-type]
            target=target,
            generated=accepted,
            rejected=rejected,
        )

    def from_requirement(
        self, requirement: str, *, path: str, method: str = "POST"
    ) -> CaseGenerationReport:
        """从需求文本生成用例。

        只有接入模型时才有意义——规则推导无法理解自然语言需求。未接入模型时返回
        空报告并说明原因，而不是假装生成了东西。
        """
        target = f"需求文本 → {method.upper()} {path}"

        if self.source == "rule":
            return CaseGenerationReport(
                source="rule",
                target=target,
                generated=[],
                rejected=[
                    {
                        "name": "<需求文本生成>",
                        "reason": (
                            "规则提供者无法解析自然语言需求；请配置 ai_api_key 以启用模型生成"
                        ),
                    }
                ],
            )

        operation = self._lookup_operation(path, method)
        candidates, rejected = self._generate_with_model(target, operation, requirement=requirement)

        accepted: list[GeneratedCase] = []
        for candidate in candidates:
            try:
                accepted.append(self._validate(candidate, path, method))
            except CaseValidationError as exc:
                name = str(candidate.get("name", "<未命名>"))
                rejected.append({"name": name, "reason": str(exc)})

        return CaseGenerationReport(
            source="llm", target=target, generated=accepted, rejected=rejected
        )

    # ------------------------------------------------------------------ #
    # 规则推导（离线基线）
    # ------------------------------------------------------------------ #

    def _lookup_operation(self, path: str, method: str) -> dict[str, Any]:
        """从文档中取出操作定义。"""
        paths = self.document.get("paths", {})
        if path not in paths:
            raise CaseValidationError(f"OpenAPI 文档中不存在路径：{path}")

        method_key = method.lower()
        if method_key not in paths[path]:
            raise CaseValidationError(f"{path} 未声明方法 {method.upper()}")

        return paths[path][method_key]

    def _request_schema(self, operation: dict[str, Any]) -> dict[str, Any]:
        """取出请求体 schema（已展开一层 $ref）。"""
        body = operation.get("requestBody")
        if not body:
            return {}

        content = body.get("content", {})
        media = content.get("application/json") or next(iter(content.values()), {})
        return resolve_local_ref(self.document, media.get("schema", {}))

    def _declared_statuses(self, operation: dict[str, Any]) -> set[int]:
        statuses = set()
        for code in operation.get("responses", {}):
            if str(code).isdigit():
                statuses.add(int(code))
        return statuses

    def _required_properties(self, schema: dict[str, Any]) -> dict[str, Any]:
        properties = schema.get("properties", {})
        return {
            name: properties[name]
            for name in schema.get("required", [])
            if name in properties
        }

    def _iter_constraint_fields(
        self, schema: dict[str, Any]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """收集「带约束的叶子字段」，供边界值与枚举用例推导。

        返回 ``(字段路径, 所在容器路径, 字段 schema)`` 三元组。字段路径用点号表达
        嵌套（如 ``person.is_keep_stu``），与断言的取值路径保持一致。

        会递归展开一层 ``$ref``：真实契约里嵌套对象（如 ``CalculateRequest.person``）
        都以引用形式出现，不展开就推导不出任何枚举/边界用例——这正是本方法存在的
        意义，也是当初漏掉它们的根因。

        只展开到第二层：更深的嵌套会让「模型/规则生成的 payload」难以映射回
        请求体结构，收益骤减。
        """
        collected: list[tuple[str, str, dict[str, Any]]] = []

        def walk(prefix: str, container: str, node: dict[str, Any], depth: int) -> None:
            if depth > 2:
                return

            resolved = resolve_local_ref(self.document, node)
            properties = resolved.get("properties")
            if not isinstance(properties, dict):
                return

            for name, spec in properties.items():
                path = f"{prefix}.{name}" if prefix else name
                resolved_spec = resolve_local_ref(self.document, spec)
                if resolved_spec.get("type") == "object" or "$ref" in spec:
                    walk(path, container, resolved_spec, depth + 1)
                elif (
                    resolved_spec.get("enum")
                    or resolved_spec.get("minimum") is not None
                    or resolved_spec.get("maximum") is not None
                ):
                    collected.append((path, container, resolved_spec))

        walk("", schema.get("title", ""), schema, 0)
        return collected

    def _generate_with_rules(
        self, target: str, operation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """基于规范做确定性推导。

        覆盖六类经典场景：正常路径、必填缺失、边界值、非法枚举、未知字段、未认证。
        这几类恰好是「接口契约测试」的最小充分集——不依赖模型也能跑出真实价值。
        """
        method, path = target.split(" ", 1)
        schema = self._request_schema(operation)
        required = self._required_properties(schema)
        statuses = self._declared_statuses(operation)
        requires_auth = bool(operation.get("security"))
        success = 200 if 200 in statuses else (min(statuses) if statuses else 200)

        cases: list[dict[str, Any]] = []

        # 1. 正常路径
        happy_payload = {name: self._sample_value(spec) for name, spec in required.items()}
        cases.append(
            {
                "name": f"{target} 正常请求",
                "method": method,
                "path": path,
                "payload": happy_payload or None,
                "expected_status": success,
                "assertions": [],
                "tags": ["generated", "happy-path"],
                "requires_auth": requires_auth,
                "rationale": "按必填字段构造最小合法请求，验证主链路可用",
                "confidence": 0.9,
            }
        )

        # 2. 必填字段缺失 → 期望参数校验失败
        for name in list(required)[:3]:
            partial = {key: value for key, value in happy_payload.items() if key != name}
            cases.append(
                {
                    "name": f"{target} 缺少必填字段 {name}",
                    "method": method,
                    "path": path,
                    "payload": partial,
                    "expected_status": 422,
                    "assertions": [],
                    "tags": ["generated", "negative"],
                    "requires_auth": requires_auth,
                    "rationale": f"缺少必填字段 {name} 时应被参数校验拒绝",
                    "confidence": 0.85,
                }
            )

        # 3. 数值字段边界（含嵌套对象内的字段，如 person.children_num）
        constrained = self._iter_constraint_fields(schema)
        numeric = [
            (field_path, spec)
            for field_path, _, spec in constrained
            if spec.get("type") in {"integer", "number"}
        ][:3]

        for field_path, spec in numeric:
            minimum = spec.get("minimum")
            maximum = spec.get("maximum")
            if minimum is None and maximum is None:
                continue

            boundary = minimum if minimum is not None else maximum
            cases.append(
                {
                    "name": f"{target} 字段 {field_path} 取边界值 {boundary}",
                    "method": method,
                    "path": path,
                    "payload": {**happy_payload, **_set_nested(field_path, boundary)},
                    "expected_status": success,
                    "assertions": [],
                    "tags": ["generated", "boundary"],
                    "requires_auth": requires_auth,
                    "rationale": (
                        f"字段 {field_path} 的合法边界为 {minimum}~{maximum}，边界值应被接受"
                    ),
                    "confidence": 0.8,
                }
            )

            if minimum is not None:
                # minimum = 0 时，-1 同样违反下界约束，是有效的负向用例
                cases.append(
                    {
                        "name": f"{target} 字段 {field_path} 低于下界",
                        "method": method,
                        "path": path,
                        "payload": {
                            **happy_payload,
                            **_set_nested(field_path, minimum - 1),
                        },
                        "expected_status": 422,
                        "assertions": [],
                        "tags": ["generated", "negative", "boundary"],
                        "requires_auth": requires_auth,
                        "rationale": f"字段 {field_path} 低于下界 {minimum} 时应被拒绝",
                        "confidence": 0.85,
                    }
                )

        # 4. 枚举字段取非法值（含嵌套对象内的字段，如 person.is_keep_stu）
        enum_fields = [
            (field_path, spec)
            for field_path, _, spec in constrained
            if spec.get("enum")
        ][:3]

        for field_path, spec in enum_fields:
            enum_values = spec["enum"]
            cases.append(
                {
                    "name": f"{target} 字段 {field_path} 取非法枚举值",
                    "method": method,
                    "path": path,
                    "payload": {
                        **happy_payload,
                        **_set_nested(field_path, "__invalid__"),
                    },
                    "expected_status": 422,
                    "assertions": [],
                    "tags": ["generated", "negative"],
                    "requires_auth": requires_auth,
                    "rationale": (
                        f"字段 {field_path} 仅接受 {enum_values}，非法值应被拒绝"
                    ),
                    "confidence": 0.9,
                }
            )

        # 5. 未知字段
        if schema.get("additionalProperties") is False:
            cases.append(
                {
                    "name": f"{target} 携带未知字段",
                    "method": method,
                    "path": path,
                    "payload": {**happy_payload, "__unexpected__": 1},
                    "expected_status": 422,
                    "assertions": [],
                    "tags": ["generated", "negative"],
                    "requires_auth": requires_auth,
                    "rationale": "契约声明禁止未知字段，多传字段应被拒绝而不是静默忽略",
                    "confidence": 0.85,
                }
            )

        # 6. 未认证访问
        if requires_auth:
            cases.append(
                {
                    "name": f"{target} 未认证访问被拒",
                    "method": method,
                    "path": path,
                    "payload": happy_payload or None,
                    "expected_status": 401,
                    "assertions": [],
                    "tags": ["generated", "security"],
                    "requires_auth": False,
                    "rationale": "受保护接口在缺少令牌时必须返回 401",
                    "confidence": 0.9,
                }
            )

        return cases[: self.max_cases_per_operation]

    @staticmethod
    def _sample_value(spec: dict[str, Any]) -> Any:
        """按字段类型构造一个合法的示例值。"""
        if "example" in spec:
            return spec["example"]
        if "enum" in spec:
            return spec["enum"][0]
        if "default" in spec:
            return spec["default"]

        match spec.get("type"):
            case "integer" | "number":
                minimum = spec.get("minimum", 0)
                maximum = spec.get("maximum")
                if maximum is None:
                    return minimum + 1 if minimum else 1
                return minimum if minimum is not None else maximum
            case "boolean":
                return False
            case "array":
                return []
            case "object":
                return {}
            case _:
                return "string"

    # ------------------------------------------------------------------ #
    # 模型提议
    # ------------------------------------------------------------------ #

    def _generate_with_model(
        self,
        target: str,
        operation: dict[str, Any],
        *,
        requirement: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """请模型生成候选用例。

        模型输出必须是 JSON 数组；解析失败时返回拒绝记录而不是抛异常——
        生成失败不应该让整个流程崩掉，它只是一次「没产出」。
        """
        prompt = self._build_prompt(target, operation, requirement)
        try:
            raw = self.provider.complete(prompt, system=SYSTEM_PROMPT)
        except ProviderError as exc:
            logger.warning("模型调用失败：%s", exc)
            return [], [{"name": target, "reason": f"模型调用失败：{exc}"}]

        return self._parse_model_output(raw)

    @staticmethod
    def _build_prompt(
        target: str, operation: dict[str, Any], requirement: str | None
    ) -> str:
        sections = [
            f"目标接口：{target}",
            "接口定义（OpenAPI 片段）：",
            json.dumps(operation, ensure_ascii=False, indent=2)[:4000],
        ]
        if requirement:
            sections.append("需求描述：")
            sections.append(requirement[:2000])

        sections.append(
            "请输出 JSON 数组，每个元素形如："
            '{"name": "...", "method": "POST", "path": "...", "payload": {...}, '
            '"expected_status": 200, "assertions": [{"path": "字段", "op": "eq", "value": 1}], '
            '"tags": ["..."], "requires_auth": true, "rationale": "为什么需要这条用例", '
            '"confidence": 0.0~1.0}。'
            "断言只允许使用这些操作符：eq, ne, gt, ge, lt, le, contains, in, startswith。"
            "优先覆盖边界值、非法输入、鉴权与幂等场景。"
        )
        return "\n".join(sections)

    @staticmethod
    def _parse_model_output(raw: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """解析模型输出为候选列表。"""
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            return [], [{"name": "<模型输出>", "reason": f"不是合法 JSON：{exc}"}]

        if isinstance(parsed, dict):
            parsed = parsed.get("cases", parsed.get("items", []))

        if not isinstance(parsed, list):
            return [], [{"name": "<模型输出>", "reason": "顶层结构不是数组"}]

        candidates = [item for item in parsed if isinstance(item, dict)]
        dropped = len(parsed) - len(candidates)
        if not dropped:
            return candidates, []
        rejected = [{"name": "<模型输出>", "reason": f"{dropped} 个元素不是对象，已忽略"}]
        return candidates, rejected

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #

    def _validate(self, candidate: dict[str, Any], path: str, method: str) -> GeneratedCase:
        """校验候选用例；不通过则抛 :class:`CaseValidationError`。"""
        # 补齐模型可能漏掉的字段，再由模型校验类型与范围
        normalized = {
            "name": str(candidate.get("name") or "未命名用例"),
            "method": str(candidate.get("method") or method).upper(),
            "path": str(candidate.get("path") or path),
            "payload": candidate.get("payload"),
            "expected_status": candidate.get("expected_status", 200),
            "assertions": candidate.get("assertions") or [],
            "tags": candidate.get("tags") or [],
            "requires_auth": bool(candidate.get("requires_auth", False)),
            "rationale": str(candidate.get("rationale") or ""),
            "confidence": float(candidate.get("confidence", 0.5)),
            "source": candidate.get("source", self.source),
        }

        try:
            generated = GeneratedCase(**normalized)
        except (ValidationError, TypeError, ValueError) as exc:
            raise CaseValidationError(f"结构不合法：{exc}") from exc

        self._assert_path_is_declared(generated)
        self._assert_status_is_plausible(generated)
        self._assert_operators_are_known(generated)

        # 低置信度或缺少理由的候选需要人工过目
        generated.needs_review = (
            generated.confidence < REVIEW_CONFIDENCE_THRESHOLD or not generated.rationale
        )
        return generated

    def _assert_path_is_declared(self, case: GeneratedCase) -> None:
        paths = self.document.get("paths", {})
        if case.path not in paths:
            raise CaseValidationError(f"路径未在 OpenAPI 文档中声明：{case.path}")

        if case.method.lower() not in paths[case.path]:
            raise CaseValidationError(
                f"{case.path} 未声明方法 {case.method}（已声明："
                f"{', '.join(sorted(k.upper() for k in paths[case.path] if k in HTTP_METHODS))}）"
            )

    def _assert_status_is_plausible(self, case: GeneratedCase) -> None:
        if not 100 <= case.expected_status <= 599:
            raise CaseValidationError(f"期望状态码超出 HTTP 范围：{case.expected_status}")

        operation = self.document.get("paths", {}).get(case.path, {}).get(case.method.lower(), {})
        declared = self._declared_statuses(operation)

        # 422 由框架统一产生，通常不会出现在手写文档里，因此放行
        if declared and case.expected_status not in declared | GENERIC_ERROR_STATUS:
            raise CaseValidationError(
                f"期望状态码 {case.expected_status} 未在该操作中声明（已声明：{sorted(declared)}）"
            )

    @staticmethod
    def _assert_operators_are_known(case: GeneratedCase) -> None:
        for assertion in case.assertions:
            if assertion.op not in VALID_OPERATORS:
                raise CaseValidationError(f"未知断言操作符：{assertion.op}")

    # ------------------------------------------------------------------ #
    # 转成平台用例定义
    # ------------------------------------------------------------------ #

    @staticmethod
    def to_platform_payload(case: GeneratedCase) -> dict[str, Any]:
        """转换为平台 ``POST /cases`` 可直接提交的请求体。

        只输出平台用例模型认识的字段——``rationale`` / ``confidence`` 属于生成侧
        元数据，留在报告里，不进用例库。
        """
        return {
            "name": case.name,
            "description": case.rationale,
            "method": case.method.upper(),
            "path": case.path,
            "payload": case.payload,
            "expected_status": case.expected_status,
            "assertions": [Assertion(**a.model_dump()).model_dump() for a in case.assertions],
            "tags": case.tags,
            "requires_auth": case.requires_auth,
        }
