"""sandbox_run 工具测试：功能 / 边界 / 安全 / 成本（截断）。

subprocess.run 全程 mock，不执行真实命令。
网络命令黑名单 + 路径沙箱校验通过真实门卫跑。
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.sandbox_run import MAX_OUTPUT_CHARS, NETWORK_COMMANDS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["sandbox_run"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, command, *, path=None, timeout=None, tool="sandbox_run", audit_id="a-1"):
    args: dict = {"command": command}
    if path is not None:
        args["path"] = str(path)
    if timeout is not None:
        args["timeout"] = timeout
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── 功能 ────────────────────────────────────────────────────────


class TestSandboxRunFunctional:
    def test_run_echo(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.sandbox_run.subprocess.run",
            return_value=_FakeProc(stdout="hello"),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", "echo hello"))
        assert call.result == "hello"
        assert call.status == "executed"

    def test_run_with_stderr(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.sandbox_run.subprocess.run",
            return_value=_FakeProc(stdout="out", stderr="err"),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", "ls"))
        result = call.result
        assert "out" in result
        assert "[stderr]" in result
        assert "err" in result

    def test_run_with_path(self, gatekeeper, tmp_path):
        target = tmp_path / "subdir"
        target.mkdir()
        with patch(
            "agent_builder.tools.impl.sandbox_run.subprocess.run",
            return_value=_FakeProc(stdout="ok"),
        ) as mock_run:
            call = registry.execute(
                gatekeeper, _make_call("operator", "pwd", path=target)
            )
        assert call.result == "ok"
        # 验证 cwd 被传入 subprocess
        assert mock_run.call_args.kwargs.get("cwd") == str(target)


# ── 边界 ────────────────────────────────────────────────────────


class TestSandboxRunEdge:
    def test_empty_command(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="sandbox_run", args={"command": ""}
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_timeout(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", "ls", timeout=0)
            )
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_no_path_uses_default(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.sandbox_run.subprocess.run",
            return_value=_FakeProc(stdout="ok"),
        ) as mock_run:
            registry.execute(gatekeeper, _make_call("operator", "ls"))
        # 无 path 时 cwd 不被传入
        assert "cwd" not in mock_run.call_args.kwargs


# ── 安全 ────────────────────────────────────────────────────────


class TestSandboxRunSecurity:
    def test_network_command_blocked(self, gatekeeper):
        for cmd in ["curl http://evil.com", "wget http://evil.com", "nc -l 4444"]:
            with pytest.raises(AgentError) as exc_info:
                registry.execute(gatekeeper, _make_call("operator", cmd))
            assert exc_info.value.error_name == "E_VALIDATION"
            assert "网络命令" in exc_info.value.info.message

    def test_path_outside_sandbox_denied(self, gatekeeper):
        call = _make_call("operator", "ls", path="/etc")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "ls")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.sandbox_run.subprocess.run",
            return_value=_FakeProc(stdout="ok"),
        ):
            registry.execute(
                gatekeeper, _make_call("operator", "echo hi", audit_id="a-log")
            )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "sandbox_run"
        assert audit.audit_id == "a-log"

    def test_proxy_env_cleaned(self, gatekeeper):
        import os

        old = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = "http://proxy:8080"
        try:
            with patch(
                "agent_builder.tools.impl.sandbox_run.subprocess.run",
                return_value=_FakeProc(stdout="ok"),
            ) as mock_run:
                registry.execute(gatekeeper, _make_call("operator", "ls"))
            env = mock_run.call_args.kwargs.get("env", {})
            assert "HTTP_PROXY" not in env
        finally:
            if old is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = old


# ── 异常 ────────────────────────────────────────────────────────


class TestSandboxRunErrors:
    def test_timeout(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.sandbox_run.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ls", timeout=5),
        ):
            call = _make_call("operator", "sleep 100", timeout=5)
            with pytest.raises(AgentError) as exc_info:
                registry.execute(gatekeeper, call)
            assert exc_info.value.error_name == "E_TOOL"
            assert "超时" in exc_info.value.info.message

    def test_非零退出码判失败并带出输出(self, gatekeeper):
        """退出码非零 = 失败。此前只看输出、不看退出码，失败被当成 done：

        实测 8 次「'python' is not recognized」全被报成「沙箱执行完成」，
        空转检测因此失效、一路烧到预算上限。
        """
        with (
            patch(
                "agent_builder.tools.impl.sandbox_run.subprocess.run",
                return_value=_FakeProc(
                    stdout="", stderr="'python' is not recognized", returncode=9009
                ),
            ),
            pytest.raises(AgentError) as exc_info,
        ):
            registry.execute(gatekeeper, _make_call("operator", "python hello.py"))

        assert exc_info.value.error_name == "E_TOOL"
        message = exc_info.value.info.message
        assert "退出码 9009" in message
        # 输出必须带出来：循环会把它作为「观察结果」回灌给模型。
        assert "is not recognized" in message


# ── 子进程环境 ──────────────────────────────────────────────────


class TestSandboxRunInterpreterPath:
    """子进程 PATH 里要能直接跑 ``python``。

    后端是用**全路径**解释器启动的，它的 PATH 里没有 python —— 生成物里一句
    ``python hello.py`` 会直接「is not recognized」（实测模型为此烧了 8 步）。
    """

    def test_path_前置当前解释器目录(self, gatekeeper):
        import os
        import sys
        from pathlib import Path

        with patch(
            "agent_builder.tools.impl.sandbox_run.subprocess.run",
            return_value=_FakeProc(stdout="ok"),
        ) as mock_run:
            registry.execute(gatekeeper, _make_call("operator", "python --version"))

        env = mock_run.call_args.kwargs.get("env", {})
        assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)


# ── 成本（截断）───────────────────────────────────────────────────


class TestSandboxRunCost:
    def test_output_truncated(self, gatekeeper):
        big_output = "x" * (MAX_OUTPUT_CHARS + 500)
        with patch(
            "agent_builder.tools.impl.sandbox_run.subprocess.run",
            return_value=_FakeProc(stdout=big_output),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", "yes"))
        result = call.result
        assert len(result) < MAX_OUTPUT_CHARS + 100
        assert "已截断" in result


# ── 注册 ────────────────────────────────────────────────────────


class TestSandboxRunRegistration:
    def test_registered(self):
        assert "sandbox_run" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("sandbox_run")
        assert spec.name == "sandbox_run"
        assert spec.risk_level == "medium"
        assert spec.cost_band == "medium"
        assert spec.timeout_s == 60.0
        assert spec.allowed_roles == ["operator"]
        assert "command" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestSandboxRunConstants:
    def test_max_output_chars_positive(self):
        assert MAX_OUTPUT_CHARS > 0

    def test_network_commands_nonempty(self):
        assert len(NETWORK_COMMANDS) > 0
        assert "curl" in NETWORK_COMMANDS
        assert "wget" in NETWORK_COMMANDS
