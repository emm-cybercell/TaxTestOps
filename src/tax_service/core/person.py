"""纳税人数据模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Person:
    """纳税人。

    字段顺序与实验给定实现的构造器 ``Person(children_Num, is_KeepStu,
    is_HouseLoans, is_RentHouse, is_SupportingElderly, sibling_Num)`` 一致，
    便于逐条比对。

    .. warning::
       本类**刻意不做字段校验**，以保留 SUT 的原始输入契约。非法取值
       （如 ``is_keep_stu=3``、``sibling_num=-1``）会被原样传递给计算逻辑，
       由此暴露的缺陷见 ``docs/05-缺陷分析报告.md`` 的 D3 / D5。
    """

    children_num: int = 0
    """子女个数，每个子女每月扣除 1000 元。"""

    is_keep_stu: int = 0
    """继续教育状态。

    - ``0``：未继续教育
    - ``1``：接受继续教育且当年未获取到证书，每月扣除 400 元
    - ``2``：接受继续教育且当年获取到证书，当年扣除定额 3600 元
    """

    house_loans: bool = False
    """是否发生住房贷款利息，是则每月扣除 1000 元。"""

    is_rent_house: int = 0
    """住房租金所在城市档次。

    - ``0``：无住房租金
    - ``1``：直辖市、省会（首府）城市，每月 1500 元
    - ``2``：市辖区户籍人口超过 100 万的城市，每月 1100 元
    - ``3``：市辖区户籍人口不超过 100 万的城市，每月 800 元
    """

    supporting_elderly: bool = False
    """是否赡养老人，是则每月扣除 2000 元并由兄弟姐妹分摊。"""

    sibling_num: int = 0
    """兄弟姐妹个数（不含本人）。"""
