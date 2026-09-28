"""TestRunner 测试执行者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：执行/测试通过/测试失败/拒绝/权限不足/环境异常重试
- 边界：空步骤
- 授权：test_runner 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.test_runner import (
    MAX_RETRIES,
    TEST_ACTIONS,
    TestRunner,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_runner() -> TestRunner:
    return TestRunner(correlation_id="c-test")


def _make_step(
    action: str = "test_run",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestRunnerFunctional:
    def test_execute_all_passed(self):
        """全部通过 → done。"""
        r = _make_runner()
        step = _make_step("test_run", {
            "test_cases": [
                {"name": "test_a", "status": "passed", "log": "ok"},
                {"name": "test_b", "status": "passed", "log": "ok"},
            ],
            "repro_log": "pytest -v",
        })
        result = r.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.passed == 2
        assert result.failed == 0
        assert result.repro_log == "pytest -v"

    def test_execute_with_failures(self):
        """有失败 → failed。"""
        r = _make_runner()
        step = _make_step("test_run", {
            "test_cases": [
                {"name": "test_a", "status": "passed"},
                {"name": "test_b", "status": "failed", "log": "AssertionError"},
            ],
        })
        result = r.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "failed"
        assert result.passed == 1
        assert result.failed == 1

    def test_execute_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        r = _make_runner()
        step = _make_step("test_run")
        result = r.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_test_action(self):
        """非验证类 → rejected。"""
        r = _make_runner()
        step = _make_step("web_search")
        result = r.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出验证范围" in result.error_msg

    def test_permission_denied(self):
        """权限不足 → env_failure（不重试）。"""
        r = _make_runner()
        step = _make_step("test_run")

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = r.execute(step, executor_fn=fn)
        assert result.status == "env_failure"
        assert "权限不足" in result.error_msg

    def test_env_exception_retry(self):
        """环境异常 → 重试 1 次。"""
        r = _make_runner()
        step = _make_step("test_run")
        call_count = 0

        def fn(s: Step) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("沙箱不可用")
            return "ok"

        result = r.execute(step, executor_fn=fn)
        assert call_count == 2  # 初始 + 重试
        assert result.status == "done"

    def test_env_exception_max_retries(self):
        """环境异常重试仍失败 → env_failure。"""
        r = _make_runner()
        step = _make_step("test_run")

        def fn(s: Step) -> str:
            raise OSError("沙箱不可用")

        result = r.execute(step, executor_fn=fn)
        assert result.status == "env_failure"
        assert "环境异常" in result.error_msg

    def test_execution_failure(self):
        """执行失败 → failed。"""
        r = _make_runner()
        step = _make_step("test_run")

        def fn(s: Step) -> str:
            raise RuntimeError("测试框架错误")

        result = r.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error_msg

    def test_sandbox_run_accepted(self):
        """sandbox_run 是验证类 action。"""
        r = _make_runner()
        step = _make_step("sandbox_run", {
            "test_cases": [{"name": "t", "status": "passed"}],
        })
        result = r.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_error_cases_counted(self):
        """error 状态计入 error 计数。"""
        r = _make_runner()
        step = _make_step("test_run", {
            "test_cases": [
                {"name": "t1", "status": "passed"},
                {"name": "t2", "status": "error"},
                {"name": "t3", "status": "failed"},
            ],
        })
        result = r.execute(step, executor_fn=lambda s: "ok")
        assert result.passed == 1
        assert result.failed == 1
        assert result.error == 1
        assert result.status == "failed"

    def test_invalid_status_becomes_skipped(self):
        """非法状态 → skipped。"""
        r = _make_runner()
        step = _make_step("test_run", {
            "test_cases": [{"name": "t", "status": "invalid"}],
        })
        result = r.execute(step, executor_fn=lambda s: "ok")
        assert result.cases[0].status == "skipped"


# ── 边界 ────────────────────────────────────────────────────────


class TestRunnerEdge:
    def test_empty_cases(self):
        """无测试用例 → 空清单。"""
        r = _make_runner()
        step = _make_step("test_run", {})
        result = r.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.cases == []
        assert result.passed == 0

    def test_custom_max_retries(self):
        """自定义最大重试次数。"""
        r = TestRunner(correlation_id="c-test", max_retries=0)
        step = _make_step("test_run")
        call_count = 0

        def fn(s: Step) -> str:
            nonlocal call_count
            call_count += 1
            raise OSError("fails")

        result = r.execute(step, executor_fn=fn)
        assert call_count == 1  # max_retries=0 → 只执行 1 次
        assert result.status == "env_failure"


# ── 授权 ────────────────────────────────────────────────────────


class TestRunnerPermissions:
    def test_role_registered(self):
        assert "test_runner" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["test_runner"]
        assert set(perm.allowed_tools) == {
            "test_run",
            "sandbox_run",
            "file_read",
            "code_search",
            "memory_read",
            "audit_log",
            "citation_check",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["test_runner"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """test_runner 无写文件/提交/回滚工具（不修改被测代码）。"""
        perm = DEFAULT_ROLE_PERMS["test_runner"]
        forbidden = {"file_write", "git_commit", "rollback", "web_fetch", "web_search"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestRunnerConstants:
    def test_test_actions_nonempty(self):
        assert len(TEST_ACTIONS) > 0
        assert "test_run" in TEST_ACTIONS
        assert "sandbox_run" in TEST_ACTIONS

    def test_test_actions_excludes_non_test(self):
        assert "web_search" not in TEST_ACTIONS
        assert "file_write" not in TEST_ACTIONS
        assert "data_query" not in TEST_ACTIONS

    def test_max_retries_positive(self):
        assert MAX_RETRIES >= 0
