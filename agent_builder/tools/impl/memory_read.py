"""memory_read：检索记忆（短期 / 长期）。

安全边界：只读，无副作用，无审批。门卫只做角色权限校验。
敏感信息：存储层已 base64 编码，读取时解码显示原文（加 [SENSITIVE] 标记）。
"""

from __future__ import annotations

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.impl.memory_store import decode_sensitive, load_memory
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 默认返回条目数。
DEFAULT_LIMIT = 10
# 单次查询最大条目数。
MAX_ENTRIES = 100
# 支持的 scope 值。
VALID_SCOPES = frozenset({"short", "long", "all"})


def read_memory(
    query: str = "",
    scope: str = "all",
    limit: int = DEFAULT_LIMIT,
) -> str:
    """检索记忆。

    Args:
        query: 搜索关键词（匹配 key 或 content）；空则返回全部（截断到 limit）。
        scope: 检索范围 ``short`` / ``long`` / ``all``。
        limit: 返回最大条目数，超过 MAX_ENTRIES 自动截断。

    Returns:
        每行一条记忆：``[scope] key — content (ts)``。
        敏感条目加 ``[SENSITIVE]`` 前缀。
        无匹配返回 ``(no memory found)``。

    Raises:
        AgentError(E_VALIDATION): scope 非法 / limit 非法。
    """
    cid = current_correlation_id.get()
    if scope not in VALID_SCOPES:
        raise validation_error(
            f"memory_read: scope 非法 {scope!r}，可选: {sorted(VALID_SCOPES)}",
            source="tool.memory_read",
            correlation_id=cid,
        )
    if limit <= 0:
        raise validation_error(
            f"memory_read: limit 必须为正数: {limit}",
            source="tool.memory_read",
            correlation_id=cid,
        )
    limit = min(limit, MAX_ENTRIES)

    data = load_memory()
    query_lower = query.strip().lower()

    lines: list[str] = []
    scopes = ["short", "long"] if scope == "all" else [scope]
    for s in scopes:
        for entry in data.get(s, []):
            key = str(entry.get("key", ""))
            content = str(entry.get("content", ""))
            encrypted = bool(entry.get("encrypted", False))
            ts = str(entry.get("ts", ""))

            if encrypted:
                try:
                    content = decode_sensitive(content)
                except Exception:  # noqa: BLE001  解码失败保留原文标记
                    content = "[DECODE_FAILED]"
                prefix = "[SENSITIVE] "
            else:
                prefix = ""

            if query_lower and query_lower not in key.lower() and query_lower not in content.lower():
                continue

            lines.append(f"[{s}] {prefix}{key} — {content} ({ts})")
            if len(lines) >= limit:
                break
        if len(lines) >= limit:
            break

    if not lines:
        return "(no memory found)"
    return "\n".join(lines)


spec = ToolSpec(
    name="memory_read",
    description="检索记忆（短期/长期，支持关键词过滤）",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词（匹配 key 或 content），空则返回全部",
                "default": "",
            },
            "scope": {
                "type": "string",
                "enum": ["short", "long", "all"],
                "description": "检索范围",
                "default": "all",
            },
            "limit": {
                "type": "integer",
                "description": "返回最大条目数",
                "default": DEFAULT_LIMIT,
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator", "memory_manager"],
)

registry.register(spec, read_memory)


__all__ = ["DEFAULT_LIMIT", "MAX_ENTRIES", "VALID_SCOPES", "read_memory", "spec"]
