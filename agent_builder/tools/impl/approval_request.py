"""approval_request：发起审批请求（发审批门）。

安全边界：框架级，无审批。无路径/网络访问，纯记录生成。
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

MAX_REASON_CHARS = 1000


def request_approval(
    tool_call_id: str, reason: str, requested_role: str = ""
) -> str:
    """发起审批请求。

    Args:
        tool_call_id: 待审批的工具调用 ID。
        reason: 审批原因。
        requested_role: 请求审批的角色（可选）。

    Returns:
        确认消息：``approval requested: <req_id>``。

    Raises:
        AgentError(E_VALIDATION): tool_call_id/reason 为空 / reason 超长。
    """
    cid = current_correlation_id.get()
    if not tool_call_id or not tool_call_id.strip():
        raise validation_error(
            "approval_request: tool_call_id 不能为空",
            source="tool.approval_request",
            correlation_id=cid,
        )
    if not reason or not reason.strip():
        raise validation_error(
            "approval_request: reason 不能为空",
            source="tool.approval_request",
            correlation_id=cid,
        )
    if len(reason) > MAX_REASON_CHARS:
        raise validation_error(
            f"approval_request: reason 长度 {len(reason)} 超过上限 {MAX_REASON_CHARS}",
            source="tool.approval_request",
            correlation_id=cid,
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    req_id = f"apr-{ts}-{tool_call_id.strip()[:8]}"
    return f"approval requested: {req_id}"


spec = ToolSpec(
    name="approval_request",
    description="发起审批请求（发审批门）",
    parameters={
        "type": "object",
        "properties": {
            "tool_call_id": {
                "type": "string",
                "description": "待审批的工具调用 ID",
            },
            "reason": {"type": "string", "description": "审批原因"},
            "requested_role": {
                "type": "string",
                "description": "请求审批的角色（可选）",
                "default": "",
            },
        },
        "required": ["tool_call_id", "reason"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, request_approval)


__all__ = ["MAX_REASON_CHARS", "request_approval", "spec"]
