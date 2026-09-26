"""消息协议（docs/contracts/03-message-protocol.md 的实现）。

角色之间只通过结构化消息通信（禁止自由聊天）。每条消息带统一头 + 类型体。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 13 种消息类型（契约 03 清单，禁止新增未经契约层评审的类型）────────
MESSAGE_TYPES: frozenset[str] = frozenset({
    "task_assign",      # Conductor/路由者 → 执行者：分派步骤
    "tool_call",        # 任意角色 → 工具门卫：请求工具执行
    "tool_result",      # 工具门卫 → 请求方：返回结果或拒绝
    "plan_proposal",    # 调度器 → Conductor → 用户：展示执行计划
    "plan_confirm",     # 用户 → Conductor：确认/修改计划
    "report",           # 执行者/验证者 → 汇总方：交付阶段性产出
    "rework_request",   # 验证组 → Conductor：打回返工
    "approval_request", # 门卫/看门人 → 用户：高风险动作求批
    "approval_result",  # 用户 → 请求方：审批结果
    "interrupt",        # 用户/门卫 → Conductor：请求中断
    "metric_report",    # 主架构 → 审计员：上报运行指标
    "change_proposal",  # 审计员/方案生成者 → 编程者：提交变更提案
    "change_notify",    # 记录员 → 编程者：变更完成通知
})

# 规则 2：tool_call 只能发给工具门卫（唯一执行出口）。
TOOL_GATEKEEPER = "tool_gatekeeper"

# 规则 3：approval_request 默认有效期（小时）。
APPROVAL_TTL_HOURS = 24


def is_valid_message_type(msg_type: str) -> bool:
    return msg_type in MESSAGE_TYPES


@dataclass(slots=True)
class Message:
    """统一消息头 + 类型体（契约 03 · 统一消息头 JSON）。"""

    msg_id: str
    type: str
    from_role: str
    to: str
    task_id: str
    correlation_id: str
    ts: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not is_valid_message_type(self.type):
            raise ValueError(
                f"未知消息类型: {self.type!r}，契约只允许 13 种类型: {sorted(MESSAGE_TYPES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "type": self.type,
            "from": self.from_role,
            "to": self.to,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "ts": self.ts,
            **self.payload,
        }


def make_message(
    msg_type: str,
    *,
    msg_id: str,
    from_role: str,
    to: str,
    task_id: str,
    correlation_id: str,
    ts: str,
    **payload: Any,
) -> Message:
    """构造一条结构化消息，并执行契约级校验。"""
    msg = Message(
        msg_id=msg_id,
        type=msg_type,
        from_role=from_role,
        to=to,
        task_id=task_id,
        correlation_id=correlation_id,
        ts=ts,
        payload=payload,
    )

    # 规则 2：tool_call 只能发给工具门卫。
    if msg_type == "tool_call" and to != TOOL_GATEKEEPER:
        raise ValueError(
            f"tool_call 只能发给工具门卫（{TOOL_GATEKEEPER}），收到 to={to!r}"
        )

    # 规则 3：approval_request 必须带 expires_at。
    if msg_type == "approval_request" and "expires_at" not in payload:
        raise ValueError("approval_request 必须携带 expires_at 字段")

    # 关键字段校验：tool_call 需要 tool/args；tool_result 需要 audit_id/status。
    if msg_type == "tool_call":
        missing = [k for k in ("tool", "args") if k not in payload]
        if missing:
            raise ValueError(f"tool_call 缺少关键字段: {missing}")
    if msg_type == "tool_result":
        missing = [k for k in ("audit_id", "status") if k not in payload]
        if missing:
            raise ValueError(f"tool_result 缺少关键字段: {missing}")
    if msg_type == "plan_confirm":
        missing = [k for k in ("confirmed",) if k not in payload]
        if missing:
            raise ValueError(f"plan_confirm 缺少关键字段: {missing}")
    if msg_type == "interrupt":
        missing = [k for k in ("reason",) if k not in payload]
        if missing:
            raise ValueError(f"interrupt 缺少关键字段: {missing}")

    return msg


__all__ = [
    "APPROVAL_TTL_HOURS",
    "MESSAGE_TYPES",
    "TOOL_GATEKEEPER",
    "Message",
    "is_valid_message_type",
    "make_message",
]
