"""file_write 工具测试：功能 / 边界 / 安全 / 成本（high_risk 审批）。

file_write 列入 operator.high_risk_tools，所有写操作需审批（approval.granted_by 非空）。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Approval, RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.file_write import MAX_CONTENT_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["file_write"],
            high_risk_tools=["file_write"],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


def _make_call(role, path, content, overwrite=False, *, approved=False, audit_id="a-1"):
    approval = Approval(required=True, granted_by="tester" if approved else None)
    return ToolCall(
        audit_id=audit_id, role=role, tool="file_write",
        args={"path": str(path), "content": content, "overwrite": overwrite},
        approval=approval,
    )


# ── 功能 ────────────────────────────────────────────────────────


class TestFileWriteFunctional:
    def test_write_new_file(self, gatekeeper, workspace):
        target = workspace / "new.txt"
        call = registry.execute(
            gatekeeper, _make_call("operator", target, "hello", approved=True)
        )
        assert target.read_text(encoding="utf-8") == "hello"
        assert "wrote" in call.result
        assert "5 chars" in call.result

    def test_write_unicode(self, gatekeeper, workspace):
        target = workspace / "中文.txt"
        call = registry.execute(
            gatekeeper, _make_call("operator", target, "你好世界", approved=True)
        )
        assert target.read_text(encoding="utf-8") == "你好世界"
        assert "4 chars" in call.result

    def test_overwrite_existing(self, gatekeeper, workspace):
        target = workspace / "old.txt"
        target.write_text("old", encoding="utf-8")
        registry.execute(
            gatekeeper,
            _make_call("operator", target, "new content", overwrite=True, approved=True),
        )
        assert target.read_text(encoding="utf-8") == "new content"

    def test_creates_parent_dirs(self, gatekeeper, workspace):
        target = workspace / "sub" / "dir" / "nested.txt"
        registry.execute(
            gatekeeper, _make_call("operator", target, "deep", approved=True)
        )
        assert target.read_text(encoding="utf-8") == "deep"


# ── 边界 ────────────────────────────────────────────────────────


class TestFileWriteEdge:
    def test_empty_path(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="file_write",
            args={"path": "", "content": "x"},
            approval=Approval(required=True, granted_by="u"),
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        # 缺参数 → E_VALIDATION（不是 E_PERMISSION）。
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_content_rejected(self, gatekeeper, workspace):
        # content=None 会在实现层报 E_VALIDATION，但 args 里 content 必填
        call = ToolCall(
            audit_id="a-e2", role="operator", tool="file_write",
            args={"path": str(workspace / "x.txt"), "content": None},  # type: ignore[arg-type]
            approval=Approval(required=True, granted_by="u"),
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        # None 会触发 TypeError → E_VALIDATION（registry 包装）
        assert exc_info.value.error_name in ("E_VALIDATION", "E_PERMISSION")

    def test_file_exists_no_overwrite(self, gatekeeper, workspace):
        target = workspace / "exists.txt"
        target.write_text("keep", encoding="utf-8")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", target, "new", approved=True)
            )
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "已存在" in exc_info.value.info.message
        # 错误信息给出可执行的修复提示，且为确定性失败（不重试）
        assert "overwrite=true" in exc_info.value.info.message
        assert exc_info.value.retryable is False
        # 原文件未被覆盖
        assert target.read_text(encoding="utf-8") == "keep"

    def test_content_too_long(self, gatekeeper, workspace):
        target = workspace / "big.txt"
        call = ToolCall(
            audit_id="a-e3", role="operator", tool="file_write",
            args={"path": str(target), "content": "a" * (MAX_CONTENT_CHARS + 1)},
            approval=Approval(required=True, granted_by="u"),
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "超长" in exc_info.value.info.message


# ── 占位符护栏（防「占位覆盖」写坏已有文件）────────────────────────


class TestFileWritePlaceholderGuard:
    """实测（用户任务复现 run-2 / run-3）：模型两次在「重写实现」时发出
    ``content="PLACEHOLDER"`` 的覆盖写，把上一轮**已经通过真实运行验证**的实现
    整个抹掉，随后那轮决策又解析失败 → 整个任务死掉。这是破坏性且可确定性防住的事故。"""

    def test_占位符覆盖被拒且原文件不被改动(self, gatekeeper, workspace):
        target = workspace / "agent.py"
        target.write_text("class Real:\n    pass\n", encoding="utf-8")
        call = _make_call("operator", target, "PLACEHOLDER", overwrite=True, approved=True)

        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)

        assert exc_info.value.error_name == "E_VALIDATION"
        assert "占位符" in exc_info.value.info.message
        # 关键：原文件必须原封不动（护栏的全部意义就在这里）
        assert target.read_text(encoding="utf-8") == "class Real:\n    pass\n"

    @pytest.mark.parametrize("bad", ["TODO", "todo", "...", "…", "待补充", "TBD", "  PLACEHOLDER  "])
    def test_各种占位符都被拒且不落盘(self, gatekeeper, workspace, bad):
        target = workspace / "case.py"
        call = _make_call("operator", target, bad, approved=True)

        with pytest.raises(AgentError):
            registry.execute(gatekeeper, call)

        assert not target.exists()  # 新文件也不该被写出来

    @pytest.mark.parametrize(
        "ok",
        ["x = 1", "class A:\n    pass", "# 这里用 PLACEHOLDER 作为示例说明占位符写法"],
    )
    def test_正常内容不受影响(self, gatekeeper, workspace, ok):
        target = workspace / "case.py"
        call = _make_call("operator", target, ok, approved=True)

        result = registry.execute(gatekeeper, call).result

        assert "wrote" in result
        assert target.read_text(encoding="utf-8") == ok


# ── 写完即回灌符号轮廓（模型"记住"自己的接口）────────────────────


class TestFileWriteOutline:
    """实测：模型写完实现后写测试时，会按**想象**的 API 写（``Plan`` vs ``ResearchPlan``、
    ``add_source`` vs ``collect_sources``、``rank_papers(topic=)`` vs ``keywords``），
    自己写的测试和自己的实现自相矛盾。写完必须把**内容的符号轮廓**随返回值带回。"""

    def test_写完_py_返回符号轮廓(self, gatekeeper, workspace):
        target = workspace / "agent.py"
        code = "class PaperAgent:\n    def rank_papers(self, keywords):\n        return keywords\n"
        call = _make_call("operator", target, code, approved=True)

        result = registry.execute(gatekeeper, call).result

        assert result.startswith(f"wrote {target}")
        assert "符号轮廓" in result
        assert "class PaperAgent" in result
        assert "def rank_papers" in result

    def test_非_py_文件不附轮廓(self, gatekeeper, workspace):
        target = workspace / "notes.md"
        call = _make_call("operator", target, "# 标题\nclass 不是代码\n", approved=True)

        result = registry.execute(gatekeeper, call).result

        assert "符号轮廓" not in result

    def test_写盘内容逐字节一致不做换行翻译(self, gatekeeper, workspace):
        """``newline=""``：写盘内容必须与给的内容**逐字节**一致。

        否则 Windows 下 `\\n` 会被翻成 CRLF，而 ``file_read`` 读回来是 LF ——
        模型照读到的文本构造 ``file_edit`` 的 old_string 时永远匹配不上
        （实测：473 行 CRLF 文件上 4 次编辑全部「找不到 old_string」）。
        """
        target = workspace / "x.py"
        call = _make_call("operator", target, "a = 1\nb = 2\n", approved=True)

        registry.execute(gatekeeper, call)

        assert target.read_bytes() == b"a = 1\nb = 2\n"


# ── 安全（审批门 + 沙箱）───────────────────────────────────────────

class TestFileWriteSecurity:
    def test_unapproved_rejected(self, gatekeeper, workspace):
        target = workspace / "secret.txt"
        call = _make_call("operator", target, "data", approved=False)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        assert "高风险" in exc_info.value.info.message
        assert not target.exists()

    def test_unregistered_role_denied(self, gatekeeper, workspace):
        target = workspace / "x.txt"
        call = _make_call("intruder", target, "x", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_path_outside_workspace_denied(self, gatekeeper, workspace):
        outside = workspace.parent / "outside.txt"
        call = _make_call("operator", outside, "x", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, workspace):
        target = workspace / "audit.txt"
        registry.execute(
            gatekeeper, _make_call("operator", target, "data", approved=True, audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "file_write"
        assert audit.audit_id == "a-log"

    def test_rejected_audit_logged(self, gatekeeper, workspace):
        target = workspace / "deny.txt"
        call = _make_call("operator", target, "data", approved=False)
        with pytest.raises(AgentError):
            registry.execute(gatekeeper, call)
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is False
        assert "高风险" in audit.reason


# ── 注册 ────────────────────────────────────────────────────────


class TestFileWriteRegistration:
    def test_registered(self):
        assert "file_write" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("file_write")
        assert spec.name == "file_write"
        assert spec.risk_level == "medium"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "content" in spec.parameters["required"]
        assert "path" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestFileWriteConstants:
    def test_max_content_chars_positive(self):
        assert MAX_CONTENT_CHARS > 0
