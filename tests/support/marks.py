"""缺陷标记工具：让同一套用例在「含缺陷」与「已修复」两种模式下都成立。

背景
----
一个只会在缺陷上「报红」的测试集，无法证明它真的在验证正确性——它可能只是恰好
断言了一个错误的值。要证明测试有效，必须能演示：**切到正确实现后，所有用例真正通过**。

做法
----
服务层通过 ``TAX_CALCULATOR_IMPL`` 在两种实现间切换：

- ``legacy``（默认）：实验给定的原实现，保留全部缺陷；
- ``standard``：按需求规格修正后的实现。

需求符合性断言用 :func:`defect` 标记：

- ``legacy`` 模式下标记为 ``xfail(strict=True)``——已知不符合需求，暂不计为失败；
- ``standard`` 模式下**不加任何标记**——必须真正通过。

于是同一套用例同时承担两个职责：缺陷回归护栏 + 修复验证。
"""

from __future__ import annotations

import os

import pytest

CALCULATOR_IMPL_ENV = "TAX_CALCULATOR_IMPL"
DEFAULT_IMPL = "legacy"


def current_impl() -> str:
    """当前生效的计算器实现标识。"""
    return os.getenv(CALCULATOR_IMPL_ENV, DEFAULT_IMPL)


def is_legacy_mode() -> bool:
    """是否运行在「保留缺陷」模式。"""
    return current_impl() == "legacy"


def defect(defect_id: str, summary: str) -> pytest.MarkDecorator:
    """标记一条「需求符合性」断言。

    :param defect_id: 缺陷编号，如 ``"D1"``。
    :param summary: 缺陷摘要，会出现在 pytest 报告中。
    """
    return pytest.mark.xfail(
        is_legacy_mode(),
        strict=True,
        reason=f"{defect_id}：{summary}",
    )


def legacy_only(reason: str) -> pytest.MarkDecorator:
    """标记「只对 legacy 实现成立」的用例。

    用于**特征化断言**——它们描述的是实验给定实现的当前行为（含缺陷值），
    在 ``standard`` 模式下这些行为已不存在，因此应当跳过而不是失败。

    与 :func:`defect` 的区别：``defect`` 说的是「需求未被满足」（修复后应转为通过）；
    ``legacy_only`` 说的是「这是在刻画旧实现」（修复后应被移除或改写）。
    """
    return pytest.mark.skipif(
        not is_legacy_mode(),
        reason=f"仅适用于 legacy 实现：{reason}",
    )
