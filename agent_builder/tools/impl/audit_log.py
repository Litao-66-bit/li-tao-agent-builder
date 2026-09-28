"""audit_log：写审计日志（谁 / 做了什么）。

安全边界：只追加不可篡改，无审批。门卫只做角色权限校验。
存储层：audit_store.append_audit 追加到 JSON 文件，不提供修改/删除接口。
"""

from __future__ import annotations

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.impl.audit_store import append_audit
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# detail 字段最大字符数。
MAX_DETAIL_CHARS = 2000


def log_audit(
    role: str,
    action: str,
    detail: str = "",
    correlation_id: str = "",
) -> str:
    """写一条审计日志。

    Args:
        role: 执行者角色（如 operator / memory_manager）。
        action: 执行的动作描述（如 "file_write:/tmp/x.py"）。
        detail: 额外详情（可选，截断到 MAX_DETAIL_CHARS）。
        correlation_id: 关联 ID；空则用当前上下文的 correlation_id。

    Returns:
        确认消息：``logged <id>``。

    Raises:
        AgentError(E_VALIDATION): role/action 为空 / detail 超长。
        AgentError(E_TOOL): 存储读写失败（由 audit_store 抛出）。
    """
    cid = current_correlation_id.get()
    if not role or not role.strip():
        raise validation_error(
            "audit_log: role 不能为空",
            source="tool.audit_log",
            correlation_id=cid,
        )
    if not action or not action.strip():
        raise validation_error(
            "audit_log: action 不能为空",
            source="tool.audit_log",
            correlation_id=cid,
        )
    if len(detail) > MAX_DETAIL_CHARS:
        raise validation_error(
            f"audit_log: detail 长度 {len(detail)} 超过上限 {MAX_DETAIL_CHARS}",
            source="tool.audit_log",
            correlation_id=cid,
        )

    cid_to_use = correlation_id if correlation_id.strip() else cid
    record_id = append_audit(role.strip(), action.strip(), detail, cid_to_use)
    return f"logged {record_id}"


spec = ToolSpec(
    name="audit_log",
    description="写审计日志（只追加不可篡改）",
    parameters={
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "执行者角色"},
            "action": {"type": "string", "description": "动作描述"},
            "detail": {
                "type": "string",
                "description": "额外详情（可选）",
                "default": "",
            },
            "correlation_id": {
                "type": "string",
                "description": "关联 ID（空则用当前上下文）",
                "default": "",
            },
        },
        "required": ["role", "action"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, log_audit)


__all__ = ["MAX_DETAIL_CHARS", "log_audit", "spec"]
