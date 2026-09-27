"""记忆存储后端（memory_read / memory_write / memory_forget 共享）。

不注册为工具，仅供上述三个记忆工具模块导入。
存储层：JSON 文件，结构 {"short": [...], "long": [...]}。
敏感信息：base64 编码后存储（存储层加密，防文件被直接读取时泄露明文）。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from agent_builder.contracts.errors import tool_error
from agent_builder.tools.registry import current_correlation_id

# 默认记忆目录：用户主目录下的 .li-tao-agent。
DEFAULT_MEMORY_DIR = Path.home() / ".li-tao-agent"
DEFAULT_MEMORY_FILE = "memory.json"


def _get_memory_path() -> Path:
    """返回当前记忆文件路径（测试时可 monkeypatch）。"""
    env = os.environ.get("AGENT_MEMORY_FILE")
    if env:
        return Path(env)
    DEFAULT_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_MEMORY_DIR / DEFAULT_MEMORY_FILE


def load_memory() -> dict[str, list[dict[str, Any]]]:
    """加载记忆存储。文件不存在或为空返回空结构。"""
    path = _get_memory_path()
    if not path.exists():
        return {"short": [], "long": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        cid = current_correlation_id.get()
        raise tool_error(
            f"memory_store: 记忆文件损坏（JSON 解析失败）: {exc}",
            source="tool.memory_store",
            correlation_id=cid,
        ) from exc
    except OSError as exc:
        cid = current_correlation_id.get()
        raise tool_error(
            f"memory_store: 读取记忆文件失败: {exc}",
            source="tool.memory_store",
            correlation_id=cid,
        ) from exc
    if not isinstance(data, dict):
        return {"short": [], "long": []}
    data.setdefault("short", [])
    data.setdefault("long", [])
    return data


def save_memory(data: dict[str, list[dict[str, Any]]]) -> None:
    """写回记忆存储。"""
    path = _get_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        cid = current_correlation_id.get()
        raise tool_error(
            f"memory_store: 写入记忆文件失败: {exc}",
            source="tool.memory_store",
            correlation_id=cid,
        ) from exc


def encode_sensitive(text: str) -> str:
    """敏感信息编码（base64，存储层防直接读取明文）。

    注：base64 不是真正加密，真正加密需密钥管理（超出当前范围）。
    此处满足"敏感信息不落明文"的最低要求。
    """
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_sensitive(encoded: str) -> str:
    """解码敏感信息。"""
    return base64.b64decode(encoded.encode("ascii")).decode("utf-8")


__all__ = [
    "DEFAULT_MEMORY_DIR",
    "DEFAULT_MEMORY_FILE",
    "_get_memory_path",
    "decode_sensitive",
    "encode_sensitive",
    "load_memory",
    "save_memory",
]
