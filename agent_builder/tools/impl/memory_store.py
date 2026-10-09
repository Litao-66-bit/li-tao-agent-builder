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
from agent_builder.tools.gatekeeper import current_workspace_dir
from agent_builder.tools.registry import current_correlation_id

# 默认记忆位置：**当前工作区内**的隐藏目录（不是用户主目录）。
#
# 此前默认是 ``Path.home()/".li-tao-agent"`` —— 落在工作区之外，而 memory_* 这类工具
# **没有 path 参数、也不走门卫的沙箱校验**，于是它们直接读写了工作区外的用户主目录：
# 实测在 Windows 上直接 ``WinError 5 拒绝访问``（模型只看到「读取记忆失败：权限不足」），
# 而且读之前还会在用户主目录 ``mkdir``。放到工作区里既守住边界，也保证可读写。
# 目录名以 ``.`` 开头 → 右栏产物树跳过隐藏目录，不打扰用户。
DEFAULT_MEMORY_DIRNAME = ".agent-memory"
DEFAULT_MEMORY_FILE = "memory.json"


def default_memory_dir() -> Path:
    """默认记忆目录：当前工作区（沙箱基准）下的 ``.agent-memory``；无上下文时退回 cwd。"""
    root = current_workspace_dir.get() or Path.cwd()
    return root / DEFAULT_MEMORY_DIRNAME


def _get_memory_path() -> Path:
    """返回当前记忆文件路径（测试时可 monkeypatch）。"""
    env = os.environ.get("AGENT_MEMORY_FILE")
    if env:
        return Path(env)
    target = default_memory_dir() / DEFAULT_MEMORY_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


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
    "DEFAULT_MEMORY_DIRNAME",
    "DEFAULT_MEMORY_FILE",
    "_get_memory_path",
    "decode_sensitive",
    "default_memory_dir",
    "encode_sensitive",
    "load_memory",
    "save_memory",
]
