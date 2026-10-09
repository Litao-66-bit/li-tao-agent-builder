"""file_read 工具测试：功能 / 边界 / 安全 / 成本（截断）。"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.file_read import MAX_OUTPUT_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    """临时白名单目录。"""
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    """operator 角色允许 file_read 的门卫。"""
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["file_read"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


def _make_call(role, path, *, tool="file_read", audit_id="a-1"):
    """构造 ToolCall（不执行），供成功/异常测试统一使用。"""
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args={"path": str(path)})


# ── 功能 ────────────────────────────────────────────────────────


class TestFileReadFunctional:
    def test_read_success(self, gatekeeper, workspace):
        target = workspace / "demo.txt"
        target.write_text("hello world", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target))
        assert call.result == "hello world"
        assert call.status == "executed"

    def test_read_unicode(self, gatekeeper, workspace):
        target = workspace / "中文.txt"
        target.write_text("你好，世界\n第二行", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target))
        assert call.result == "你好，世界\n第二行"

    def test_empty_file(self, gatekeeper, workspace):
        target = workspace / "empty.txt"
        target.write_text("", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target))
        assert call.result == ""


# ── 边界 ────────────────────────────────────────────────────────


class TestWorkspaceRelativeResolution:
    """回归：工具路径的解析基准是**当前工作区**，不是进程 cwd。

    实测背景：默认工作区恰好等于 cwd，所以此前看不出来；一旦用户切换工作区，
    `path="tests"` 会按 cwd 解析 → ①被门卫误判「超出沙箱白名单」，②即便放行也读写错目录。
    """

    def test_相对路径按工作区解析(self, gatekeeper, workspace, tmp_path_factory, monkeypatch):
        other = tmp_path_factory.mktemp("elsewhere")
        monkeypatch.chdir(other)  # cwd 与工作区不同
        (workspace / "a.txt").write_text("hi", encoding="utf-8")

        call = _make_call("operator", "a.txt")  # 相对路径

        assert registry.execute(gatekeeper, call).result == "hi"

    def test_相对路径不会偷渡到cwd(self, workspace, tmp_path_factory, monkeypatch):
        """反证：cwd 下同名文件不该被读到 —— 说明基准确实是工作区。"""
        other = tmp_path_factory.mktemp("elsewhere2")
        (other / "a.txt").write_text("from cwd", encoding="utf-8")
        monkeypatch.chdir(other)
        perms = {
            "operator": RolePerm(
                role="operator", allowed_tools=["file_read"], high_risk_tools=[]
            ),
        }
        gk = ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-rel")

        call = _make_call("operator", "a.txt")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gk, call)

        # 工作区里没有这个文件 → 走实现的「不存在」分支（而不是沙箱越界）。
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "不存在" in exc_info.value.info.message


class TestFileReadEdge:
    def test_missing_path_arg(self, gatekeeper):
        # args 不含 path → 门卫 _check_sandbox_path 取到空串 → 拦下并记拒绝。
        # 错误码是 E_VALIDATION（缺参数），**不是** E_PERMISSION —— 后者会把「模型忘了
        # 给参数」说成「没权限」，实测中模型因此误判为"换个动作"而不是补参数重试。
        call = ToolCall(audit_id="a-e1", role="operator", tool="file_read", args={})
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "缺少 path" in exc_info.value.info.message
        assert gatekeeper.audit_log[-1].allowed is False  # 仍然被拒并留档

    def test_empty_path_string(self, gatekeeper):
        call = ToolCall(audit_id="a-e2", role="operator", tool="file_read", args={"path": ""})
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_nonexistent_file(self, gatekeeper, workspace):
        call = _make_call("operator", workspace / "nope.txt")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "不存在" in exc_info.value.info.message

    def test_path_is_directory(self, gatekeeper, workspace):
        sub = workspace / "subdir"
        sub.mkdir()
        call = _make_call("operator", sub)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "不是文件" in exc_info.value.info.message

    def test_binary_file_rejected(self, gatekeeper, workspace):
        target = workspace / "binary.bin"
        target.write_bytes(bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0x01, 0x02, 0xFF]))
        call = _make_call("operator", target)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_TOOL"
        assert "UTF-8" in exc_info.value.info.message


# ── 安全 ────────────────────────────────────────────────────────


class TestFileReadSecurity:
    def test_unregistered_role_denied(self, gatekeeper, workspace):
        target = workspace / "secret.txt"
        target.write_text("top secret", encoding="utf-8")
        call = _make_call("intruder", target)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_role_without_tool_denied(self, workspace):
        perms = {
            "reader": RolePerm(role="reader", allowed_tools=["web_search"], high_risk_tools=[]),
        }
        gk = ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-s2")
        target = workspace / "x.txt"
        target.write_text("x", encoding="utf-8")
        call = _make_call("reader", target)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gk, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_path_outside_workspace_denied(self, gatekeeper, workspace):
        # workspace.parent 一定在白名单之外
        outside = workspace.parent / "outside_tool_test.txt"
        call = _make_call("operator", outside)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, workspace):
        target = workspace / "audit.txt"
        target.write_text("data", encoding="utf-8")
        registry.execute(gatekeeper, _make_call("operator", target, audit_id="a-log"))
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "file_read"
        assert audit.audit_id == "a-log"


# ── 行范围读取（大文件尾部此前永远读不到）────────────────────────


class TestFileReadRange:
    """实测（用户任务 cea128b8）：观察窗口只有 2000 字，6.6k 字文件的 `__init__` 永远
    进不了提示词，模型只能反复重读同一个文件直到空转停下。行范围是唯一的出路。"""

    def test_按行范围读取带表头(self, gatekeeper, workspace):
        target = workspace / "demo.py"
        target.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")
        call = registry.execute(
            gatekeeper,
            ToolCall(
                audit_id="a-range",
                role="operator",
                tool="file_read",
                args={"path": str(target), "start_line": 3, "end_line": 5},
            ),
        )
        assert call.result == f"【{target} 第 3-5 行 / 共 10 行】\nline3\nline4\nline5"

    def test_只给起始行读到尾(self, gatekeeper, workspace):
        target = workspace / "demo.py"
        target.write_text("\n".join(f"line{i}" for i in range(1, 6)), encoding="utf-8")
        call = registry.execute(
            gatekeeper,
            ToolCall(
                audit_id="a-range2",
                role="operator",
                tool="file_read",
                args={"path": str(target), "start_line": 4},
            ),
        )
        assert "第 4-5 行 / 共 5 行" in call.result
        assert call.result.endswith("line4\nline5")

    def test_结束行超界自动收敛到末行(self, gatekeeper, workspace):
        target = workspace / "demo.py"
        target.write_text("a\nb\nc", encoding="utf-8")
        call = registry.execute(
            gatekeeper,
            ToolCall(
                audit_id="a-range3",
                role="operator",
                tool="file_read",
                args={"path": str(target), "start_line": 2, "end_line": 999},
            ),
        )
        assert "第 2-3 行 / 共 3 行" in call.result

    def test_起始行超界报参数错误(self, gatekeeper, workspace):
        target = workspace / "demo.py"
        target.write_text("a\nb", encoding="utf-8")
        with pytest.raises(AgentError) as exc:
            registry.execute(
                gatekeeper,
                ToolCall(
                    audit_id="a-range4",
                    role="operator",
                    tool="file_read",
                    args={"path": str(target), "start_line": 99},
                ),
            )
        assert "超出文件行数" in str(exc.value)

    def test_结束行小于起始行报参数错误(self, gatekeeper, workspace):
        target = workspace / "demo.py"
        target.write_text("a\nb\nc", encoding="utf-8")
        with pytest.raises(AgentError) as exc:
            registry.execute(
                gatekeeper,
                ToolCall(
                    audit_id="a-range5",
                    role="operator",
                    tool="file_read",
                    args={"path": str(target), "start_line": 3, "end_line": 1},
                ),
            )
        assert "不能小于" in str(exc.value)

    def test_起始行为0报参数错误(self, gatekeeper, workspace):
        target = workspace / "demo.py"
        target.write_text("a\nb", encoding="utf-8")
        with pytest.raises(AgentError) as exc:
            registry.execute(
                gatekeeper,
                ToolCall(
                    audit_id="a-range6",
                    role="operator",
                    tool="file_read",
                    args={"path": str(target), "start_line": 0},
                ),
            )
        assert "必须 ≥ 1" in str(exc.value)

    def test_签名里带上行范围参数(self):
        """参数存在但不在签名里 = 模型不知道它存在（与 overwrite 同款教训）。"""
        from agent_builder.api.role_catalog import action_signature

        sig = action_signature("file_read")
        assert "start_line" in sig and "end_line" in sig


# ── 成本（大输出截断）───────────────────────────────────────────


class TestFileReadCost:
    def test_huge_file_truncated(self, gatekeeper, workspace):
        target = workspace / "huge.txt"
        target.write_text("a" * (MAX_OUTPUT_CHARS + 500), encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target))
        result = call.result
        assert len(result) < MAX_OUTPUT_CHARS + 100
        assert result.startswith("a" * MAX_OUTPUT_CHARS)
        assert "已截断" in result


# ── 注册 ────────────────────────────────────────────────────────


class TestFileReadRegistration:
    def test_registered_in_registry(self):
        assert "file_read" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("file_read")
        assert spec.name == "file_read"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "path" in spec.parameters["required"]
