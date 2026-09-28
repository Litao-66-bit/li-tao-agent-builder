"""change_notify：变更通知（发编程者）。

安全边界：框架级，无审批。无路径/网络访问，纯记录生成。
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

MAX_SUMMARY_CHARS = 2000
VALID_CHANGE_TYPES = frozenset({"code", "config", "doc", "test", "other"})


def notify_change(target: str, change_type: str, summary: str) -> str:
    """发送变更通知。

    Args:
        target: 通知接收者（如角色名或用户 ID）。
        change_type: 变更类型 ``code`` / ``config`` / ``doc`` / ``test`` / ``other``。
        summary: 变更摘要。

    Returns:
        确认消息：``notified <target>: <notify_id>``。

    Raises:
        AgentError(E_VALIDATION): target/summary 为空 / change_type 非法 / summary 超长。
    """
    cid = current_correlation_id.get()
    if not target or not target.strip():
        raise validation_error(
            "change_notify: target 不能为空",
            source="tool.change_notify",
            correlation_id=cid,
        )
    if change_type not in VALID_CHANGE_TYPES:
        raise validation_error(
            f"change_notify: change_type 非法 {change_type!r}，可选: {sorted(VALID_CHANGE_TYPES)}",
            source="tool.change_notify",
            correlation_id=cid,
        )
    if not summary or not summary.strip():
        raise validation_error(
            "change_notify: summary 不能为空",
            source="tool.change_notify",
            correlation_id=cid,
        )
    if len(summary) > MAX_SUMMARY_CHARS:
        raise validation_error(
            f"change_notify: summary 长度 {len(summary)} 超过上限 {MAX_SUMMARY_CHARS}",
            source="tool.change_notify",
            correlation_id=cid,
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    notify_id = f"ntf-{ts}-{target.strip()[:8]}"
    return f"notified {target.strip()}: {notify_id}"


spec = ToolSpec(
    name="change_notify",
    description="变更通知（发编程者）",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "通知接收者"},
            "change_type": {
                "type": "string",
                "enum": ["code", "config", "doc", "test", "other"],
                "description": "变更类型",
            },
            "summary": {"type": "string", "description": "变更摘要"},
        },
        "required": ["target", "change_type", "summary"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, notify_change)


__all__ = [
    "MAX_SUMMARY_CHARS",
    "VALID_CHANGE_TYPES",
    "notify_change",
    "spec",
]
