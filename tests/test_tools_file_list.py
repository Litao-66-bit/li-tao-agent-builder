"""file_list 工具测试：功能 / 边界 / 安全 / 成本（截断）。"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.file_list import EMPTY_DIR_MARKER, MAX_ENTRIES
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    """临时白名单目录。"""
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    """operator 角色允许 file_list 的门卫。"""
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["file_list"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


def _make_call(role, path, *, tool="file_list", audit_id="a-1"):
    """构造 ToolCall（不执行），供成功/异常测试统一使用。"""
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args={"path": str(path)})


# ── 功能 ────────────────────────────────────────────────────────


class TestFileListFunctional:
    def test_list_mixed_dir(self, gatekeeper, workspace):
        sub = workspace / "subdir"
        sub.mkdir()
        (workspace / "a.txt").write_text("aaa", encoding="utf-8")
        (workspace / "b.txt").write_text("bbb", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", workspace))
        result = call.result
        lines = result.split("\n")
        # 目录在前，文件在后；各自按名称排序。
        # 卡 48：目录**带上条目数** —— 只给 `[DIR] subdir/` 时模型看不出里面有多大，
        # 只能盲目下钻或直接跳过。这里 subdir 是空的，所以是 (0 项)。
        assert lines[0] == "[DIR]  subdir/  (0 项)"
        assert lines[1] == "[FILE] a.txt (3 bytes)"
        assert lines[2] == "[FILE] b.txt (3 bytes)"
        assert call.status == "executed"

    def test_list_empty_dir(self, gatekeeper, workspace):
        """空目录必须给**显式标记**，绝不能返回空串。

        回归（实测用户任务 d2cb511f，空工作区）：空串让模型无法区分「这个目录是空的」与
        「工具没返回东西」，于是连着 3 轮重复同一个 ``file_list(".")``（思考原话
        「工作区情况未知」），第 3 步就被空转闸门掐死 —— 0 产物。此前这里断言的**正是那个
        空串**（缺陷被测试钉住了），所以这条必须一起改。
        """
        call = registry.execute(gatekeeper, _make_call("operator", workspace))
        assert call.result == EMPTY_DIR_MARKER
        assert call.result != ""  # 核心不变式：观察结果不能是空的
        assert "空目录" in call.result
        assert call.status == "executed"

    def test_list_only_files(self, gatekeeper, workspace):
        (workspace / "x.txt").write_text("x", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", workspace))
        assert call.result == "[FILE] x.txt (1 bytes)"

    def test_list_only_dirs(self, gatekeeper, workspace):
        (workspace / "d1").mkdir()
        (workspace / "d2").mkdir()
        call = registry.execute(gatekeeper, _make_call("operator", workspace))
        lines = call.result.split("\n")
        assert lines[0] == "[DIR]  d1/  (0 项)"
        assert lines[1] == "[DIR]  d2/  (0 项)"

        # 反证（卡 48 的目的）：目录里有东西时，条目数必须如实反映出来。
        (workspace / "d1" / "x.txt").write_text("x", encoding="utf-8")
        (workspace / "d1" / "y.txt").write_text("y", encoding="utf-8")
        again = registry.execute(gatekeeper, _make_call("operator", workspace)).result
        assert "[DIR]  d1/  (2 项)" in again

    def test_list_nested_subdir(self, gatekeeper, workspace):
        sub = workspace / "parent"
        sub.mkdir()
        (sub / "child.txt").write_text("child", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", sub))
        # 条目带被列目录的前缀：模型才能把这个名字**原样**喂给 file_read。
        assert call.result == "[FILE] parent/child.txt (5 bytes)"

    def test_条目带目录前缀(self, gatekeeper, workspace):
        """裸文件名会误导模型：它知道文件在 agents/ 里，却会去读 ``path="research_agent.py"``

        （被解析成工作区根）→「文件不存在」→ 反复重试（实测连撞 2–5 次直到空转终止）。
        """
        sub = workspace / "agents"
        sub.mkdir()
        (sub / "research_agent.py").write_text("x", encoding="utf-8")

        call = registry.execute(gatekeeper, _make_call("operator", sub))

        assert call.result == "[FILE] agents/research_agent.py (1 bytes)"

    def test_列工作区根时不加前缀(self, gatekeeper, workspace):
        (workspace / "top.txt").write_text("t", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", workspace))
        assert call.result == "[FILE] top.txt (1 bytes)"


# ── 边界 ────────────────────────────────────────────────────────


class TestFileListEdge:
    def test_missing_path_arg(self, gatekeeper):
        # 缺参数 → E_VALIDATION（不是 E_PERMISSION），见 test_tools_file_read.py 同名用例。
        call = ToolCall(audit_id="a-e1", role="operator", tool="file_list", args={})
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "缺少 path" in exc_info.value.info.message

    def test_empty_path_string(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e2", role="operator", tool="file_list", args={"path": ""}
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_nonexistent_path(self, gatekeeper, workspace):
        call = _make_call("operator", workspace / "nope")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "不存在" in exc_info.value.info.message

    def test_path_is_file_not_dir(self, gatekeeper, workspace):
        target = workspace / "file.txt"
        target.write_text("hello", encoding="utf-8")
        call = _make_call("operator", target)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "不是目录" in exc_info.value.info.message


# ── 安全 ────────────────────────────────────────────────────────


class TestFileListSecurity:
    def test_unregistered_role_denied(self, gatekeeper, workspace):
        call = _make_call("intruder", workspace)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_role_without_tool_denied(self, workspace):
        perms = {
            "reader": RolePerm(role="reader", allowed_tools=["file_read"], high_risk_tools=[]),
        }
        gk = ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-s2")
        call = _make_call("reader", workspace)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gk, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_path_outside_workspace_denied(self, gatekeeper, workspace):
        outside = workspace.parent / "outside_list_test_dir"
        call = _make_call("operator", outside)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, workspace):
        (workspace / "audit.txt").write_text("data", encoding="utf-8")
        registry.execute(gatekeeper, _make_call("operator", workspace, audit_id="a-log"))
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "file_list"
        assert audit.audit_id == "a-log"


# ── 成本（超大目录截断）───────────────────────────────────────────


class TestFileListCost:
    def test_huge_dir_truncated(self, gatekeeper, workspace):
        for i in range(MAX_ENTRIES + 100):
            (workspace / f"file_{i:04d}.txt").write_text("x", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", workspace))
        result = call.result
        lines = result.split("\n")
        # 卡 48：截断信息放到**第一行** —— 观察窗口只保头部，写在末尾会跟着尾巴一起被丢掉
        # （实测：200 条匹配 / 1.2 万字的输出被裁到 2000 字，末尾的提示根本进不了提示词）。
        assert lines[0].startswith("…[共 ")
        assert "仅显示前" in lines[0]
        assert "600 条目" in lines[0] or f"{MAX_ENTRIES + 100} 条目" in lines[0]
        # 截断后的行数不应超过 MAX_ENTRIES + 1（截断提示行）
        assert len(lines) <= MAX_ENTRIES + 1


# ── 注册 ────────────────────────────────────────────────────────


class TestFileListRegistration:
    def test_registered_in_registry(self):
        assert "file_list" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("file_list")
        assert spec.name == "file_list"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "path" in spec.parameters["required"]
