"""CodeWorker 代码执行者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：执行/自测/重试/拒绝/新依赖/权限不足
- 边界：空步骤
- 授权：code_worker 角色权限矩阵（file_write 高风险）
"""

from __future__ import annotations

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Step
from agent_builder.roles.code_worker import (
    CODE_ACTIONS,
    MAX_SELF_CHECK_RETRIES,
    CodeWorker,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_worker() -> CodeWorker:
    return CodeWorker(correlation_id="c-test")


def _make_step(
    action: str = "file_write",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestCodeWorkerFunctional:
    def test_execute_success(self):
        """执行成功 → done + self_check_passed。"""
        w = _make_worker()
        step = _make_step("file_write", {"path": "/x.py", "content": "print(1)"})

        def fn(s: Step) -> str:
            return "ok"

        result = w.execute(step, executor_fn=fn)
        assert result.status == "done"
        assert result.self_check_passed is True
        assert "/x.py" in result.files_changed

    def test_execute_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        w = _make_worker()
        step = _make_step("file_write")
        result = w.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_code_action(self):
        """非代码类 → rejected。"""
        w = _make_worker()
        step = _make_step("web_search")
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出代码范围" in result.error

    def test_file_edit_能通过角色层派发(self):
        """回归（实测用户任务 5ecf069d）：把 `file_edit` 接进权限矩阵与派发表**还不够** ——
        角色实现层还有一份 `CODE_ACTIONS` 范围清单。漏了它，模型第 6 步主动用 `file_edit`
        改实现时被判「任务超出代码范围」→ 整轮 0 产物。三层都要接才算真的可用。"""
        w = _make_worker()
        step = _make_step(
            "file_edit",
            {"path": "/x.py", "old_string": "a = 1", "new_string": "a = 2"},
        )

        result = w.execute(step, executor_fn=lambda s: "edited /x.py (第 1 行，替换 1 处)")

        assert result.status == "done"
        assert result.error is None
        assert "/x.py" in result.files_changed

    def test_pending_dependencies(self):
        """有新依赖 → pending_approval。"""
        w = _make_worker()
        step = _make_step("file_write", {"dependencies": ["numpy", "pandas"]})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "pending_approval"
        assert "numpy" in result.pending_dependencies
        assert "pandas" in result.pending_dependencies

    def test_permission_denied(self):
        """权限不足 → pending_approval（不重试）。"""
        w = _make_worker()
        step = _make_step("file_write")

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = w.execute(step, executor_fn=fn)
        assert result.status == "pending_approval"
        assert "权限不足" in result.error

    def test_self_check_retry_then_success(self):
        """自测失败 → 自查修复 → 成功。"""
        w = _make_worker()
        step = _make_step("file_write")
        call_count = 0

        def fn(s: Step) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")
            return "ok"

        result = w.execute(step, executor_fn=fn)
        assert call_count == 2
        assert result.status == "done"
        assert result.self_check_passed is True

    def test_self_check_max_retries_then_fail(self):
        """自测 2 次仍失败 → failed（如实上报）。"""
        w = _make_worker()
        step = _make_step("file_write")
        call_count = 0

        def fn(s: Step) -> str:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("always fails")

        result = w.execute(step, executor_fn=fn)
        assert call_count == MAX_SELF_CHECK_RETRIES + 1  # 初始 + 2 次自查
        assert result.status == "failed"
        assert result.self_check_passed is False
        assert "RuntimeError" in result.error

    def test_non_retryable_agent_error_no_self_check_retry(self):
        """不可重试的契约异常（如 file_write 未授权覆盖）→ 不自查重试，直接上报。"""
        w = _make_worker()
        step = _make_step("file_write")
        call_count = 0

        def fn(s: Step) -> str:
            nonlocal call_count
            call_count += 1
            raise AgentError(
                "E_VALIDATION",
                "file_write: 文件已存在且未授权覆盖",
                "tool.file_write",
                "c-test",
                retryable=False,
            )

        result = w.execute(step, executor_fn=fn)
        assert call_count == 1  # 未做内部自查重试
        assert result.status == "failed"
        assert result.retryable is False
        assert "E_VALIDATION" in result.error

    def test_参数类校验失败也不自查重试(self):
        """``E_VALIDATION`` 是**确定性**失败，重试只会白跑。

        错误码表里 E_VALIDATION 默认 ``retryable=True``，于是同一动作被重试 3 次 ——
        实测 ``file_read`` 传错路径就在 gatekeeper 留下 3 条一模一样的审计行。
        """
        w = _make_worker()
        step = _make_step("file_read")
        call_count = 0

        def fn(s: Step) -> str:
            nonlocal call_count
            call_count += 1
            raise AgentError(
                "E_VALIDATION",
                "file_read: 文件不存在: research_agent.py",
                "tool.file_read",
                "c-test",
            )

        result = w.execute(step, executor_fn=fn)

        assert call_count == 1  # 未做内部自查重试
        assert result.status == "failed"
        assert "E_VALIDATION" in result.error

    def test_files_changed_from_files_list(self):
        """files_changed 从 inputs['files'] 提取。"""
        w = _make_worker()
        step = _make_step("file_write", {"files": ["/a.py", "/b.py"]})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.files_changed == ["/a.py", "/b.py"]

    def test_files_changed_from_path(self):
        """files_changed 从 inputs['path'] 提取。"""
        w = _make_worker()
        step = _make_step("file_write", {"path": "/x.py"})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.files_changed == ["/x.py"]

    def test_change_desc_extracted(self):
        """change_desc 从 inputs 提取。"""
        w = _make_worker()
        step = _make_step("file_write", {"change_desc": "新增函数 foo"})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.change_desc == "新增函数 foo"

    def test_run_instructions_extracted(self):
        """run_instructions 从 inputs 提取。"""
        w = _make_worker()
        step = _make_step("file_write", {"run_instructions": "python /x.py"})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.run_instructions == "python /x.py"

    def test_code_search_accepted(self):
        """code_search 是代码类 action。"""
        w = _make_worker()
        step = _make_step("code_search", {"pattern": "foo"})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_file_read_accepted(self):
        """file_read 是代码类 action。"""
        w = _make_worker()
        step = _make_step("file_read", {"path": "/x.py"})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_只读动作不报文件变更(self):
        """只读动作（列出 / 读取 / 搜索）**没有改动任何文件** → files_changed 必须为空。

        回归：此前一律从 ``inputs['path']`` 回填，于是「列出 tests」被当成产出了 ``tests``，
        编排层的 ``artifacts_of()`` 信以为真 → 前端给它渲染「查看产物」，
        点开必然报「路径不是文件: tests」。
        """
        w = _make_worker()
        for action, inputs in (
            ("file_list", {"path": "tests"}),
            ("file_read", {"path": "tests"}),
            ("code_search", {"path": "tests", "pattern": "def test_"}),
        ):
            result = w.execute(_make_step(action, inputs), executor_fn=lambda s: "ok")
            assert result.status == "done", action
            assert result.files_changed == [], action


# ── 边界 ────────────────────────────────────────────────────────


class TestCodeWorkerEdge:
    def test_empty_inputs(self):
        """inputs 为空 → 仍可执行（无文件变更）。"""
        w = _make_worker()
        step = _make_step("file_write", {})
        result = w.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.files_changed == []

    def test_custom_max_retries(self):
        """自定义最大自查次数。"""
        w = CodeWorker(correlation_id="c-test", max_self_check_retries=0)
        step = _make_step("file_write")
        call_count = 0

        def fn(s: Step) -> str:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("fails")

        result = w.execute(step, executor_fn=fn)
        assert call_count == 1  # max_retries=0 → 只执行 1 次
        assert result.status == "failed"


# ── 授权 ────────────────────────────────────────────────────────


class TestCodeWorkerPermissions:
    def test_role_registered(self):
        assert "code_worker" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["code_worker"]
        assert set(perm.allowed_tools) == {
            "file_read",
            "file_list",
            "code_search",
            "file_write",
            "file_edit",
            "sandbox_run",
            "memory_read",
            "audit_log",
        }

    def test_file_write_is_high_risk(self):
        """file_write / file_edit 是高风险工具（需审批）。"""
        perm = DEFAULT_ROLE_PERMS["code_worker"]
        assert "file_write" in perm.high_risk_tools
        assert "file_edit" in perm.high_risk_tools

    def test_no_unauthorized_tools(self):
        """code_worker 无网络/提交/回滚工具。"""
        perm = DEFAULT_ROLE_PERMS["code_worker"]
        forbidden = {"web_fetch", "git_commit", "rollback", "test_run", "data_query"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestCodeWorkerConstants:
    def test_max_self_check_retries_positive(self):
        assert MAX_SELF_CHECK_RETRIES > 0

    def test_code_actions_nonempty(self):
        assert len(CODE_ACTIONS) > 0
        assert "file_write" in CODE_ACTIONS
        assert "file_edit" in CODE_ACTIONS
        assert "code_search" in CODE_ACTIONS

    def test_code_actions_excludes_non_code(self):
        """CODE_ACTIONS 不含非代码类 action。"""
        assert "web_search" not in CODE_ACTIONS
        assert "web_fetch" not in CODE_ACTIONS
        assert "data_query" not in CODE_ACTIONS
