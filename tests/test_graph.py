"""最小闭环全图测试：MockClient 驱动 decompose→schedule→confirm→execute→verify→summarize。

覆盖：
- 完整闭环：invoke 停在确认点 → 确认恢复 → 产出报告
- 用户拒绝：E_USER_CANCEL 中止
- 门卫：角色权限 / 沙箱路径 / 高风险审批门
- MockClient：分解 JSON 路由
"""

from __future__ import annotations

from langgraph.types import Command

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Approval, RolePerm, ToolCall
from agent_builder.graph.build import build_graph
from agent_builder.llm.client import MockClient
from agent_builder.tools.gatekeeper import WORKSPACE_DIR, ToolGatekeeper

REQUIREMENT = "帮我生成一个每日新闻摘要 Agent"


class TestMinimalLoop:
    def _run(self):
        llm = MockClient(correlation_id="c-test")
        task_id = "t-loop-1"
        config = {"configurable": {"thread_id": task_id}}
        graph = build_graph(llm)
        return llm, task_id, config, graph

    def test_full_loop_with_confirmation(self):
        llm, task_id, config, graph = self._run()

        # 第一次 invoke：停在 confirm（interrupt），拿到计划
        first = graph.invoke(
            {"task_id": task_id, "requirement": REQUIREMENT, "correlation_id": "c-1"}, config
        )
        assert "__interrupt__" in first
        plan = first["plan"]
        assert plan["order"] == ["step-001"]
        assert plan["confirmed_by_user"] is False
        assert first["steps"][0]["action"] == "llm_think"

        # 用户确认 → 恢复执行
        final = graph.invoke(Command(resume={"confirmed": True}), config)
        assert final["report"].startswith("mock 最终报告")
        assert final["results"]["step-001"].startswith("mock 执行结果")
        # 计划已标记确认
        assert final["plan"]["confirmed_by_user"] is True
        # 分解器与执行者、汇报员均被调用过
        assert len(llm.calls) >= 3

    def test_user_reject_aborts(self):
        _, task_id, config, graph = self._run()
        first = graph.invoke(
            {"task_id": task_id, "requirement": REQUIREMENT, "correlation_id": "c-2"}, config
        )
        assert "__interrupt__" in first
        import pytest

        with pytest.raises(AgentError) as exc_info:
            graph.invoke(Command(resume={"confirmed": False}), config)
        assert exc_info.value.error_name == "E_USER_CANCEL"

    def test_mock_client_decompose_json(self):
        llm = MockClient()
        raw = llm.chat_json("你是「分解器」...", REQUIREMENT)
        assert raw["steps"][0]["id"] == "step-001"


class TestGatekeeper:
    def _gatekeeper(self) -> ToolGatekeeper:
        perms = {
            "code_worker": RolePerm(
                role="code_worker",
                allowed_tools=["file_read", "file_write", "llm_think"],
                high_risk_tools=["file_write"],
            )
        }
        return ToolGatekeeper(perms, correlation_id="c-gate")

    def test_unknown_role_rejected(self):
        gk = self._gatekeeper()
        call = ToolCall(audit_id="a-1", role="hacker", tool="file_read", args={})
        try:
            gk.check(call)
            raise AssertionError("应当拒绝未注册角色")
        except AgentError as err:
            assert err.error_name == "E_PERMISSION"
            assert err.retryable is False

    def test_unlisted_tool_rejected(self):
        gk = self._gatekeeper()
        call = ToolCall(audit_id="a-2", role="code_worker", tool="git_commit", args={})
        try:
            gk.check(call)
            raise AssertionError("应当拒绝未列入白名单的工具")
        except AgentError as err:
            assert err.error_name == "E_PERMISSION"

    def test_high_risk_requires_approval(self):
        gk = self._gatekeeper()
        # file_write 是高风险：未审批 → 拒绝
        call = ToolCall(
            audit_id="a-3",
            role="code_worker",
            tool="file_write",
            args={"path": "/tmp/x.py"},
            approval=Approval(required=True),
        )
        try:
            gk.check(call)
            raise AssertionError("高风险工具未审批应当被拒")
        except AgentError as err:
            assert err.error_name == "E_PERMISSION"

        # 审批后 → 放行
        granted = ToolCall(
            audit_id="a-4",
            role="code_worker",
            tool="file_write",
            args={"path": str(WORKSPACE_DIR / "demo.txt")},
            approval=Approval(required=True, granted_by="user", ts="2026-09-26T10:00:00+08:00"),
        )
        assert gk.check(granted).status == "executed"

    def test_path_escape_rejected(self):
        gk = self._gatekeeper()
        call = ToolCall(
            audit_id="a-5",
            role="code_worker",
            tool="file_read",
            args={"path": "/etc/passwd"},
        )
        try:
            gk.check(call)
            raise AssertionError("路径越权应当被拒")
        except AgentError as err:
            assert err.error_name == "E_PERMISSION"

    def test_audit_log_records(self):
        gk = self._gatekeeper()
        call = ToolCall(audit_id="a-6", role="code_worker", tool="llm_think", args={})
        gk.check(call)
        snapshot = gk.snapshot()
        assert snapshot[-1]["allowed"] is True
        assert snapshot[-1]["tool"] == "llm_think"
