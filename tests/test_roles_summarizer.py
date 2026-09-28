"""Summarizer 汇报员角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：汇总/五段式/未完成项/缺失项/拒绝/权限不足
- 边界：空步骤
- 授权：summarizer 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.summarizer import (
    REPORT_SECTIONS,
    SUMMARIZE_ACTIONS,
    Summarizer,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_summarizer() -> Summarizer:
    return Summarizer(correlation_id="c-test")


def _make_step(
    action: str = "summarize",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestSummarizerFunctional:
    def test_summarize_success(self):
        """汇总成功 → done + 五段式。"""
        s = _make_summarizer()
        step = _make_step("summarize", {
            "conclusions": ["结论1", "结论2"],
            "evidence": ["依据1"],
            "sources": ["url1"],
            "pending_items": [],
            "next_steps": ["下一步1"],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.status == "done"
        assert len(result.sections) == 5  # 五段式
        assert result.conclusions == ["结论1", "结论2"]
        assert result.evidence == ["依据1"]
        assert result.sources == ["url1"]
        assert result.next_steps == ["下一步1"]

    def test_report_action_accepted(self):
        """report 是汇报类 action。"""
        s = _make_summarizer()
        step = _make_step("report", {
            "conclusions": ["结论"],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.status == "done"

    def test_pending_items_listed(self):
        """存在未完成项 → 如实列出。"""
        s = _make_summarizer()
        step = _make_step("summarize", {
            "conclusions": ["结论"],
            "pending_items": ["未完成1", "未完成2"],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.has_pending is True
        assert result.pending_items == ["未完成1", "未完成2"]

    def test_missing_items_marked(self):
        """素材缺失 → 标注缺失项，不补编。"""
        s = _make_summarizer()
        step = _make_step("summarize", {
            "conclusions": ["结论"],
            "missing_items": ["缺失数据1"],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.has_pending is True
        assert result.missing_items == ["缺失数据1"]

    def test_no_pending(self):
        """无未完成项 → has_pending=False。"""
        s = _make_summarizer()
        step = _make_step("summarize", {
            "conclusions": ["结论"],
            "pending_items": [],
            "missing_items": [],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.has_pending is False

    def test_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        s = _make_summarizer()
        step = _make_step("summarize")
        result = s.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_summarize_action(self):
        """非汇报类 → rejected。"""
        s = _make_summarizer()
        step = _make_step("file_write")
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.status == "rejected"
        assert "超出汇报范围" in result.error

    def test_permission_denied(self):
        """权限不足 → failed。"""
        s = _make_summarizer()
        step = _make_step("summarize")

        def fn(st: Step) -> str:
            raise PermissionError("需要审批")

        result = s.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        s = _make_summarizer()
        step = _make_step("summarize")

        def fn(st: Step) -> str:
            raise RuntimeError("汇总错误")

        result = s.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_five_sections_built(self):
        """五段式章节构建正确。"""
        s = _make_summarizer()
        step = _make_step("summarize", {
            "conclusions": ["结论"],
            "evidence": ["依据"],
            "sources": ["来源"],
            "pending_items": ["未完成"],
            "next_steps": ["下一步"],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        titles = [sec.title for sec in result.sections]
        assert titles == ["结论", "依据", "来源", "未完成项", "下一步"]

    def test_empty_pending_shows_none(self):
        """无未完成项 → 章节内容为"无"。"""
        s = _make_summarizer()
        step = _make_step("summarize", {
            "conclusions": ["结论"],
            "pending_items": [],
            "next_steps": [],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        pending_section = next(sec for sec in result.sections if sec.title == "未完成项")
        assert pending_section.content == "无"


# ── 边界 ────────────────────────────────────────────────────────


class TestSummarizerEdge:
    def test_empty_inputs(self):
        """空输入 → 空报告。"""
        s = _make_summarizer()
        step = _make_step("summarize", {})
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.status == "done"
        assert result.conclusions == []
        assert result.has_pending is False


# ── 授权 ────────────────────────────────────────────────────────


class TestSummarizerPermissions:
    def test_role_registered(self):
        assert "summarizer" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["summarizer"]
        assert set(perm.allowed_tools) == {
            "file_read",
            "memory_read",
            "memory_write",
            "audit_log",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["summarizer"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """summarizer 无写文件/提交/回滚/网络工具。"""
        perm = DEFAULT_ROLE_PERMS["summarizer"]
        forbidden = {"file_write", "git_commit", "rollback", "web_fetch", "web_search", "sandbox_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestSummarizerConstants:
    def test_summarize_actions_nonempty(self):
        assert len(SUMMARIZE_ACTIONS) > 0
        assert "summarize" in SUMMARIZE_ACTIONS
        assert "report" in SUMMARIZE_ACTIONS

    def test_summarize_actions_excludes_non_summarize(self):
        assert "file_write" not in SUMMARIZE_ACTIONS
        assert "web_search" not in SUMMARIZE_ACTIONS
        assert "data_query" not in SUMMARIZE_ACTIONS

    def test_report_sections_five(self):
        """报告固定五段式。"""
        assert len(REPORT_SECTIONS) == 5
        assert "结论" in REPORT_SECTIONS
        assert "依据" in REPORT_SECTIONS
        assert "来源" in REPORT_SECTIONS
        assert "未完成项" in REPORT_SECTIONS
        assert "下一步" in REPORT_SECTIONS
