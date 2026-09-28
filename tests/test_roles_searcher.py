"""Searcher 检索执行者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：执行/去重/排序/无来源标未查证/多轮/拒绝/权限不足
- 边界：空步骤
- 授权：searcher 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.searcher import (
    MAX_ROUNDS,
    MIN_RESULTS,
    SEARCH_ACTIONS,
    Searcher,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_searcher() -> Searcher:
    return Searcher(correlation_id="c-test")


def _make_step(
    action: str = "web_search",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestSearcherFunctional:
    def test_execute_success(self):
        """执行成功 → done + items。"""
        s = _make_searcher()
        step = _make_step("web_search", {
            "query": "Python 3.14",
            "results": [
                {"claim": "Python 3.14 发布", "source_url": "python.org", "confidence": 0.9, "verified": True},
                {"claim": "新功能", "source_url": "docs.python.org", "confidence": 0.7, "verified": False},
            ],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.status == "done"
        assert len(result.items) == 2
        assert result.items[0].confidence >= result.items[1].confidence  # 降序
        assert result.keywords_used == ["Python 3.14"]

    def test_execute_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        s = _make_searcher()
        step = _make_step("web_search")
        result = s.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_search_action(self):
        """非检索类 → rejected。"""
        s = _make_searcher()
        step = _make_step("file_write")
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.status == "rejected"
        assert "超出检索范围" in result.error

    def test_dedup(self):
        """去重（按 source_url + claim）。"""
        s = _make_searcher()
        step = _make_step("web_search", {
            "results": [
                {"claim": "相同声明", "source_url": "url1", "confidence": 0.9},
                {"claim": "相同声明", "source_url": "url1", "confidence": 0.8},
                {"claim": "不同声明", "source_url": "url2", "confidence": 0.7},
            ],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert len(result.items) == 2  # 去重后 2 条

    def test_sort_by_confidence(self):
        """按置信度降序排序。"""
        s = _make_searcher()
        step = _make_step("web_search", {
            "results": [
                {"claim": "低置信", "source_url": "url1", "confidence": 0.3},
                {"claim": "高置信", "source_url": "url2", "confidence": 0.9},
                {"claim": "中置信", "source_url": "url3", "confidence": 0.6},
            ],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.items[0].confidence == 0.9
        assert result.items[1].confidence == 0.6
        assert result.items[2].confidence == 0.3

    def test_no_source_marked_unverified(self):
        """无来源 → 标"未查证"（verified=False）。"""
        s = _make_searcher()
        step = _make_step("web_search", {
            "results": [
                {"claim": "无来源声明", "source_url": "", "confidence": 0.5, "verified": True},
            ],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.items[0].verified is False

    def test_permission_denied(self):
        """工具被门卫拒绝 → failed。"""
        s = _make_searcher()
        step = _make_step("web_search")

        def fn(st: Step) -> str:
            raise PermissionError("URL 不在白名单")

        result = s.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "门卫拒绝" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        s = _make_searcher()
        step = _make_step("web_search")

        def fn(st: Step) -> str:
            raise RuntimeError("网络超时")

        result = s.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_multiple_rounds(self):
        """结果不足 → 多轮搜索。"""
        s = _make_searcher()
        step = _make_step("web_search", {
            "results": [
                {"claim": "仅1条", "source_url": "url1", "confidence": 0.5},
            ],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.rounds >= 1
        # 结果不足（< MIN_RESULTS）→ 会多轮
        assert result.rounds == MAX_ROUNDS

    def test_sufficient_results_one_round(self):
        """结果充足 → 只搜 1 轮。"""
        s = _make_searcher()
        results = [{"claim": f"c{i}", "source_url": f"u{i}", "confidence": 0.5} for i in range(MIN_RESULTS + 1)]
        step = _make_step("web_search", {"results": results})
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.rounds == 1

    def test_web_fetch_accepted(self):
        """web_fetch 是检索类 action。"""
        s = _make_searcher()
        step = _make_step("web_fetch", {
            "url": "https://x.com",
            "results": [{"claim": "c", "source_url": "u", "confidence": 0.5}],
        })
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.status == "done"


# ── 边界 ────────────────────────────────────────────────────────


class TestSearcherEdge:
    def test_empty_results(self):
        """无结果 → 空清单。"""
        s = _make_searcher()
        step = _make_step("web_search", {})
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.status == "done"
        assert result.items == []

    def test_empty_query(self):
        """空查询 → keywords_used 为空。"""
        s = _make_searcher()
        step = _make_step("web_search", {"results": []})
        result = s.execute(step, executor_fn=lambda st: "ok")
        assert result.keywords_used == []


# ── 授权 ────────────────────────────────────────────────────────


class TestSearcherPermissions:
    def test_role_registered(self):
        assert "searcher" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["searcher"]
        assert set(perm.allowed_tools) == {
            "web_search",
            "web_fetch",
            "citation_check",
            "memory_read",
            "audit_log",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["searcher"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """searcher 无写文件/提交/回滚工具。"""
        perm = DEFAULT_ROLE_PERMS["searcher"]
        forbidden = {"file_write", "git_commit", "rollback", "sandbox_run", "test_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestSearcherConstants:
    def test_search_actions_nonempty(self):
        assert len(SEARCH_ACTIONS) > 0
        assert "web_search" in SEARCH_ACTIONS
        assert "web_fetch" in SEARCH_ACTIONS

    def test_search_actions_excludes_non_search(self):
        assert "file_write" not in SEARCH_ACTIONS
        assert "code_search" not in SEARCH_ACTIONS
        assert "data_query" not in SEARCH_ACTIONS

    def test_max_rounds_positive(self):
        assert MAX_ROUNDS > 0

    def test_min_results_positive(self):
        assert MIN_RESULTS > 0
