"""test_run 工具测试：功能 / 边界 / 安全 / 成本（截断）。

subprocess.run 和 urllib.request.urlopen 全程 mock。
路径沙箱 + URL 安全校验通过真实门卫跑。
"""

from __future__ import annotations

import subprocess
import sys
import urllib.error
from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.test_run import MAX_OUTPUT_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["test_run"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, target, *, timeout=None, tool="test_run", audit_id="a-1"):
    args: dict = {"target": target}
    if timeout is not None:
        args["timeout"] = timeout
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


class _FakeResp:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.headers = {"Content-Type": content_type}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── 功能 ────────────────────────────────────────────────────────


class TestTestRunFunctional:
    def test_pytest_mode(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("def test_ok(): pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout="1 passed"),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", str(test_file)))
        assert "1 passed" in call.result
        assert call.status == "executed"

    def test_用当前解释器跑pytest(self, gatekeeper, tmp_path):
        """回归：必须用 ``sys.executable -m pytest``，不能依赖 PATH 里的裸 ``pytest``。

        本机依赖是 ``pip install --target .deps`` 装的 —— ``pytest`` 与 ``python``
        **都不在 PATH**，裸命令必然 ``WinError 2``（实测报「未安装或不在 PATH」）。
        钉住这条，避免有人再改回裸 ``pytest``。
        """
        test_file = tmp_path / "test_x.py"
        test_file.write_text("def test_ok(): pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout="1 passed"),
        ) as run:
            registry.execute(gatekeeper, _make_call("operator", str(test_file)))
        argv = run.call_args[0][0]
        assert argv[0] == sys.executable
        assert argv[1:3] == ["-m", "pytest"]
        assert argv[0] != "pytest"

    def test_doc_mode(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.test_run.urllib.request.urlopen",
            return_value=_FakeResp(b"hello doc"),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", "https://example.com/doc")
            )
        assert call.result == "hello doc"

    def test_doc_unicode(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.test_run.urllib.request.urlopen",
            return_value=_FakeResp("你好文档".encode()),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", "https://example.com/cn")
            )
        assert call.result == "你好文档"


# ── 边界 ────────────────────────────────────────────────────────


class TestTestRunEdge:
    def test_empty_target(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="test_run", args={"target": ""}
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        # 缺参数 → E_VALIDATION（不是 E_PERMISSION）。
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_timeout(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout="ok"),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", str(test_file), timeout=0)
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 异常 ────────────────────────────────────────────────────────


class TestTestRunErrors:
    def test_pytest_not_found(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            side_effect=FileNotFoundError("pytest not found"),
        ):
            call = _make_call("operator", str(test_file))
            with pytest.raises(AgentError) as exc_info:
                registry.execute(gatekeeper, call)
            assert exc_info.value.error_name == "E_TOOL"
            assert "pytest" in exc_info.value.info.message

    def test_pytest_timeout(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=5),
        ):
            call = _make_call("operator", str(test_file), timeout=5)
            with pytest.raises(AgentError) as exc_info:
                registry.execute(gatekeeper, call)
            assert exc_info.value.error_name == "E_TOOL"

    def test_doc_http_error(self, gatekeeper):
        url = "https://example.com/404"
        with patch(
            "agent_builder.tools.impl.test_run.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url, 404, "Not Found", {}, None),  # type: ignore[arg-type]
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", url))
        assert exc_info.value.error_name == "E_TOOL"
        assert "404" in exc_info.value.info.message


# ── 安全 ────────────────────────────────────────────────────────


class TestTestRunSecurity:
    def test_path_outside_sandbox_denied(self, gatekeeper):
        call = _make_call("operator", "/etc/passwd")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_url_ssrf_denied(self, gatekeeper):
        call = _make_call("operator", "http://127.0.0.1/admin")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_url_metadata_denied(self, gatekeeper):
        call = _make_call("operator", "http://169.254.169.254/latest/")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "https://example.com")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout="ok"),
        ):
            registry.execute(
                gatekeeper, _make_call("operator", str(test_file), audit_id="a-log")
            )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "test_run"
        assert audit.audit_id == "a-log"


# ── 成本（截断）───────────────────────────────────────────────────


