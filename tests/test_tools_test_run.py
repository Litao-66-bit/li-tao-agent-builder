"""test_run 工具测试：功能 / 边界 / 安全 / 成本（截断）。

subprocess.run 和 urllib.request.urlopen 全程 mock。
路径沙箱 + URL 安全校验通过真实门卫跑。
"""

from __future__ import annotations

import subprocess
import sys
import urllib.error
from pathlib import Path
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
        # 首行是「实际执行了什么」的审计信息（不参与截断）→ 只对正文量长度，
        # 否则这里量到的是 正文 + 审计行，上界会随工作区路径长短漂移。
        body = call.result.split("\n", 1)[1]
        assert len(body) < MAX_OUTPUT_CHARS + 100
        assert "已截断" in body

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
        """插件 import 不到时加 ``-p`` 会让 pytest 直接报错退出 —— 必须退回原始命令。

        注意 ``-p no:cacheprovider`` **不受此限**：它引用的是 pytest **内置**插件名
        （永远存在、不可能 import 失败），而且必须**无条件**加 —— 见
        ``test_必须关掉pytest缓存插件``。这里钉住的保护意图是
        「**tmpfix 插件**拿不到就绝不能加它」。
        """
        import agent_builder.tools.impl.test_run as tr

        monkeypatch.setattr(tr, "_tmpfix_available", lambda: False)

        argv = tr._pytest_argv("tests/test_x.py")

        assert tr.TMPFIX_PLUGIN not in argv
        assert argv[:4] == [tr.sys.executable, "-m", "pytest", "tests/test_x.py"]
        assert argv[-2:] == ["-p", "no:cacheprovider"]

    def test_必须关掉pytest缓存插件(self):
        """回归（实测任务 8d79faee，2026-10-10）：cacheprovider 会在**用户工作区根**
        用 mkdtemp 建 ``pytest-cache-files-*`` 再 rename 成 ``.pytest_cache``；
        受限令牌下 mkdtemp 的显式 0o700 权限让 rename 失败（``[WinError 5]``），
        于是每次 test_run 都在工作区留下一个**列不出、删不掉**的残留目录
        （还会被列进产物面板）。

        实测口径：加 ``-p no:cacheprovider`` 后 8 → 8（零新增）；不加 8 → 10。
        """
        import agent_builder.tools.impl.test_run as tr

        argv = tr._pytest_argv("tests/test_x.py")

        assert "no:cacheprovider" in argv



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


class TestTarget按工作区解析:
    """回归（实测用户任务 ``98bab8c4``，2026-10-09）：

    agent 把交付物写进工作区 ``outputs/``，第 5 步 ``test_run`` 的 target 是相对路径
    ``outputs/test_paper_agent.py``。子进程**没有传 cwd**，于是相对路径被按继承来的
    进程 cwd（＝本应用仓库根）解析 → 跑到了仓库根下**同名的旧文件**上，报出
    「9 passed in 0.13s」，而工作区里那份真正要交付的测试其实是 ``4 failed / 1 passed``。

    后果有两层：① 假绿（循环据此以 final 收尾，把不工作的东西当成功交付）；
    ② 门卫校验的对象（工作区内相对路径）与真正执行的对象（工作区外文件）不一致。

    这一组钉住修法：**target 按工作区解析成绝对路径 + 子进程 cwd 设为工作区**。
    """

    def test_相对target解析为工作区内绝对路径且cwd为工作区(self, monkeypatch, tmp_path):
        import agent_builder.tools.impl.test_run as tr
        from agent_builder.tools.gatekeeper import current_workspace_dir

        captured: dict = {}

        class _Proc:
            returncode = 0
            stdout = "1 passed"
            stderr = ""

        def _fake_run(*args, **kwargs):
            captured["argv"] = list(args[0])
            captured.update(kwargs)
            return _Proc()

        monkeypatch.setattr(tr.subprocess, "run", _fake_run)
        token = current_workspace_dir.set(tmp_path)
        try:
            tr._run_pytest("outputs/test_x.py", 30.0, "c-test")
        finally:
            current_workspace_dir.reset(token)

        target = Path(captured["argv"][3])
        assert target.is_absolute()
        assert target == tmp_path / "outputs" / "test_x.py"
        assert target != Path("outputs/test_x.py")  # 绝不能把相对路径原样交给子进程
        assert Path(captured["cwd"]) == tmp_path  # cwd 必须是工作区，不是进程 cwd

    def test_工作区内失败的用例必须真报失败(self, tmp_path):
        """真跑一次 pytest（不 mock）：工作区里放一个必失败用例 → 输出必须是 failed。

        这是**行为侧**证据：只钉住「传了 cwd」证明不了「跑的确实是工作区那份文件」。
        修好之前，这里会去进程 cwd 下的 ``outputs/`` 找 ``test_x.py``（找不到 → pytest
        报 file or directory not found），断言随即失败 —— 正是实测踩到的那个假绿。
        """
        import agent_builder.tools.impl.test_run as tr
        from agent_builder.tools.gatekeeper import current_workspace_dir

        outputs = tmp_path / "outputs"
        outputs.mkdir()
        (outputs / "test_x.py").write_text(
            "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert False\n",
            encoding="utf-8",
        )
        token = current_workspace_dir.set(tmp_path)
        try:
            out = tr._run_pytest("outputs/test_x.py", 60.0, "c-test")
        finally:
            current_workspace_dir.reset(token)

        assert "1 failed" in out
        assert "1 passed" in out


