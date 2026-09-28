"""DocWorker 文档执行者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：执行/素材不足/拒绝/权限不足/来源标注/来源不可考
- 边界：空步骤
- 授权：doc_worker 角色权限矩阵（file_write 高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.doc_worker import (
    DOC_ACTIONS,
    DocWorker,
    Source,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_worker() -> DocWorker:
    return DocWorker(correlation_id="c-test")


def _make_step(
    action: str = "file_write",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestDocWorkerFunctional:
    def test_execute_success(self):
        """执行成功 → done + document + sources。"""
        w = _make_worker()
        step = _make_step("file_write", {
            "materials": ["素材1"],
            "document": "# 文档\n内容",
            "sources": [
                {"fact": "Python 3.14 发布", "source": "python.org", "verified": True},
                {"fact": "某数据", "source": "unknown", "verified": False},
            ],
        })
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.document == "# 文档\n内容"
        assert len(result.sources) == 2
        assert result.sources[0].fact == "Python 3.14 发布"
        assert result.sources[0].source == "python.org"
        assert result.sources[0].verified is True
        assert result.sources[1].verified is False

    def test_execute_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        w = _make_worker()
        step = _make_step("file_write", {"materials": ["素材1"]})
        result = w.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_doc_action(self):
        """非文档类 → rejected。"""
        w = _make_worker()
        step = _make_step("web_search")
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出文档范围" in result.error

    def test_insufficient_materials(self):
        """素材不足 → pending（请求补充而非编造）。"""
        w = _make_worker()
        step = _make_step("file_write", {"materials": []})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "pending"
        assert len(result.pending_supplements) > 0
        assert "素材不足" in result.pending_supplements[0]

    def test_permission_denied(self):
        """权限不足 → pending_approval。"""
        w = _make_worker()
        step = _make_step("file_write", {"materials": ["素材1"]})

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = w.execute(step, executor_fn=fn)
        assert result.status == "pending_approval"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        w = _make_worker()
        step = _make_step("file_write", {"materials": ["素材1"]})

        def fn(s: Step) -> str:
            raise RuntimeError("写入失败")

        result = w.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_sources_parsed(self):
        """来源标注清单正确解析。"""
        w = _make_worker()
        step = _make_step("file_write", {
            "materials": ["素材"],
            "sources": [
                {"fact": "事实1", "source": "url1", "verified": True},
                {"fact": "事实2", "source": "url2", "verified": False},
            ],
        })
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert len(result.sources) == 2
        assert isinstance(result.sources[0], Source)

    def test_unverified_source_marked(self):
        """来源不可考 → 标存疑（verified=False）。"""
        w = _make_worker()
        step = _make_step("file_write", {
            "materials": ["素材"],
            "sources": [
                {"fact": "事实", "source": "不可考来源", "verified": False},
            ],
        })
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.sources[0].verified is False

    def test_pending_supplements_extracted(self):
        """待补充内容从 inputs 提取。"""
        w = _make_worker()
        step = _make_step("file_write", {
            "materials": ["素材"],
            "pending_supplements": ["版本号待确认", "作者待补充"],
        })
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert len(result.pending_supplements) == 2
        assert "版本号待确认" in result.pending_supplements

    def test_web_fetch_accepted(self):
        """web_fetch 是文档类 action。"""
        w = _make_worker()
        step = _make_step("web_fetch", {"url": "https://x.com", "materials": ["素材"]})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_citation_check_accepted(self):
        """citation_check 是文档类 action。"""
        w = _make_worker()
        step = _make_step("citation_check", {"sources": ["url1"], "materials": ["素材"]})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"


# ── 边界 ────────────────────────────────────────────────────────


class TestDocWorkerEdge:
    def test_empty_sources(self):
        """无来源 → 空清单。"""
        w = _make_worker()
        step = _make_step("file_write", {"materials": ["素材"]})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.sources == []

    def test_materials_with_fn_no_materials_key(self):
        """inputs 无 materials 键但有 fn → pending（素材不足）。"""
        w = _make_worker()
        step = _make_step("file_write", {})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "pending"


# ── 授权 ────────────────────────────────────────────────────────


class TestDocWorkerPermissions:
    def test_role_registered(self):
        assert "doc_worker" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["doc_worker"]
        assert set(perm.allowed_tools) == {
            "file_read",
            "file_write",
            "web_fetch",
            "citation_check",
            "memory_read",
            "audit_log",
        }

    def test_file_write_is_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["doc_worker"]
        assert "file_write" in perm.high_risk_tools

    def test_no_unauthorized_tools(self):
        """doc_worker 无代码/提交/回滚工具。"""
        perm = DEFAULT_ROLE_PERMS["doc_worker"]
        forbidden = {"code_search", "git_commit", "rollback", "sandbox_run", "test_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestDocWorkerConstants:
    def test_doc_actions_nonempty(self):
        assert len(DOC_ACTIONS) > 0
        assert "file_write" in DOC_ACTIONS
        assert "web_fetch" in DOC_ACTIONS

    def test_doc_actions_excludes_non_doc(self):
        """DOC_ACTIONS 不含非文档类 action。"""
        assert "web_search" not in DOC_ACTIONS
        assert "code_search" not in DOC_ACTIONS
        assert "data_query" not in DOC_ACTIONS
