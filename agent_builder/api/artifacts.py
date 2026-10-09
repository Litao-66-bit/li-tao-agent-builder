"""产物提取 —— 「这一步到底产出了哪些文件」的唯一判定处。

为什么单独一个模块：agentic 循环（``agent_loop``）与固定工作流（``orchestrator``）
都要用同一套判定，但 ``deciders`` 反向 import 了 ``orchestrator``，
``orchestrator`` 再 import ``agent_loop`` 会成环。放在这里两边都能安全引用。

判定规则（顺序即优先级）：
1. 结构化结果里的 ``files_changed``（角色自己报告的产出）；
2. 兜底：**真正写盘**的动作（``file_write``）+ 步骤输入里的 ``path``。

注意：只读动作（列出文件 / 读取文件……）的 ``inputs.path`` **不算产物** ——
这正是「列出文件」也被渲染出「查看产物」按钮、点开却报「路径不是文件: .」的根因。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_builder.contracts.schemas import Step

# 会真正写盘的动作 —— 只有这些的 inputs.path 才算产物。
# file_edit 同样会改盘上的文件（卡 1 的教训：只读动作的 path 绝不能算产物）。
_FILE_PRODUCING_ACTIONS: frozenset[str] = frozenset({"file_write", "file_edit"})


def artifacts_of(step: Step, result: Any) -> list[str]:
    """提取一步的产物路径（相对工作区）。空列表 = 没产出。"""
    if isinstance(result, Mapping):
        files = result.get("files_changed")
        if isinstance(files, (list, tuple)):
            return [str(item) for item in files if str(item).strip()]
    path = step.inputs.get("path") if isinstance(step.inputs, Mapping) else None
    if step.action in _FILE_PRODUCING_ACTIONS and isinstance(path, str) and path.strip():
        return [path]
    return []


__all__ = ["artifacts_of"]
