"""质量门禁：覆盖率阈值 + 分层结果汇总。

用法::

    # 跑全量并执行门禁
    .venv/Scripts/python.exe scripts/quality_gate.py

    # 只检查已有报告
    .venv/Scripts/python.exe scripts/quality_gate.py --report-only

门禁规则
--------
1. **核心域必须 100%**：``core`` 下是纯函数逻辑，没有 IO、没有并发，
   没有理由不被完全覆盖；
2. **接口与基础设施 >= 90%**：包含异常分支与降级路径，允许少量防御性代码未覆盖；
3. **整体语句覆盖率 >= 90%**；
4. **零失败**：任何失败或意外通过都阻断。

退出码非 0 表示门禁未通过，可直接用于 CI。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COVERAGE_JSON = PROJECT_ROOT / "coverage.json"

#: 各模块的覆盖率下限。键是 ``tax_service`` 下的路径前缀。
THRESHOLDS: dict[str, float] = {
    "tax_service/core/legacy.py": 100.0,
    "tax_service/core/oracle.py": 100.0,
    "tax_service/core/deductions.py": 100.0,
    "tax_service/core/policy.py": 100.0,
    "tax_service/core/person.py": 100.0,
    "tax_service/core/calculator.py": 100.0,
    "tax_service/services/": 95.0,
    "tax_service/api/": 90.0,
    "tax_service/db/": 90.0,
    "tax_service/infra/": 90.0,
    "tax_service/external/": 90.0,
    "tax_service/config.py": 90.0,
    "tax_service/platform/": 90.0,
    "tax_service/ai/": 85.0,
}

OVERALL_THRESHOLD = 90.0


def run_tests() -> int:
    """跑全量测试并生成 JSON 覆盖率报告。

    命令以字面量列表传入（``shell=False``），不接受任何外部输入拼接。
    ``--cov-report=json`` 默认写入当前工作目录下的 ``coverage.json``，
    因此 ``cwd`` 必须设为项目根目录。
    """
    completed = subprocess.run(  # noqa: S603 - 参数为字面量，无外部输入
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--cov",
            "--cov-report=term-missing",
            "--cov-report=json",
        ],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return completed.returncode


def load_report() -> dict:
    if not COVERAGE_JSON.exists():
        raise SystemExit(f"未找到覆盖率报告：{COVERAGE_JSON}（请先不加 --report-only 运行一次）")
    return json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))


def normalize(path: str) -> str:
    """把报告里的绝对路径归一化为 ``tax_service/...`` 形式。"""
    return Path(path).as_posix().split("src/")[-1]


def evaluate(report: dict) -> tuple[list[str], list[str]]:
    """返回 ``(违规列表, 通过列表)``。"""
    violations: list[str] = []
    passed: list[str] = []

    files: dict[str, dict] = report["files"]

    for prefix, threshold in THRESHOLDS.items():
        matched = {
            normalize(path): data for path, data in files.items() if prefix in normalize(path)
        }
        if not matched:
            violations.append(f"{prefix}: 未匹配到任何文件（阈值 {threshold}%）")
            continue

        worst_path, worst = min(
            matched.items(), key=lambda item: item[1]["summary"]["percent_covered"]
        )
        actual = worst["summary"]["percent_covered"]

        line = f"{prefix}（最低 {worst_path}）：{actual:.1f}% / 阈值 {threshold}%"
        if actual + 1e-9 < threshold:
            violations.append(line)
        else:
            passed.append(line)

    totals = report["totals"]
    overall = totals["percent_covered"]
    branch_covered = totals.get("covered_branches", 0)
    branch_total = totals.get("num_branches", 0)
    branch_percent = (branch_covered / branch_total * 100) if branch_total else 0.0

    overall_line = (
        f"整体：语句 {overall:.1f}%（{totals['covered_lines']}/{totals['num_statements']}）｜"
        f"分支 {branch_percent:.1f}%（{branch_covered}/{branch_total}）"
    )
    if overall + 1e-9 < OVERALL_THRESHOLD:
        violations.append(f"{overall_line} / 阈值 {OVERALL_THRESHOLD}%")
    else:
        passed.append(overall_line)

    return violations, passed


def main() -> int:
    parser = argparse.ArgumentParser(description="TaxTestOps 质量门禁")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="不跑测试，只检查已有的 coverage.json",
    )
    args = parser.parse_args()

    if not args.report_only:
        exit_code = run_tests()
        if exit_code != 0:
            print(f"\n测试未通过（退出码 {exit_code}），门禁失败")
            return exit_code

    report = load_report()
    violations, passed = evaluate(report)

    print("\n" + "=" * 72)
    print("质量门禁结果")
    print("=" * 72)

    for line in passed:
        print(f"  [OK]   {line}")
    for line in violations:
        print(f"  [FAIL] {line}")

    print("=" * 72)
    if violations:
        print(f"门禁失败：{len(violations)} 项未达标")
        return 1

    print(f"门禁通过：{len(passed)} 项全部达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
