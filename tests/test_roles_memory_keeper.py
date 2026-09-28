"""MemoryKeeper 记忆管家角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：写入/检索/遗忘/敏感加密/未确认拒绝/无结果/拒绝/权限不足
- 边界：空步骤
- 授权：memory_keeper 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.memory_keeper import (
    MEMORY_ACTIONS,
    MEMORY_TIERS,
    UNCONFIRMED_MARKERS,
    MemoryKeeper,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_keeper() -> MemoryKeeper:
    return MemoryKeeper(correlation_id="c-test")


def _make_step(
    action: str = "memory_write",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestMemoryKeeperFunctional:
    def test_write_session(self):
        """写入会话级 → done + encrypted=False。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "会话内容", "tier": "session"})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.tier == "session"
        assert result.encrypted is False

    def test_write_knowledge(self):
        """写入知识级 → done + encrypted=False。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "确认的事实", "tier": "knowledge"})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.tier == "knowledge"
        assert result.encrypted is False

    def test_write_sensitive_encrypted(self):
        """写入敏感级 → done + encrypted=True。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "密钥", "tier": "sensitive"})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.tier == "sensitive"
        assert result.encrypted is True

    def test_write_unconfirmed_rejected(self):
        """未确认结论写入长期 → rejected。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "这是推测的结果", "tier": "knowledge"})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "未确认" in result.reason

    def test_write_unconfirmed_session_allowed(self):
        """未确认结论写入会话级 → 允许（不拒绝）。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "这是推测的结果", "tier": "session"})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_read_with_results(self):
        """检索有结果 → done + entries。"""
        k = _make_keeper()
        step = _make_step("memory_read", {
            "results": [
                {"content": "事实1", "tier": "knowledge"},
                {"content": "事实2", "tier": "session"},
            ],
        })
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert len(result.entries) == 2

    def test_read_no_results(self):
        """检索无结果 → done + "无记录"。"""
        k = _make_keeper()
        step = _make_step("memory_read", {"results": []})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.message == "无记录"
        assert result.entries == []

    def test_forget(self):
        """遗忘/清理 → done。"""
        k = _make_keeper()
        step = _make_step("memory_forget", {"forget_reason": "过期清理"})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert "过期清理" in result.reason

    def test_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        k = _make_keeper()
        step = _make_step("memory_write")
        result = k.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_no_fn_read_returns_pending(self):
        """memory_read + executor_fn=None → pending。"""
        k = _make_keeper()
        step = _make_step("memory_read")
        result = k.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_memory_action(self):
        """非记忆类 → rejected。"""
        k = _make_keeper()
        step = _make_step("file_write")
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出记忆范围" in result.reason

    def test_permission_denied_write(self):
        """写入权限不足 → failed。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "x", "tier": "session"})

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = k.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.reason

    def test_permission_denied_read(self):
        """检索权限不足 → failed。"""
        k = _make_keeper()
        step = _make_step("memory_read")

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = k.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "x", "tier": "session"})

        def fn(s: Step) -> str:
            raise RuntimeError("存储错误")

        result = k.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.reason

    def test_invalid_tier_defaults_session(self):
        """非法分级 → 默认 session。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "x", "tier": "invalid"})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.tier == "session"


# ── 边界 ────────────────────────────────────────────────────────


class TestMemoryKeeperEdge:
    def test_empty_content_write(self):
        """空内容写入 → done（不强制非空）。"""
        k = _make_keeper()
        step = _make_step("memory_write", {"content": "", "tier": "session"})
        result = k.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_unconfirmed_markers_diverse(self):
        """多种未确认标记都能识别。"""
        k = _make_keeper()
        for marker in ["未确认", "推测", "可能", "猜测"]:
            step = _make_step("memory_write", {"content": f"这是{marker}的结论", "tier": "knowledge"})
            result = k.execute(step, executor_fn=lambda s: "ok")
            assert result.status == "rejected", f"标记 {marker} 应被拒绝"


# ── 授权 ────────────────────────────────────────────────────────


class TestMemoryKeeperPermissions:
    def test_role_registered(self):
        assert "memory_keeper" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["memory_keeper"]
        assert set(perm.allowed_tools) == {
            "memory_read",
            "memory_write",
            "memory_forget",
            "audit_log",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["memory_keeper"]
        assert perm.high_risk_tools == []

    def test_no_external_tools(self):
        """memory_keeper 无网络/文件/提交工具。"""
        perm = DEFAULT_ROLE_PERMS["memory_keeper"]
        forbidden = {"file_write", "web_fetch", "web_search", "git_commit", "rollback", "sandbox_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestMemoryKeeperConstants:
    def test_memory_actions_nonempty(self):
        assert len(MEMORY_ACTIONS) > 0
        assert "memory_read" in MEMORY_ACTIONS
        assert "memory_write" in MEMORY_ACTIONS
        assert "memory_forget" in MEMORY_ACTIONS

    def test_memory_actions_excludes_non_memory(self):
        assert "file_write" not in MEMORY_ACTIONS
        assert "web_search" not in MEMORY_ACTIONS
        assert "data_query" not in MEMORY_ACTIONS

    def test_memory_tiers(self):
        assert "session" in MEMORY_TIERS
        assert "knowledge" in MEMORY_TIERS
        assert "sensitive" in MEMORY_TIERS

    def test_unconfirmed_markers_nonempty(self):
        assert len(UNCONFIRMED_MARKERS) > 0
