"""契约层单元测试：错误码 / 消息协议 / Schema / 状态机。

覆盖 docs/contracts/ 01~04 的关键约束：
- E_PERMISSION 永不重试；错误必带 correlation_id
- 消息类型白名单 13 种；tool_call 只能发给门卫；approval_request 必带 expires_at
- Plan DAG 引用完整与环检测；ToolCall 审批门；RolePerm 工具白名单
- 状态机 10 状态 13 转换；retry_count ≤2；中断必须 resume_point
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import (
    ERROR_NAMES,
    AgentError,
    ErrorInfo,
    is_retryable,
    permission_error,
)
from agent_builder.contracts.messages import (
    APPROVAL_TTL_HOURS,
    TOOL_GATEKEEPER,
    is_valid_message_type,
    make_message,
)
from agent_builder.contracts.schemas import (
    Approval,
    Plan,
    RolePerm,
    Step,
    ToolCall,
)
from agent_builder.contracts.state_machine import (
    TaskEvent,
    TaskStatus,
    TransitionError,
    can_transition,
    next_status,
)

# ── 04 · 错误码体系 ─────────────────────────────────────────────


class TestErrors:
    def test_error_code_table(self):
        assert ERROR_NAMES["E_PERMISSION"] == 1000
        assert ERROR_NAMES["E_TIMEOUT"] == 2000
        assert ERROR_NAMES["E_VALIDATION"] == 3000
        assert ERROR_NAMES["E_MODEL"] == 4000
        assert ERROR_NAMES["E_TOOL"] == 5000
        assert ERROR_NAMES["E_USER_CANCEL"] == 6000
        assert ERROR_NAMES["E_INTERNAL"] == 9000

    def test_permission_never_retryable(self):
        assert is_retryable("E_TIMEOUT") is True
        assert is_retryable("E_VALIDATION") is True
        assert is_retryable("E_PERMISSION") is False
        assert is_retryable("E_USER_CANCEL") is False
        assert is_retryable("E_INTERNAL") is False

    def test_error_info_build_carries_correlation_id(self):
        info = ErrorInfo.build(
            "E_PERMISSION", "路径超出白名单", "tool_gatekeeper", "c-abc123", retryable=False
        )
        assert info.error_code == 1000
        assert info.retryable is False
        assert info.correlation_id == "c-abc123"

    def test_agent_error_factory(self):
        err = permission_error("越权", "gatekeeper", "c-1")
        assert isinstance(err, AgentError)
        assert err.error_name == "E_PERMISSION"
        assert err.retryable is False
        assert err.to_dict()["error_code"] == 1000

    def test_unknown_error_name_rejected(self):
        with pytest.raises(ValueError):
            ErrorInfo.build("E_UNKNOWN", "?", "s", "c-1")


# ── 03 · 消息协议 ───────────────────────────────────────────────


class TestMessages:
    def test_exactly_13_types(self):
        from agent_builder.contracts.messages import MESSAGE_TYPES

        assert len(MESSAGE_TYPES) == 13

    def test_unknown_type_rejected(self):
        assert is_valid_message_type("chat") is False
        with pytest.raises(ValueError, match="未知消息类型"):
            make_message(
                "chat",
                msg_id="m-1",
                from_role="planner",
                to="user",
                task_id="t-1",
                correlation_id="c-1",
                ts="2026-09-26T10:00:00+08:00",
            )

    def test_tool_call_only_to_gatekeeper(self):
        with pytest.raises(ValueError, match="工具门卫"):
            make_message(
                "tool_call",
                msg_id="m-1",
                from_role="code_worker",
                to="user",
                task_id="t-1",
                correlation_id="c-1",
                ts="2026-09-26T10:00:00+08:00",
                tool="file_read",
                args={},
            )

    def test_tool_call_to_gatekeeper_ok(self):
        msg = make_message(
            "tool_call",
            msg_id="m-1",
            from_role="code_worker",
            to=TOOL_GATEKEEPER,
            task_id="t-1",
            correlation_id="c-1",
            ts="2026-09-26T10:00:00+08:00",
            tool="file_read",
            args={"path": "/workspace/x.py"},
        )
        assert msg.type == "tool_call"
        assert msg.payload["tool"] == "file_read"

    def test_approval_request_requires_expires_at(self):
        with pytest.raises(ValueError, match="expires_at"):
            make_message(
                "approval_request",
                msg_id="m-2",
                from_role="tool_gatekeeper",
                to="user",
                task_id="t-1",
                correlation_id="c-1",
                ts="2026-09-26T10:00:00+08:00",
                approval_type="file_write",
                target="/workspace/x.py",
                detail="覆盖已有文件",
            )
        assert APPROVAL_TTL_HOURS == 24

    def test_tool_call_missing_fields(self):
        with pytest.raises(ValueError, match="关键字段"):
            make_message(
                "tool_call",
                msg_id="m-3",
                from_role="code_worker",
                to=TOOL_GATEKEEPER,
                task_id="t-1",
                correlation_id="c-1",
                ts="2026-09-26T10:00:00+08:00",
            )


# ── 01 · Schema ─────────────────────────────────────────────────


class TestSchemas:
    def test_step_invalid_status(self):
        with pytest.raises(ValueError):
            Step(id="s-1", action="code_gen", status="boom")

    def test_plan_rejects_missing_step(self):
        plan = Plan(task_id="t-1", order=["s-2"])
        with pytest.raises(ValueError, match="不存在的步骤"):
            plan.validate_steps({"s-1": Step(id="s-1", action="x")})

    def test_plan_detects_cycle(self):
        s1 = Step(id="s-1", action="a", depends_on=["s-2"])
        s2 = Step(id="s-2", action="b", depends_on=["s-1"])
        plan = Plan(task_id="t-1", order=["s-1", "s-2"])
        with pytest.raises(ValueError, match="环依赖"):
            plan.validate_steps({"s-1": s1, "s-2": s2})

    def test_plan_acyclic_ok(self):
        s1 = Step(id="s-1", action="a")
        s2 = Step(id="s-2", action="b", depends_on=["s-1"])
        s3 = Step(id="s-3", action="c", depends_on=["s-1"])
        plan = Plan(task_id="t-1", order=["s-1", "s-2", "s-3"], parallel_groups=[["s-2", "s-3"]])
        plan.validate_steps({"s-1": s1, "s-2": s2, "s-3": s3})  # 不抛错即通过

    def test_toolcall_approval_gate(self):
        denied = ToolCall(
            audit_id="ac-1",
            role="code_worker",
            tool="file_write",
            approval=Approval(required=True),
        )
        assert denied.is_approved() is False  # 门卫必须拒绝

        granted = ToolCall(
            audit_id="ac-2",
            role="code_worker",
            tool="file_write",
            approval=Approval(required=True, granted_by="user", ts="2026-09-26T10:00:00+08:00"),
        )
        assert granted.is_approved() is True

    def test_roleperm_high_risk_within_allowed(self):
        with pytest.raises(ValueError, match="超出"):
            RolePerm(role="code_worker", allowed_tools=["file_read"], high_risk_tools=["file_write"])

    def test_roleperm_gatekeeper_allowance(self):
        perm = RolePerm(role="code_worker", allowed_tools=["file_read", "file_write"], high_risk_tools=["file_write"])
        assert perm.is_allowed("file_read") is True
        assert perm.is_allowed("git_commit") is False  # 未列出 → 一律拒绝
        assert perm.is_high_risk("file_write") is True


# ── 02 · 状态机 ─────────────────────────────────────────────────


class TestStateMachine:
    def test_happy_path(self):
        seq = [
            (TaskStatus.RECEIVED, TaskEvent.REQ_CONFIRMED, TaskStatus.PLANNING),
            (TaskStatus.PLANNING, TaskEvent.PLAN_READY, TaskStatus.AWAITING_CONFIRM),
            (TaskStatus.AWAITING_CONFIRM, TaskEvent.PLAN_ACCEPTED, TaskStatus.EXECUTING),
            (TaskStatus.EXECUTING, TaskEvent.ALL_STEPS_DONE, TaskStatus.VERIFYING),
            (TaskStatus.VERIFYING, TaskEvent.VERIFY_PASSED, TaskStatus.DELIVERING),
            (TaskStatus.DELIVERING, TaskEvent.DELIVERED, TaskStatus.DELIVERED),
        ]
        for cur, event, expected in seq:
            assert next_status(cur, event) == expected

    def test_illegal_transition(self):
        assert can_transition(TaskStatus.RECEIVED, TaskEvent.ALL_STEPS_DONE) is False
        with pytest.raises(TransitionError):
            next_status(TaskStatus.RECEIVED, TaskEvent.ALL_STEPS_DONE)

    def test_any_to_failed(self):
        for cur in TaskStatus:
            assert next_status(cur, TaskEvent.INTERNAL_ERROR) == TaskStatus.FAILED


# ── Conductor 编排器 ────────────────────────────────────────────


def _make_plan() -> Plan:
    return Plan(task_id="t-1", order=["s-1"])