class TestSubprocessEncoding:
    """回归（本次验证时实测被绊倒）：``subprocess.run(text=True)`` 不指定编码时，读线程按
    **本地编码**（中文 Windows 是 gbk）解码子进程输出。子进程若写 UTF-8
    （``PYTHONIOENCODING=utf-8`` / ``PYTHONUTF8=1``，开发机上很常见），解码即抛
    ``UnicodeDecodeError`` → ``proc.stdout`` 变成 **None** → ``len(output)`` 抛
    ``TypeError: object of type 'NoneType' has no len()`` —— 整个 test_run **崩掉**，
    而不是给出输出（函数签名也变成了"可能返回 None"）。

    修法：读侧显式 ``encoding="utf-8", errors="replace"``，写侧在 ``_child_env()`` 里固定
    ``PYTHONIOENCODING=utf-8``，两侧成对锁死。
    """

    def test_子进程写UTF8时仍能取到输出(self, monkeypatch, tmp_path):
        import agent_builder.tools.impl.test_run as tr
        from agent_builder.tools.gatekeeper import current_workspace_dir

        # 正是实测的触发条件：父进程环境带 PYTHONIOENCODING=utf-8 → 子进程输出 UTF-8
        monkeypatch.setenv("PYTHONIOENCODING", "utf-8")

        outputs = tmp_path / "outputs"
        outputs.mkdir()
        (outputs / "test_cn.py").write_text(
            "def test_cn():\n    print('中文输出-复现')\n    assert False\n",
            encoding="utf-8",
        )
        token = current_workspace_dir.set(tmp_path)
        try:
            out = tr._run_pytest("outputs/test_cn.py", 60.0, "c-test")
        finally:
            current_workspace_dir.reset(token)

        assert "1 failed" in out
        # 中文须经 UTF-8 完整往返；编码不匹配时这里根本走不到（上面先抛 TypeError）
        assert "中文输出-复现" in out

    def test_子进程环境固定UTF8(self, tmp_path):
        import agent_builder.tools.impl.test_run as tr
        from agent_builder.tools.gatekeeper import current_workspace_dir

        token = current_workspace_dir.set(tmp_path)
        try:
            env = tr._child_env()
        finally:
            current_workspace_dir.reset(token)

        assert env["PYTHONIOENCODING"] == "utf-8"


class Test执行对象可审计与越界拒绝:
    """卡2：报告的每一环都要能核对「到底跑了哪个文件」；越界 target 工具层自己就拒。

    动机同卡1：那次假绿之所以能一路走到底，是因为**报告里没有任何一处写明执行对象**，
    于是"验证了别的文件"这件事在模型侧、审计侧、人侧都不可见。
    """

    def test_输出写明工作区与实际执行路径(self, monkeypatch, tmp_path):
        import agent_builder.tools.impl.test_run as tr
        from agent_builder.tools.gatekeeper import current_workspace_dir

        class _Proc:
            returncode = 0
            stdout = "1 passed in 0.10s"
            stderr = ""

        monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: _Proc())
        token = current_workspace_dir.set(tmp_path)
        try:
            out = tr._run_pytest("outputs/test_x.py", 30.0, "c-test")
        finally:
            current_workspace_dir.reset(token)

        # 审计行必须在最前（否则可能被输出截断吞掉）
        assert out.splitlines()[0].startswith("[test_run]")
        assert f"工作区={tmp_path}" in out
        assert f"实际执行={tmp_path / 'outputs' / 'test_x.py'}" in out

    def test_审计行不干扰计数解析(self):
        """审计行拼在 pytest 输出之前，绝不能让「测试通过 N 项」的口径失真。"""
        from agent_builder.roles.test_runner import _parse_pytest_counts

        audit = r"[test_run] 工作区=C:\ws 实际执行=C:\ws\outputs\test_x.py"
        assert _parse_pytest_counts(f"{audit}\n1 failed, 1 passed in 0.13s\n") == (1, 1, 0)
        assert _parse_pytest_counts(f"{audit}\n9 passed in 0.13s\n") == (9, 0, 0)

    def test_越界target不执行(self, monkeypatch, tmp_path):
        """门卫会拦，但"直接调实现"绕过了门卫 —— 工具层必须自己拒绝，且绝不执行。"""
        import agent_builder.tools.impl.test_run as tr
        from agent_builder.contracts.errors import AgentError
        from agent_builder.tools.gatekeeper import current_workspace_dir

        called: list = []

        class _Proc:
            returncode = 0
            stdout = "1 passed"
            stderr = ""

        def _fake_run(*args, **kwargs):
            called.append(args)
            return _Proc()

        monkeypatch.setattr(tr.subprocess, "run", _fake_run)
        outside = tmp_path.parent / "outside_test_x.py"
        token = current_workspace_dir.set(tmp_path)
        try:
            with pytest.raises(AgentError):
                tr._run_pytest(str(outside), 30.0, "c-test")
        finally:
            current_workspace_dir.reset(token)

        assert called == []


# ── 常量 ────────────────────────────────────────────────────────


class TestTestRunConstants:
    def test_max_output_chars_positive(self):
        assert MAX_OUTPUT_CHARS > 0
