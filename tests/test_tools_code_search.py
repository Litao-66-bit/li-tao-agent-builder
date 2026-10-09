"""code_search 工具测试：功能 / 边界 / 安全 / 成本（截断）。

subprocess 调用全程 mock，不依赖系统是否安装 ripgrep。
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.code_search import MAX_RESULTS, RG_BIN, RG_TIMEOUT
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["code_search"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


@pytest.fixture(autouse=True)
def _force_rg_available(monkeypatch):
    """这些用例测的是「rg 可用」这条路径：把解析结果钉成 "rg"，与真实机器是否装了 rg 无关。

    （「找不到 rg」那条路径由 ``TestBuiltinScanner`` 覆盖。）
    """
    import agent_builder.tools.impl.code_search as cs

    monkeypatch.setattr(cs, "_resolve_rg", lambda: "rg")


def _make_call(role, path, pattern="hello", max_results=None, *, tool="code_search", audit_id="a-1"):
    args = {"path": str(path), "pattern": pattern}
    if max_results is not None:
        args["max_results"] = max_results
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


class _CompletedProc:
    """subprocess.CompletedProcess 的简易替身，避免构造真实对象。"""

    def __init__(self, stdout: str, stderr: str, returncode: int):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _rg_match_json(path: str, line_no: int, content: str) -> str:
    """构造一行 rg --json 的 match 记录。"""
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": path},
                "line_number": line_no,
                "lines": {"text": content},
            },
        }
    )


# ── 功能 ────────────────────────────────────────────────────────


class TestCodeSearchFunctional:
    def test_search_success(self, gatekeeper, workspace):
        target = workspace / "demo.py"
        target.write_text("hello world\nfoo bar\nhello again\n", encoding="utf-8")
        out = "\n".join(
            [_rg_match_json(str(target), 1, "hello world\n"),
             _rg_match_json(str(target), 3, "hello again\n")]
        )
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc(out, "", 0),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", workspace, "hello"))
        result = call.result
        assert str(target) + ":1:hello world" in result
        assert str(target) + ":3:hello again" in result
        assert call.status == "executed"

    def test_no_match_returns_empty(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc("", "", 1),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", workspace, "nope"))
        assert call.result == ""

    def test_unicode_content(self, gatekeeper, workspace):
        target = workspace / "中文.txt"
        target.write_text("你好世界\n", encoding="utf-8")
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc(
                _rg_match_json(str(target), 1, "你好世界\n"), "", 0
            ),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", workspace, "你好"))
        assert "你好世界" in call.result


# ── 边界 ────────────────────────────────────────────────────────


class TestCodeSearchEdge:
    def test_empty_path(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="code_search",
            args={"path": "", "pattern": "x"},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        # 缺参数 → E_VALIDATION（不是 E_PERMISSION）：它不该被说成"没权限"。
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_pattern(self, gatekeeper, workspace):
        call = ToolCall(
            audit_id="a-e2", role="operator", tool="code_search",
            args={"path": str(workspace), "pattern": ""},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_max_results(self, gatekeeper, workspace):
        call = ToolCall(
            audit_id="a-e3", role="operator", tool="code_search",
            args={"path": str(workspace), "pattern": "x", "max_results": 0},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 异常 ────────────────────────────────────────────────────────


class TestBuiltinScanner:
    """rg 不可用时的内置纯 Python 扫描器 —— 核心能力不能因缺一个外部二进制整体废掉。

    实测（用户任务 2395cf45）：没装 rg 的环境里 ``code_search`` 直接 ``E_TOOL`` 失败，
    模型"搜代码定位函数"的正常动作全废；一轮里 ``test_run`` + ``code_search`` 连撞两个
    失败 → 两轮无产出 → 空转闸门在**第 3 步**掐死整轮。
    """

    def test_命中并给出文件与行号(self, gatekeeper, workspace, monkeypatch):
        import agent_builder.tools.impl.code_search as cs

        (workspace / "a.py").write_text("x = 1\ndef target():\n    pass\n", encoding="utf-8")
        monkeypatch.setattr(cs, "_resolve_rg", lambda: None)

        result = registry.execute(
            gatekeeper, _make_call("operator", workspace, "def target")
        ).result

        assert "a.py:2:def target():" in result
        assert "内置扫描器" in result

    def test_无匹配时只给扫描器说明(self, gatekeeper, workspace, monkeypatch):
        import agent_builder.tools.impl.code_search as cs

        (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(cs, "_resolve_rg", lambda: None)

        result = registry.execute(
            gatekeeper, _make_call("operator", workspace, "没有这个符号")
        ).result

        assert "没有这个符号" not in result
        assert "内置扫描器" in result

    def test_跳过隐藏目录与依赖目录(self, gatekeeper, workspace, monkeypatch):
        """与 rg 默认行为一致：隐藏文件/目录不搜；外加本项目的运行时目录。"""
        import agent_builder.tools.impl.code_search as cs

        (workspace / ".git").mkdir()
        (workspace / ".git" / "cfg").write_text("needle\n", encoding="utf-8")
        (workspace / ".deps").mkdir()
        (workspace / ".deps" / "lib.py").write_text("needle\n", encoding="utf-8")
        (workspace / "keep.py").write_text("needle\n", encoding="utf-8")
        monkeypatch.setattr(cs, "_resolve_rg", lambda: None)

        result = registry.execute(gatekeeper, _make_call("operator", workspace, "needle")).result

        assert "keep.py" in result
        assert ".git" not in result
        assert ".deps" not in result

    def test_二进制文件被跳过不打断搜索(self, gatekeeper, workspace, monkeypatch):
        import agent_builder.tools.impl.code_search as cs

        (workspace / "blob.bin").write_bytes(b"\xff\xfe\x00needle")
        (workspace / "ok.py").write_text("needle = 1\n", encoding="utf-8")
        monkeypatch.setattr(cs, "_resolve_rg", lambda: None)

        result = registry.execute(gatekeeper, _make_call("operator", workspace, "needle")).result

        assert "ok.py:1:needle = 1" in result

    def test_截断与_rg_路径同格式(self, gatekeeper, workspace, monkeypatch):
        import agent_builder.tools.impl.code_search as cs

        (workspace / "a.py").write_text("hit\nhit\nhit\n", encoding="utf-8")
        monkeypatch.setattr(cs, "_resolve_rg", lambda: None)

        result = registry.execute(
            gatekeeper, _make_call("operator", workspace, "hit", max_results=1)
        ).result

        assert "已截断" in result

    def test_非法正则报_E_VALIDATION(self, gatekeeper, workspace, monkeypatch):
        import agent_builder.tools.impl.code_search as cs

        (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(cs, "_resolve_rg", lambda: None)

        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", workspace, "(["))

        assert exc_info.value.error_name == "E_VALIDATION"
        assert "正则" in exc_info.value.info.message


class TestCodeSearchErrors:
    def test_rg_起不来时退到内置扫描器(self, gatekeeper, workspace):
        """rg 路径解析出来了、但进程起不来（被移走/无执行权限）→ 同样退到内置扫描器。"""
        (workspace / "x.py").write_text("def target():\n    pass\n", encoding="utf-8")
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            side_effect=FileNotFoundError("rg not found"),
        ):
            result = registry.execute(
                gatekeeper, _make_call("operator", workspace, "def target")
            ).result
        assert "def target" in result
        assert "内置扫描器" in result

    def test_rg_timeout(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="rg", timeout=RG_TIMEOUT),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", workspace, "x"))
        assert exc_info.value.error_name == "E_TOOL"
        assert "超时" in exc_info.value.info.message

    def test_rg_error_code(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc("", "bad regex", 2),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", workspace, "("))
        assert exc_info.value.error_name == "E_TOOL"
        assert "bad regex" in exc_info.value.info.message


# ── 安全 ────────────────────────────────────────────────────────


class TestCodeSearchSecurity:
    def test_unregistered_role_denied(self, gatekeeper, workspace):
        call = _make_call("intruder", workspace)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_path_outside_workspace_denied(self, gatekeeper, workspace):
        call = _make_call("operator", workspace.parent / "outside")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc("", "", 1),
        ):
            registry.execute(gatekeeper, _make_call("operator", workspace, "x", audit_id="a-log"))
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "code_search"
        assert audit.audit_id == "a-log"


# ── 成本（截断）───────────────────────────────────────────────────


class TestCodeSearchCost:
    def test_results_truncated(self, gatekeeper, workspace):
        target = workspace / "big.py"
        target.write_text("x\n", encoding="utf-8")
        # 构造 50 条匹配，max_results=10 → 截断
        matches = [_rg_match_json(str(target), i, "x\n") for i in range(50)]
        out = "\n".join(matches)
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc(out, "", 0),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", workspace, "x", max_results=10)
            )
        lines = call.result.split("\n")
        # 应只有 10 条 + 1 条截断提示
        assert len(lines) == 11
        assert "已截断" in lines[-1]


# ── 注册 ────────────────────────────────────────────────────────


class TestCodeSearchRegistration:
    def test_registered(self):
        assert "code_search" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("code_search")
        assert spec.name == "code_search"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 30.0
        assert spec.allowed_roles == ["operator"]
        assert "pattern" in spec.parameters["required"]
        assert "path" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestCodeSearchConstants:
    def test_max_results_default(self):
        assert MAX_RESULTS == 200

    def test_rg_bin(self):
        assert RG_BIN == "rg"

    def test_rg_timeout_positive(self):
        assert RG_TIMEOUT > 0
