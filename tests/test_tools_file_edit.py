"""file_edit 工具测试：功能 / 边界 / 安全 / 成本（high_risk 审批）。

file_edit 与 file_write 同档：列入 operator.high_risk_tools，所有修改需审批。
它存在的理由（实测）：没有它时，改一个函数签名也要整份重写 16.6k 字符的文件，
模型会退化成发 ``content="PLACEHOLDER"``（被占位符护栏拦下）然后干脆不写。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Approval, RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.file_edit import MAX_NEW_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["file_edit"],
            high_risk_tools=["file_edit"],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


def _make_call(
    role, path, old_string, new_string, *, replace_all=False, approved=False, audit_id="a-1"
):
    approval = Approval(required=True, granted_by="tester" if approved else None)
    return ToolCall(
        audit_id=audit_id,
        role=role,
        tool="file_edit",
        args={
            "path": str(path),
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        },
        approval=approval,
    )


# ── 功能 ────────────────────────────────────────────────────────


class TestFileEditFunctional:
    def test_唯一替换成功并回报行范围(self, gatekeeper, workspace):
        target = workspace / "agent.py"
        target.write_text("class A:\n    def f(self):\n        return 1\n", encoding="utf-8")

        result = registry.execute(
            gatekeeper,
            _make_call(
                "operator",
                target,
                "def f(self):",
                "def f(self, x=0):",
                approved=True,
            ),
        ).result

        assert result.startswith(f"edited {target}")
        assert "第 2 行" in result
        assert "替换 1 处" in result
        # 改完把**新接口**摆回给模型（改签名却忘同步调用方是实测反复出现的失败模式）
        assert "符号轮廓" in result
        assert "def f(self, x=0)" in result
        assert target.read_text(encoding="utf-8") == (
            "class A:\n    def f(self, x=0):\n        return 1\n"
        )

    def test_只改局部不触碰其余内容(self, gatekeeper, workspace):
        target = workspace / "big.py"
        body = "".join(f"line{i} = {i}\n" for i in range(1, 401))
        target.write_text(body, encoding="utf-8")

        registry.execute(
            gatekeeper, _make_call("operator", target, "line200 = 200", "line200 = 0", approved=True)
        )

        text = target.read_text(encoding="utf-8")
        assert "line200 = 0" in text
        assert "line201 = 201" in text  # 其余行原样保留
        assert len(text.splitlines()) == 400

    def test_replace_all_全部替换(self, gatekeeper, workspace):
        target = workspace / "x.py"
        target.write_text("a = 1\nb = 1\nC = 1\n", encoding="utf-8")

        result = registry.execute(
            gatekeeper,
            _make_call("operator", target, "= 1", "= 2", replace_all=True, approved=True),
        ).result

        assert "替换 3 处" in result
        assert target.read_text(encoding="utf-8") == "a = 2\nb = 2\nC = 2\n"

    def test_保留原有换行风格(self, gatekeeper, workspace):
        """``newline=""`` 读写：不得把 CRLF 文件整份改写成 LF（否则是假 diff）。"""
        target = workspace / "win.py"
        target.write_bytes(b"a = 1\r\nb = 2\r\n")

        registry.execute(gatekeeper, _make_call("operator", target, "b = 2", "b = 3", approved=True))

        assert target.read_bytes() == b"a = 1\r\nb = 3\r\n"

    def test_CRLF_文件用_LF_的原文也能改(self, gatekeeper, workspace):
        """实测（用户任务 72b28fdf）：``file_write`` 在 Windows 上写出的文件是 **CRLF**，
        而 ``file_read`` 读回来是 **LF**（通用换行归一）—— 模型照"读到的文本"构造的
        ``old_string`` 因此永远匹配不上，4 次编辑全部「找不到 old_string」。
        所以精确匹配失败后必须再按**换行等价**匹配，并且改完保持原文件的 CRLF。"""
        target = workspace / "win.py"
        target.write_bytes(b"def f(a):\r\n    '''doc'''\r\n    return a\r\n")

        result = registry.execute(
            gatekeeper,
            _make_call(
                "operator",
                target,
                "def f(a):\n    '''doc'''",
                "def f(a, b=0):\n    '''doc'''",
                approved=True,
            ),
        ).result

        assert "替换 1 处" in result
        assert target.read_bytes() == b"def f(a, b=0):\r\n    '''doc'''\r\n    return a\r\n"


# ── 边界 ────────────────────────────────────────────────────────


class TestFileEditEdge:
    def test_找不到原文时报错并提示先读(self, gatekeeper, workspace):
        target = workspace / "x.py"
        target.write_text("a = 1\n", encoding="utf-8")

        with pytest.raises(AgentError) as exc:
            registry.execute(gatekeeper, _make_call("operator", target, "a = 2", "a = 3", approved=True))

        assert exc.value.error_name == "E_VALIDATION"
        assert "找不到 old_string" in exc.value.info.message
        assert "file_read" in exc.value.info.message
        assert exc.value.retryable is False

    def test_出现多次且未开_replace_all_时报错(self, gatekeeper, workspace):
        target = workspace / "x.py"
        target.write_text("v = 1\nv = 1\n", encoding="utf-8")

        with pytest.raises(AgentError) as exc:
            registry.execute(gatekeeper, _make_call("operator", target, "v = 1", "v = 2", approved=True))

        assert "出现 2 次" in exc.value.info.message
        assert "replace_all" in exc.value.info.message
        assert target.read_text(encoding="utf-8") == "v = 1\nv = 1\n"  # 未改动

    def test_新旧相同被拒(self, gatekeeper, workspace):
        target = workspace / "x.py"
        target.write_text("a = 1\n", encoding="utf-8")

        with pytest.raises(AgentError) as exc:
            registry.execute(gatekeeper, _make_call("operator", target, "a = 1", "a = 1", approved=True))

        assert "不会产生任何变化" in exc.value.info.message

    def test_空_old_string_被拒(self, gatekeeper, workspace):
        target = workspace / "x.py"
        target.write_text("a = 1\n", encoding="utf-8")

        with pytest.raises(AgentError) as exc:
            registry.execute(gatekeeper, _make_call("operator", target, "", "x", approved=True))

        assert "old_string 不能为空" in exc.value.info.message

    def test_文件不存在被拒(self, gatekeeper, workspace):
        with pytest.raises(AgentError) as exc:
            registry.execute(
                gatekeeper,
                _make_call("operator", workspace / "nope.py", "a", "b", approved=True),
            )
        assert "不存在" in exc.value.info.message

    def test_路径是目录被拒(self, gatekeeper, workspace):
        (workspace / "sub").mkdir()
        with pytest.raises(AgentError) as exc:
            registry.execute(
                gatekeeper, _make_call("operator", workspace / "sub", "a", "b", approved=True)
            )
        assert "不是文件" in exc.value.info.message

    def test_新内容超长被拒(self, gatekeeper, workspace):
        target = workspace / "x.py"
        target.write_text("a = 1\n", encoding="utf-8")

        with pytest.raises(AgentError) as exc:
            registry.execute(
                gatekeeper,
                _make_call("operator", target, "a = 1", "x" * (MAX_NEW_CHARS + 1), approved=True),
            )

        assert "超长" in exc.value.info.message


# ── 安全（审批门 + 沙箱）───────────────────────────────────────────


class TestFileEditSecurity:
    def test_未审批被拒(self, gatekeeper, workspace):
        target = workspace / "x.py"
        target.write_text("a = 1\n", encoding="utf-8")

        with pytest.raises(AgentError) as exc:
            registry.execute(gatekeeper, _make_call("operator", target, "a = 1", "a = 2"))

        assert exc.value.error_name == "E_PERMISSION"
        assert target.read_text(encoding="utf-8") == "a = 1\n"

    def test_越界路径被拒(self, gatekeeper, workspace):
        outside = workspace.parent / "outside.py"
        with pytest.raises(AgentError) as exc:
            registry.execute(
                gatekeeper, _make_call("operator", outside, "a", "b", approved=True)
            )
        assert exc.value.error_name in {"E_PERMISSION", "E_VALIDATION"}


# ── 注册 ────────────────────────────────────────────────────────


class TestFileEditRegistration:
    def test_registered_in_registry(self):
        assert "file_edit" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("file_edit")
        assert set(spec.parameters["properties"]) == {
            "path",
            "old_string",
            "new_string",
            "replace_all",
        }
        assert set(spec.parameters["required"]) == {"path", "old_string", "new_string"}
        assert spec.risk_level == "medium"
