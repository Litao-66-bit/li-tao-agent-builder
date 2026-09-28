"""Proposer 方案生成者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：定位根因/无根因/数据根因/多根因拆分/拒绝/权限不足
- 边界：空步骤
- 授权：proposer 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.proposer import (
    ROOT_CAUSE_TYPES,
    Proposer,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_proposer() -> Proposer:
    return Proposer(correlation_id="c-test")


def _make_step(
    action: str = "propose",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestProposerFunctional:
    def test_single_root_cause(self):
        """单根因 → 单提案。"""
        p = _make_proposer()
        step = _make_step("propose", {
            "root_causes": [{"type": "prompt"}],
            "proposals": [
                {
                    "proposal_id": "p-1",
                    "target_module": "roles/conductor.py",
                    "root_cause": "prompt",
                    "change_desc": "优化提示词",
                    "expected_benefit": "提升准确率",
                    "failure_criteria": "成功率仍低于 0.85",
                },
            ],
        })
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert len(result.proposals) == 1
        assert result.proposals[0].root_cause == "prompt"
        assert result.no_root_cause is False

    def test_no_root_cause(self):
        """无根因 → 不下方案。"""
        p = _make_proposer()
        step = _make_step("propose", {
            "root_causes": [],
            "proposals": [],
        })
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.no_root_cause is True
        assert result.proposals == []

    def test_data_root_cause_suggestion(self):
        """根因在数据 → 建议补充知识库。"""
        p = _make_proposer()
        step = _make_step("propose", {
            "root_causes": [{"type": "data"}],
            "proposals": [
                {"root_cause": "data", "change_desc": "补充知识库"},
            ],
        })
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert result.proposals[0].root_cause == "data"
        assert result.proposals[0].is_data_suggestion is True

    def test_multiple_root_causes_split(self):
        """多根因 → 拆成多个最小提案。"""
        p = _make_proposer()
        step = _make_step("propose", {
            "root_causes": [{"type": "prompt"}, {"type": "tool"}],
            "proposals": [
                {"proposal_id": "p-1", "root_cause": "prompt", "change_desc": "改提示词"},
                {"proposal_id": "p-2", "root_cause": "tool", "change_desc": "改工具"},
            ],
        })
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert len(result.proposals) == 2
        assert result.proposals[0].proposal_id == "p-1"
        assert result.proposals[1].proposal_id == "p-2"

    def test_optimize_action_accepted(self):
        """optimize 是方案生成类 action。"""
        p = _make_proposer()
        step = _make_step("optimize", {
            "root_causes": [{"type": "process"}],
            "proposals": [{"root_cause": "process"}],
        })
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        p = _make_proposer()
        step = _make_step("propose")
        result = p.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_propose_action(self):
        """非方案生成类 → rejected。"""
        p = _make_proposer()
        step = _make_step("file_write")
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出优化范围" in result.error

    def test_permission_denied(self):
        """权限不足 → failed。"""
        p = _make_proposer()
        step = _make_step("propose", {"root_causes": [{"type": "prompt"}]})

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = p.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        p = _make_proposer()
        step = _make_step("propose")

        def fn(s: Step) -> str:
            raise RuntimeError("方案生成错误")

        result = p.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_invalid_root_cause_defaults_process(self):
        """非法根因 → 默认 process。"""
        p = _make_proposer()
        step = _make_step("propose", {
            "root_causes": [{"type": "invalid"}],
            "proposals": [{"root_cause": "invalid"}],
        })
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert result.proposals[0].root_cause == "process"

    def test_root_causes_no_proposals_generates_skeleton(self):
        """有根因无提案 → 生成空提案骨架。"""
        p = _make_proposer()
        step = _make_step("propose", {
            "root_causes": [{"type": "prompt"}, {"type": "tool"}],
            "proposals": [],
        })
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert len(result.proposals) == 2  # 每个根因一个骨架
        assert result.proposals[0].root_cause == "prompt"
        assert result.proposals[1].root_cause == "tool"


# ── 边界 ────────────────────────────────────────────────────────


class TestProposerEdge:
    def test_empty_inputs(self):
        """空输入 → 无根因。"""
        p = _make_proposer()
        step = _make_step("propose", {})
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.no_root_cause is True

    def test_diff_preview_included(self):
        """提案包含 diff 预览。"""
        p = _make_proposer()
        step = _make_step("propose", {
            "root_causes": [{"type": "tool"}],
            "proposals": [{"root_cause": "tool", "diff_preview": "+ new code"}],
        })
        result = p.execute(step, executor_fn=lambda s: "ok")
        assert result.proposals[0].diff_preview == "+ new code"


# ── 授权 ────────────────────────────────────────────────────────


class TestProposerPermissions:
    def test_role_registered(self):
        assert "proposer" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["proposer"]
        assert set(perm.allowed_tools) == {
            "memory_read",
            "config_read",
            "audit_log",
            "metric_collect",
            "diff_preview",
            "file_read",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["proposer"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """proposer 无写文件/提交/回滚/网络工具（只提议不改）。"""
        perm = DEFAULT_ROLE_PERMS["proposer"]
        forbidden = {"file_write", "git_commit", "rollback", "web_fetch", "web_search", "sandbox_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestProposerConstants:
    def test_root_cause_types_complete(self):
        assert "prompt" in ROOT_CAUSE_TYPES
        assert "tool" in ROOT_CAUSE_TYPES
        assert "process" in ROOT_CAUSE_TYPES
        assert "context" in ROOT_CAUSE_TYPES
        assert "data" in ROOT_CAUSE_TYPES

    def test_root_cause_types_count(self):
        assert len(ROOT_CAUSE_TYPES) == 5
