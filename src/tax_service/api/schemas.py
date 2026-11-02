"""接口层请求/响应模型。

边界校验策略
------------
接口层用 Pydantic 做**输入格式校验**：非法枚举值、负数、超范围数值在进入业务逻辑前
即被拒绝（HTTP 422）。

这带来一个值得注意的测试结论：**边界校验能挡住 D3（除零）与 D5（非法枚举静默忽略），
但挡不住 D1（速算扣除数算错）与 D4（年度项按月扣）**——后两者是数值语义缺陷，输入
格式完全合法。该结论由接口层测试断言，见 ``tests/api/test_tax_api.py``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SalaryIn = Annotated[
    int, Field(ge=0, le=10_000_000, description="月工资（已扣除三险一金），单位：元")
]
"""工资输入约束：非负且不超过 1000 万元，防止整数溢出与异常放大。"""


class PersonIn(BaseModel):
    """纳税人输入。"""

    model_config = ConfigDict(extra="forbid")

    children_num: Annotated[int, Field(ge=0, le=20, description="子女个数")] = 0
    is_keep_stu: Annotated[
        Literal[0, 1, 2],
        Field(description="继续教育状态：0 未继续教育 / 1 学历期间 / 2 取得证书当年"),
    ] = 0
    house_loans: Annotated[bool, Field(description="是否有住房贷款利息")] = False
    is_rent_house: Annotated[
        Literal[0, 1, 2, 3],
        Field(
            description=(
                "住房租金城市档次：0 无 / 1 直辖市省会（1500 元）/ "
                "2 人口超 100 万（1100 元）/ 3 人口不超 100 万（800 元）"
            )
        ),
    ] = 0
    supporting_elderly: Annotated[bool, Field(description="是否赡养老人")] = False
    sibling_num: Annotated[int, Field(ge=0, le=20, description="兄弟姐妹个数")] = 0


class CalculateRequest(BaseModel):
    """单次测算请求。"""

    model_config = ConfigDict(extra="forbid")

    salary: SalaryIn
    person: PersonIn = Field(default_factory=PersonIn)
    idempotency_key: Annotated[
        str | None,
        Field(
            default=None,
            min_length=1,
            max_length=64,
            description="幂等键；同一用户重复提交同一键只落库一次并返回首次结果",
        ),
    ] = None
    persist: Annotated[bool, Field(default=True, description="是否落库")] = True


class DeductionItemOut(BaseModel):
    """扣除明细项。"""

    code: str
    label: str
    amount: int


class CalculateResponse(BaseModel):
    """测算响应。"""

    record_id: int | None = Field(default=None, description="落库记录 ID；未落库时为 null")
    taxable_income: int = Field(description="应纳税所得额（需求口径）")
    tax_amount: int = Field(description="应纳税额，由当前配置的计算器实现给出")
    calculator_impl: str = Field(description="产生 tax_amount 的实现标识：legacy / standard")
    deductions: list[DeductionItemOut] = Field(default_factory=list)
    created_at: datetime | None = None


class BatchItemIn(BaseModel):
    """批量申报的单项。"""

    model_config = ConfigDict(extra="forbid")

    salary: SalaryIn
    person: PersonIn = Field(default_factory=PersonIn)
    external_ref: Annotated[str | None, Field(default=None, max_length=64)] = None


class BatchRequest(BaseModel):
    """批量申报请求。"""

    model_config = ConfigDict(extra="forbid")

    items: Annotated[list[BatchItemIn], Field(min_length=1, description="至少一项")]
    idempotency_key: Annotated[str | None, Field(default=None, min_length=1, max_length=64)] = None


class BatchItemResult(BaseModel):
    """批量申报的单项结果。"""

    index: int
    external_ref: str | None = None
    ok: bool
    record_id: int | None = None
    taxable_income: int | None = None
    tax_amount: int | None = None
    error: str | None = None


class BatchResponse(BaseModel):
    """批量申报响应。"""

    total: int
    succeeded: int
    failed: int
    items: list[BatchItemResult]


class RecordOut(BaseModel):
    """历史记录项。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    salary: int
    taxable_income: int
    tax_amount: int
    calculator_impl: str
    children_num: int
    is_keep_stu: int
    house_loans: bool
    is_rent_house: int
    supporting_elderly: bool
    sibling_num: int
    created_at: datetime


class RecordPage(BaseModel):
    """分页历史记录。"""

    total: int
    limit: int
    offset: int
    items: list[RecordOut]


class LoginRequest(BaseModel):
    """登录请求。"""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=3, max_length=64)]
    password: Annotated[str, Field(min_length=6, max_length=128)]


class TokenResponse(BaseModel):
    """登录响应。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="有效期，单位：秒")


class PolicyParametersOut(BaseModel):
    """政策参数快照。"""

    version: str
    basic_deduction: int
    effective_from: str
    source: str = Field(description="参数来源：remote / cache / fallback")


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: str
    app_name: str
    environment: str
    calculator_impl: str
    database: str
    cache: str
    version: str


class ErrorResponse(BaseModel):
    """统一错误响应。"""

    detail: str
