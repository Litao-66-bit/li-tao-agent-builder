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
    UNVERIFIABLE_MSG,
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
            return "1 passed in 0.01s"

        result = r.execute(step, executor_fn=fn)
        assert call_count == 2  # 初始 + 重试
        assert result.status == "done"
        assert result.passed == 1

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
        """既没有真实输出计数、也没有显式用例 → 无法核对（不默认 done）。"""
        r = _make_runner()
        step = _make_step("test_run", {})
        result = r.execute(step, executor_fn=lambda s: "")
        assert result.status == "env_failure"
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


# ── 真实输出为准（实机 bug：test_run 永远报「测试通过 0 项」） ────


class TestRunnerRealOutput:
    """计数必须来自工具真实输出；拿不到就判「无法核对」，绝不默认成功。"""

    def test_真实输出解析为计数(self):
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests"})
        result = r.execute(step, executor_fn=lambda s: "39 passed in 0.52s")
        assert result.status == "done"
        assert result.passed == 39
        assert result.failed == 0

    def test_真实输出有失败(self):
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests"})
        result = r.execute(
            step, executor_fn=lambda s: "1 failed, 2 passed in 0.10s"
        )
        assert result.status == "failed"
        assert result.passed == 2
        assert result.failed == 1

    def test_真实输出有错误(self):
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests"})
        result = r.execute(step, executor_fn=lambda s: "3 passed, 1 error in 0.10s")
        assert result.status == "failed"
        assert result.passed == 3
        assert result.error == 1

    def test_真实输出优先于自填用例(self):
        """口径：真实输出是唯一事实来源，不被 inputs 里自填的用例覆盖。"""
        r = _make_runner()
        step = _make_step("test_run", {
            "test_cases": [{"name": f"t{i}", "status": "passed"} for i in range(5)],
        })
        result = r.execute(step, executor_fn=lambda s: "2 passed in 0.01s")
        assert result.passed == 2
        assert result.cases == []

    def test_真实失败时带出用例与原因(self):
        """失败必须让模型看到「哪条用例、为什么」——否则调用链只剩一句通用串。"""
        out = (
            "tests/test_x.py::test_a PASSED [ 50%]\n"
            "tests/test_x.py::test_b FAILED [100%]\n"
            "========================= short test summary info ==========================\n"
            "FAILED tests/test_x.py::test_b - ModuleNotFoundError: No module named 'foo'\n"
            "========================= 1 failed, 1 passed in 0.53s ======================\n"
        )
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests/test_x.py"})
        result = r.execute(step, executor_fn=lambda s: out)
        assert result.status == "failed"
        assert result.failed == 1
        assert "test_b" in result.error_msg
        assert "ModuleNotFoundError" in result.error_msg

    def test_集合期报错时带出真因(self):
        """汇总行不带原因（import 失败等）→ 取 pytest 的 "E   ..." 行，否则模型改不动。"""
        out = (
            "============================= ERRORS ==============================\n"
            "_ ERROR collecting tests/test_x.py _\n"
            "ImportError while importing test module 'tests/test_x.py'.\n"
            "E   ModuleNotFoundError: No module named 'research_paper_agent'\n"
            "========================= 1 error in 0.11s ========================\n"
        )
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests/test_x.py"})
        result = r.execute(step, executor_fn=lambda s: out)
        assert result.status == "failed"
        assert "ModuleNotFoundError" in result.error_msg

    def test_全部通过时没有错误信息(self):
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests"})
        result = r.execute(step, executor_fn=lambda s: "2 passed in 0.01s")
        assert result.status == "done"
        assert result.error_msg is None

    def test_失败行最多带出5条(self):
        """够定位即可，不把整段输出倒给模型。"""
        out = (
            "\n".join(
                f"FAILED tests/test_x.py::test_{i} - AssertionError" for i in range(9)
            )
            + "\n9 failed in 0.10s\n"
        )
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests/test_x.py"})
        result = r.execute(step, executor_fn=lambda s: out)
        assert result.failed == 9
        assert result.error_msg.count("→") == 5
        assert "test_8" not in result.error_msg

    def test_一组都没跑到判环境失败(self):
        """pytest 明确 no tests ran → 不能当成「测试通过」。"""
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests/nope.py"})
        result = r.execute(step, executor_fn=lambda s: "no tests ran in 0.01s")
        assert result.status == "env_failure"
        assert result.error_msg == UNVERIFIABLE_MSG

    def test_拿不到可核对结果判环境失败(self):
        """输出里没有任何计数、也没有显式用例 → 无法核对（绝不默认 done）。"""
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests"})
        result = r.execute(step, executor_fn=lambda s: "ERROR: 目录不存在")
        assert result.status == "env_failure"
        assert result.error_msg == UNVERIFIABLE_MSG

    def test_输出被截断时降级统计明细标记(self):
        """汇总行随截断丢失 → 退化为数 -v 明细行的 PASSED/FAILED/ERROR。"""
        truncated = (
            "tests/test_a.py::test_1 PASSED [ 50%]\n"
            "tests/test_b.py::test_2 PASSED [100%]\n"
            "…[已截断，原文 90000 字符]"
        )
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests"})
        result = r.execute(step, executor_fn=lambda s: truncated)
        assert result.status == "done"
        assert result.passed == 2

    def test_截断说明行不干扰汇总行扫描(self):
        """截断标记本身不含计数，不应让它抢走汇总行。"""
        out = "3 passed in 0.05s\n…[已截断，原文 60000 字符]"
        r = _make_runner()
        step = _make_step("test_run", {"target": "tests"})
        result = r.execute(step, executor_fn=lambda s: out)
        assert result.passed == 3


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
