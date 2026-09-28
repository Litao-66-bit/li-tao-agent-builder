"""FactChecker 事实核验者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：核验通过/存疑/证伪/拒绝/权限不足/来源失效重试
- 边界：空步骤
- 授权：fact_checker 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.fact_checker import (
    MAX_RETRIES,
    VERIFY_ACTIONS,
    VERIFY_STATUSES,
    FactChecker,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_checker() -> FactChecker:
    return FactChecker(correlation_id="c-test")


def _make_step(
    action: str = "citation_check",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestFactCheckerFunctional:
    def test_all_passed(self):
        """全部通过 → done + all_passed。"""
        c = _make_checker()
        step = _make_step("citation_check", {
            "verifications": [
                {"claim": "Python 3.14", "source": "python.org", "status": "passed", "evidence": "官方发布"},
                {"claim": "新功能 X", "source": "docs.python.org", "status": "passed"},
            ],
        })
        result = c.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.passed == 2
        assert result.suspicious == 0
        assert result.falsified == 0
        assert result.all_passed is True

    def test_with_suspicious(self):
        """有存疑 → done + all_passed=False。"""
        c = _make_checker()
        step = _make_step("citation_check", {
            "verifications": [
                {"claim": "事实1", "source": "url1", "status": "passed"},
                {"claim": "事实2", "source": "url2", "status": "suspicious", "evidence": "来源不可考"},
            ],
        })
        result = c.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.passed == 1
        assert result.suspicious == 1
        assert result.all_passed is False

    def test_with_falsified(self):
        """有证伪 → done + falsified=1。"""
        c = _make_checker()
        step = _make_step("citation_check", {
            "verifications": [
                {"claim": "错误声明", "source": "url", "status": "falsified", "evidence": "与官方矛盾"},
            ],
        })
        result = c.execute(step, executor_fn=lambda s: "ok")
        assert result.falsified == 1
        assert result.all_passed is False

    def test_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        c = _make_checker()
        step = _make_step("citation_check")
        result = c.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_verify_action(self):
        """非核验类 → rejected。"""
        c = _make_checker()
        step = _make_step("file_write")
        result = c.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出核验范围" in result.error

    def test_permission_denied(self):
        """权限不足 → failed。"""
        c = _make_checker()
        step = _make_step("citation_check")

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = c.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_source_failure_retry(self):
        """来源失效 → 重试 1 次 → 成功。"""
        c = _make_checker()
        step = _make_step("citation_check", {
            "verifications": [{"claim": "事实", "source": "url", "status": "passed"}],
        })
        call_count = 0

        def fn(s: Step) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("链接失效")
            return "ok"

        result = c.execute(step, executor_fn=fn)
        assert call_count == 2
        assert result.status == "done"

    def test_source_failure_max_retries(self):
        """来源失效重试仍失败 → 标存疑。"""
        c = _make_checker()
        step = _make_step("citation_check")

        def fn(s: Step) -> str:
            raise OSError("链接失效")

        result = c.execute(step, executor_fn=fn)
        assert result.status == "done"
        assert result.suspicious >= 1
        assert "来源链接失效" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        c = _make_checker()
        step = _make_step("citation_check")

        def fn(s: Step) -> str:
            raise RuntimeError("核验框架错误")

        result = c.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_web_fetch_accepted(self):
        """web_fetch 是核验类 action。"""
        c = _make_checker()
        step = _make_step("web_fetch", {
            "url": "https://x.com",
            "verifications": [{"claim": "c", "source": "u", "status": "passed"}],
        })
        result = c.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_invalid_status_becomes_suspicious(self):
        """非法状态 → 归入存疑。"""
        c = _make_checker()
        step = _make_step("citation_check", {
            "verifications": [{"claim": "c", "source": "u", "status": "invalid"}],
        })
        result = c.execute(step, executor_fn=lambda s: "ok")
        assert result.items[0].status == "suspicious"
        assert result.suspicious == 1


# ── 边界 ────────────────────────────────────────────────────────


class TestFactCheckerEdge:
    def test_empty_verifications(self):
        """无核验项 → 空清单。"""
        c = _make_checker()
        step = _make_step("citation_check", {})
        result = c.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.items == []
        assert result.all_passed is True


# ── 授权 ────────────────────────────────────────────────────────


class TestFactCheckerPermissions:
    def test_role_registered(self):
        assert "fact_checker" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["fact_checker"]
        assert set(perm.allowed_tools) == {
            "citation_check",
            "web_fetch",
            "file_read",
            "code_search",
            "memory_read",
            "audit_log",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["fact_checker"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """fact_checker 无写文件/提交/回滚工具。"""
        perm = DEFAULT_ROLE_PERMS["fact_checker"]
        forbidden = {"file_write", "git_commit", "rollback", "sandbox_run", "test_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestFactCheckerConstants:
    def test_verify_actions_nonempty(self):
        assert len(VERIFY_ACTIONS) > 0
        assert "citation_check" in VERIFY_ACTIONS
        assert "web_fetch" in VERIFY_ACTIONS

    def test_verify_actions_excludes_non_verify(self):
        assert "file_write" not in VERIFY_ACTIONS
        assert "web_search" not in VERIFY_ACTIONS
        assert "data_query" not in VERIFY_ACTIONS

    def test_verify_statuses(self):
        assert "passed" in VERIFY_STATUSES
        assert "suspicious" in VERIFY_STATUSES
        assert "falsified" in VERIFY_STATUSES

    def test_max_retries_positive(self):
        assert MAX_RETRIES >= 0