class TestTestRunCost:
    def test_pytest_output_truncated(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        big = "x" * (MAX_OUTPUT_CHARS + 500)
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout=big),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", str(test_file)))
        assert len(call.result) < MAX_OUTPUT_CHARS + 100
        assert "已截断" in call.result

    def test_doc_output_truncated(self, gatekeeper):
        big = b"x" * (MAX_OUTPUT_CHARS + 500)
        with patch(
            "agent_builder.tools.impl.test_run.urllib.request.urlopen",
            return_value=_FakeResp(big),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", "https://example.com/huge")
            )
        assert len(call.result) < MAX_OUTPUT_CHARS + 100
        assert "已截断" in call.result


class TestWorkspaceTempEnv:
    """agent 自己写的 temp-using 测试在沙箱下必挂（``os.mkdir(0o700)`` 写的显式权限绕过
    了父目录继承的授权，连创建者自己都打不开 → ``PermissionError: [Errno 13]``）。
    所以 test_run / sandbox_run 都必须把子进程的临时目录指到工作区内。"""

    def test_临时环境指向工作区内且真的建好了(self, tmp_path):
        from pathlib import Path

        from agent_builder.tools.gatekeeper import current_workspace_dir, workspace_temp_env

        token = current_workspace_dir.set(tmp_path)
        try:
            env = workspace_temp_env({})
        finally:
            current_workspace_dir.reset(token)

        assert env["TMP"] == env["TEMP"] == env["TMPDIR"]
        assert Path(env["TMP"]) == tmp_path / ".agent-tmp"
        assert Path(env["TMP"]).is_dir()

    def test_test_run_把临时环境传给子进程(self, monkeypatch, tmp_path):
        """不只是有 helper：``_run_pytest`` 必须真的把它传下去。"""
        import agent_builder.tools.impl.test_run as tr
        from agent_builder.tools.gatekeeper import current_workspace_dir

        captured = {}

        class _Proc:
            returncode = 0
            stdout = "1 passed"
            stderr = ""

        def _fake_run(*args, **kwargs):
            captured.update(kwargs)
            return _Proc()

        monkeypatch.setattr(tr.subprocess, "run", _fake_run)
        token = current_workspace_dir.set(tmp_path)
        try:
            tr._run_pytest("tests/test_x.py", 30.0, "c-test")
        finally:
            current_workspace_dir.reset(token)

        assert captured["env"]["TMP"] == str(tmp_path / ".agent-tmp")


# ── 注册 ────────────────────────────────────────────────────────


    def test_Windows_下注入临时目录兼容层(self, monkeypatch, tmp_path):
        """实测（用户任务 8d50ac95）：agent 自己写的 tmpdir 用例在受限环境下必红
        （``os.mkdir(0o700)`` 连创建者都打不开），而模型**修不掉**它 —— 它只能看着
        「失败 2 项」空转。所以 test_run 必须注入兼容插件并把 pytest 临时根放到工作区内。"""
        import os as _os

        import agent_builder.tools.impl.test_run as tr
        from agent_builder.tools.gatekeeper import current_workspace_dir

        if _os.name != "nt":
            pytest.skip("该缺陷是 Windows 受限令牌特有的")

        token = current_workspace_dir.set(tmp_path)
        try:
            argv = tr._pytest_argv("tests/test_x.py")
            env = tr._child_env()
        finally:
            current_workspace_dir.reset(token)

        assert "-p" in argv
        assert tr.TMPFIX_PLUGIN in argv
        assert str(tr.APP_ROOT) in env["PYTHONPATH"]
        assert env["PYTEST_DEBUG_TEMPROOT"] == str(tmp_path / ".agent-tmp" / "pytest")

    def test_导入不了插件就绝不加_p(self, monkeypatch):
        """插件 import 不到时加 ``-p`` 会让 pytest 直接报错退出 —— 必须退回原始命令。"""
        import agent_builder.tools.impl.test_run as tr

        monkeypatch.setattr(tr, "_tmpfix_available", lambda: False)

        argv = tr._pytest_argv("tests/test_x.py")

        assert "-p" not in argv
        assert argv[:4] == [tr.sys.executable, "-m", "pytest", "tests/test_x.py"]



    def test_registered(self):
        assert "test_run" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("test_run")
        assert spec.name == "test_run"
        # 会执行外部命令/代码，与 sandbox_run 同级，不得标为 low（见工具评分「风险分级正确」维度）
        assert spec.risk_level == "medium"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 120.0
        assert spec.allowed_roles == ["operator"]
        assert "target" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestTestRunConstants:
    def test_max_output_chars_positive(self):
        assert MAX_OUTPUT_CHARS > 0
