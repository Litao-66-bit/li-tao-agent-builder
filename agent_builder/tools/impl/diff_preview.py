"""diff_preview：生成变更 diff 预览。

安全边界：纯计算（difflib），无 IO，无副作用，无审批。
"""

from __future__ import annotations

import difflib

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单侧内容最大字符数。
MAX_CONTENT_CHARS = 100_000
# 默认上下文行数。
DEFAULT_CONTEXT = 3


def preview_diff(
    old_content: str,
    new_content: str,
    context: int = DEFAULT_CONTEXT,
    label: str = "",
) -> str:
    """生成 unified diff 预览。

    Args:
        old_content: 原始内容。
        new_content: 新内容。
        context: 上下文行数（unified diff 的 n 参数）。
        label: 文件标签（显示为 ``<label> (old)`` / ``<label> (new)``）。

    Returns:
        Unified diff 文本；无差异返回 ``(no differences)``。

    Raises:
        AgentError(E_VALIDATION): 内容为空 / 超长 / context 为负。
    """
    cid = current_correlation_id.get()
    if not old_content and not new_content:
        raise validation_error(
            "diff_preview: old_content 和 new_content 不能同时为空",
            source="tool.diff_preview",
            correlation_id=cid,
        )
    if len(old_content) > MAX_CONTENT_CHARS or len(new_content) > MAX_CONTENT_CHARS:
        raise validation_error(
            f"diff_preview: 内容长度超过上限 {MAX_CONTENT_CHARS}",
            source="tool.diff_preview",
            correlation_id=cid,
        )
    if context < 0:
        raise validation_error(
            f"diff_preview: context 不能为负数: {context}",
            source="tool.diff_preview",
            correlation_id=cid,
        )

    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    from_label = f"{label} (old)" if label else "(old)"
    to_label = f"{label} (new)" if label else "(new)"

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=from_label,
        tofile=to_label,
        n=context,
    )
    result = "".join(diff)
    return result if result else "(no differences)"


spec = ToolSpec(
    name="diff_preview",
    description="生成变更 diff 预览（unified diff，纯计算）",
    parameters={
        "type": "object",
        "properties": {
            "old_content": {"type": "string", "description": "原始内容"},
            "new_content": {"type": "string", "description": "新内容"},
            "context": {
                "type": "integer",
                "description": "上下文行数",
                "default": DEFAULT_CONTEXT,
            },
            "label": {
                "type": "string",
                "description": "文件标签（可选）",
                "default": "",
            },
        },
        "required": ["old_content", "new_content"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, preview_diff)


__all__ = [
    "DEFAULT_CONTEXT",
    "MAX_CONTENT_CHARS",
    "preview_diff",
    "spec",
]
