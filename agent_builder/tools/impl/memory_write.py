"""memory_write：写入记忆（由记忆管家专用）。

安全边界：敏感信息 base64 编码存储（存储层加密，防文件直接读取泄露明文）。
门卫只做角色权限校验（allowed_roles=["memory_manager"]）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.impl.memory_store import encode_sensitive, load_memory, save_memory
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单条记忆 content 最大字符数。
MAX_CONTENT_CHARS = 10_000
# 支持的 scope 值。
VALID_SCOPES = frozenset({"short", "long"})


def write_memory(
    key: str,
    content: str,
    scope: str = "long",
    sensitive: bool = False,
) -> str:
    """写入一条记忆。

    Args:
        key: 记忆键名（唯一标识）。
        content: 记忆内容。
        scope: ``short``（短期）/ ``long``（长期）。
        sensitive: 为 true 时 content base64 编码存储（存储层加密）。

    Returns:
        确认消息：``stored <key> (<scope>, sensitive=<bool>)``。

    Raises:
        AgentError(E_VALIDATION): key/content 为空 / scope 非法 / content 超长。
        AgentError(E_TOOL): 存储读写失败（由 memory_store 抛出）。
    """
    cid = current_correlation_id.get()
    if not key or not key.strip():
        raise validation_error(
            "memory_write: key 不能为空",
            source="tool.memory_write",
            correlation_id=cid,
        )
    if not content:
        raise validation_error(
            "memory_write: content 不能为空",
            source="tool.memory_write",
            correlation_id=cid,
        )
    if scope not in VALID_SCOPES:
        raise validation_error(
            f"memory_write: scope 非法 {scope!r}，可选: {sorted(VALID_SCOPES)}",
            source="tool.memory_write",
            correlation_id=cid,
        )
    if len(content) > MAX_CONTENT_CHARS:
        raise validation_error(
            f"memory_write: content 长度 {len(content)} 超过上限 {MAX_CONTENT_CHARS}",
            source="tool.memory_write",
            correlation_id=cid,
        )

    data = load_memory()
    data.setdefault(scope, [])

    # 同 key 覆盖：移除旧的同 key 条目（同 scope 内）。
    data[scope] = [e for e in data[scope] if str(e.get("key", "")) != key]

    stored_content = encode_sensitive(content) if sensitive else content
    entry = {
        "key": key,
        "content": stored_content,
        "scope": scope,
        "sensitive": sensitive,
        "encrypted": sensitive,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    data[scope].append(entry)
    save_memory(data)

    return f"stored {key} ({scope}, sensitive={sensitive})"


spec = ToolSpec(
    name="memory_write",
    description="写入记忆（敏感信息加密存储，由记忆管家专用）",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "记忆键名（同 scope 内唯一）"},
            "content": {"type": "string", "description": "记忆内容"},
            "scope": {
                "type": "string",
                "enum": ["short", "long"],
                "description": "短期 / 长期",
                "default": "long",
            },
            "sensitive": {
                "type": "boolean",
                "description": "为 true 时 content 加密存储",
                "default": False,
            },
        },
        "required": ["key", "content"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["memory_manager"],
)

registry.register(spec, write_memory)


__all__ = [
    "MAX_CONTENT_CHARS",
    "VALID_SCOPES",
    "spec",
    "write_memory",
]
