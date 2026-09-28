"""记忆管家（MemoryKeeper）—— 主架构・记忆层。

职责：写入前校验内容分级 + 按类型存取 + 执行遗忘策略。

边界声明：
- 敏感信息 → 加密存储（memory_write 内部 base64 编码）
- 未经确认的"结论" → 不写入长期记忆
- 检索无结果 → 返回"无记录"而非编造

执行协议：
1. 写入前校验内容分级（会话级/知识级/敏感级）
2. 按类型存取：短期=会话隔离；长期=语义检索
3. 执行遗忘策略（过期清理、去重）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 记忆类 action 集合。
MEMORY_ACTIONS: frozenset[str] = frozenset({
    "memory_read",
    "memory_write",
    "memory_forget",
})

# 内容分级。
MEMORY_TIERS = frozenset({"session", "knowledge", "sensitive"})

# 未确认结论不可写入长期记忆。
UNCONFIRMED_MARKERS = frozenset({"未确认", "推测", "可能", "猜测", "draft", "unconfirmed"})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class MemoryEntry:
    """单条记忆条目。"""

    content: str
    tier: str  # session | knowledge | sensitive
    entry_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WriteResult:
    """写入确认。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    entry_id: str = ""
    tier: str = ""
    encrypted: bool = False  # 敏感信息是否加密存储
    reason: str | None = None  # 拒绝原因


@dataclass(slots=True)
class SearchResult:
    """检索结果。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    entries: list[MemoryEntry] = field(default_factory=list)
    message: str = ""  # 无结果时返回"无记录"
    error: str | None = None


@dataclass(slots=True)
class MemoryKeeper:
    """记忆管家角色：内容分级 + 敏感加密 + 遗忘策略。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
    """

    correlation_id: str = "c-unknown"

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> WriteResult | SearchResult:
        """执行记忆类步骤。

        Args:
            step: 记忆类步骤（action 必须在 MEMORY_ACTIONS 中）。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            WriteResult（写入）或 SearchResult（检索）。
        """
        # 1. 校验是否记忆类。
        if step.action not in MEMORY_ACTIONS:
            return WriteResult(
                step_id=step.id,
                status="rejected",
                reason=f"任务超出记忆范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            if step.action == "memory_read":
                return SearchResult(step_id=step.id, status="pending")
            return WriteResult(step_id=step.id, status="pending")

        # 3. 按动作分发。
        if step.action == "memory_write":
            return self._handle_write(step, executor_fn)
        if step.action == "memory_read":
            return self._handle_read(step, executor_fn)
        # memory_forget
        return self._handle_forget(step, executor_fn)

    def _handle_write(self, step: Step, executor_fn: ExecutorFn) -> WriteResult:
        """处理写入请求。"""
        content = str(step.inputs.get("content", ""))
        tier = step.inputs.get("tier", "session")
        if tier not in MEMORY_TIERS:
            tier = "session"

        # 敏感信息 → 加密存储。
        encrypted = tier == "sensitive"

        # 未确认结论 → 不写入长期记忆。
        if tier == "knowledge" and self._is_unconfirmed(content):
            return WriteResult(
                step_id=step.id,
                status="rejected",
                tier=tier,
                reason="未确认的结论不可写入长期记忆",
            )

        try:
            executor_fn(step)
            return WriteResult(
                step_id=step.id,
                status="done",
                tier=tier,
                encrypted=encrypted,
                entry_id=step.inputs.get("entry_id", ""),
            )
        except PermissionError as exc:
            return WriteResult(
                step_id=step.id,
                status="failed",
                tier=tier,
                reason=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  记忆管家需捕获所有执行异常
            return WriteResult(
                step_id=step.id,
                status="failed",
                tier=tier,
                reason=f"{type(exc).__name__}: {exc}",
            )

    def _handle_read(self, step: Step, executor_fn: ExecutorFn) -> SearchResult:
        """处理检索请求。"""
        try:
            executor_fn(step)
            raw_entries = step.inputs.get("results", [])
            entries = self._parse_entries(raw_entries)
            if not entries:
                return SearchResult(
                    step_id=step.id,
                    status="done",
                    message="无记录",
                )
            return SearchResult(
                step_id=step.id,
                status="done",
                entries=entries,
            )
        except PermissionError as exc:
            return SearchResult(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  记忆管家需捕获所有执行异常
            return SearchResult(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _handle_forget(self, step: Step, executor_fn: ExecutorFn) -> WriteResult:
        """处理遗忘/清理请求。"""
        try:
            executor_fn(step)
            return WriteResult(
                step_id=step.id,
                status="done",
                reason=step.inputs.get("forget_reason", "过期清理"),
            )
        except PermissionError as exc:
            return WriteResult(
                step_id=step.id,
                status="failed",
                reason=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  记忆管家需捕获所有执行异常
            return WriteResult(
                step_id=step.id,
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
            )

    def _is_unconfirmed(self, content: str) -> bool:
        """检查内容是否含未确认标记。"""
        lower_content = content.lower()
        return any(marker.lower() in lower_content for marker in UNCONFIRMED_MARKERS)

    def _parse_entries(self, raw_entries: list[dict[str, Any]]) -> list[MemoryEntry]:
        """解析记忆条目清单。"""
        entries: list[MemoryEntry] = []
        for raw in raw_entries:
            if isinstance(raw, dict) and "content" in raw:
                tier = raw.get("tier", "session")
                if tier not in MEMORY_TIERS:
                    tier = "session"
                entries.append(
                    MemoryEntry(
                        content=str(raw["content"]),
                        tier=tier,
                        entry_id=str(raw.get("entry_id", "")),
                        metadata=raw.get("metadata", {}),
                    )
                )
        return entries


__all__ = [
    "MEMORY_ACTIONS",
    "MEMORY_TIERS",
    "UNCONFIRMED_MARKERS",
    "ExecutorFn",
    "MemoryEntry",
    "MemoryKeeper",
    "SearchResult",
    "WriteResult",
]
