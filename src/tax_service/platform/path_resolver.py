"""按点号路径从响应体中取值，供数据驱动断言使用。

为什么单独成文件
----------------
断言路径来自用例定义（用户输入），因此这里是**唯一**需要按字符串动态访问结构的位置。
把它隔离出来有两个好处：

1. 安全边界清晰——只有这一处做动态取值，便于审查与加固；
2. 便于单独测试——路径解析是断言功能的核心，值得独立覆盖。
"""

from __future__ import annotations

from typing import Any

PATH_SEPARATOR = "."


class PathResolutionError(KeyError):
    """路径无法解析。"""


def _descend_into_list(node: list, part: str, path: str) -> Any:
    try:
        index = int(part)
    except ValueError as exc:
        raise PathResolutionError(f"路径 {path} 期望列表下标，实际收到 {part}") from exc

    if index >= len(node) or index < -len(node):
        raise PathResolutionError(f"路径 {path} 的下标 {index} 越界（列表长度 {len(node)}）")
    return node[index]


def _descend_into_mapping(node: dict, part: str, path: str) -> Any:
    if part not in node:
        available = ", ".join(sorted(str(key) for key in node)) or "（空对象）"
        raise PathResolutionError(f"路径 {path} 中不存在字段 {part}；可用字段：{available}")
    return node.get(part)


def resolve_path(payload: Any, path: str) -> Any:
    """按点号路径取值，支持列表下标。

    ``"deductions.0.amount"`` → ``payload["deductions"][0]["amount"]``

    空路径返回整个响应体，用于「断言整体结构」的场景。

    :raises PathResolutionError: 路径不存在或类型不匹配。错误信息包含完整路径与
        可用字段，让失败原因可以自解释，不必回查响应体。
    """
    if not path:
        return payload

    node = payload
    for part in path.split(PATH_SEPARATOR):
        if isinstance(node, list):
            node = _descend_into_list(node, part, path)
        elif isinstance(node, dict):
            node = _descend_into_mapping(node, part, path)
        else:
            raise PathResolutionError(
                f"路径 {path} 在字段 {part} 处无法继续解析：当前值为 {type(node).__name__}"
            )
    return node
