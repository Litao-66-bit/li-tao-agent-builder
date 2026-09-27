"""memory_forget：遗忘 / 清理过期记忆（由记忆管家专用）。

安全边界：清理操作，无审批。门卫只做角色权限校验（allowed_roles=["memory_manager"]）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.impl.memory_store import load_memory, save_memory
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 默认短期记忆 TTL（秒）：1 小时。
DEFAULT_SHORT_TTL = 3600
# 支持的 scope 值。
VALID_SCOPES = frozenset({"short", "long", "all"})


def forget_memory(
    key: str = "",
    scope: str = "all",
    expired_only: bool = True,
) -> str:
    """遗忘 / 清理记忆。

    Args:
        key: 指定 key 时只删该 key 的条目；空则按 scope/expired_only 批量清理。
        scope: ``short`` / ``long`` / ``all``。
        expired_only: 为 true 时只清理过期条目（short scope 的条目超过 TTL 视为过期）。

    Returns:
        确认消息：``forgot <N> entries``。

    Raises:
        AgentError(E_VALIDATION): scope 非法。
        AgentError(E_TOOL): 存储读写失败（由 memory_store 抛出）。
    """
    cid = current_correlation_id.get()
    if scope not in VALID_SCOPES:
        raise validation_error(
            f"memory_forget: scope 非法 {scope!r}，可选: {sorted(VALID_SCOPES)}",
            source="tool.memory_forget",
            correlation_id=cid,
        )

    data = load_memory()
    now = datetime.now(timezone.utc)
    scopes = ["short", "long"] if scope == "all" else [scope]
    total_removed = 0

    for s in scopes:
        original = data.get(s, [])
        kept: list[dict] = []
        for entry in original:
            if _should_forget(entry, key, s, expired_only, now):
                total_removed += 1
            else:
                kept.append(entry)
        data[s] = kept

    save_memory(data)
    return f"forgot {total_removed} entries"


def _should_forget(
    entry: dict,
    key: str,
    scope: str,
    expired_only: bool,
    now: datetime,
) -> bool:
    """判断一条记忆是否应被清理。"""
    entry_key = str(entry.get("key", ""))

    # 指定 key 时只删该 key。
    if key:
        return entry_key == key

    # 未指定 key 且 expired_only=true：只清过期的（short scope 超过 TTL）。
    if expired_only:
        if scope != "short":
            return False  # long scope 无 TTL，不视为过期
        ts = str(entry.get("ts", ""))
        if not ts:
            return False  # 无时间戳不清理
        try:
            entry_time = datetime.fromisoformat(ts)
        except ValueError:
            return False  # 时间戳损坏不清理
        age = (now - entry_time).total_seconds()
        return age > DEFAULT_SHORT_TTL

    # expired_only=false：未指定 key 时清空该 scope 全部。
    return True


spec = ToolSpec(
    name="memory_forget",
    description="遗忘 / 清理过期记忆（由记忆管家专用）",
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "指定 key 时只删该条目；空则按 scope 批量清理",
                "default": "",
            },
            "scope": {
                "type": "string",
                "enum": ["short", "long", "all"],
                "description": "清理范围",
                "default": "all",
            },
            "expired_only": {
                "type": "boolean",
                "description": "为 true 时只清理过期条目",
                "default": True,
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["memory_manager"],
)

registry.register(spec, forget_memory)


__all__ = ["DEFAULT_SHORT_TTL", "VALID_SCOPES", "forget_memory", "spec"]
