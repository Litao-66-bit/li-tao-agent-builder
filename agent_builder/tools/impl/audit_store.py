"""审计日志存储后端（audit_log / metric_collect 共享）。

不注册为工具，只供上述两个模块导入。
存储层：JSON 文件，结构 [{"id", "ts", "role", "action", "detail", "correlation_id"}, ...]。
只追加不可篡改：append_audit 只追加，不提供修改/删除接口。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_builder.contracts.errors import tool_error
from agent_builder.tools.gatekeeper import current_workspace_dir
from agent_builder.tools.registry import current_correlation_id

# 默认审计日志位置：**当前工作区内**的隐藏目录（不是用户主目录）。
# 同 memory_store：audit_log / metric_collect 没有 path 参数、不走沙箱校验，
# 默认落在工作区外就等于绕过边界去写用户主目录（且目录名以 ``.`` 开头 → 产物树跳过）。
DEFAULT_AUDIT_DIRNAME = ".agent-audit"
DEFAULT_AUDIT_FILE = "audit.json"


def default_audit_dir() -> Path:
    """默认审计目录：当前工作区（沙箱基准）下的 ``.agent-audit``；无上下文时退回 cwd。"""
    root = current_workspace_dir.get() or Path.cwd()
    return root / DEFAULT_AUDIT_DIRNAME


def _get_audit_path() -> Path:
    """返回当前审计日志文件路径（测试时可 monkeypatch）。"""
    env = os.environ.get("AGENT_AUDIT_FILE")
    if env:
        return Path(env)
    target = default_audit_dir() / DEFAULT_AUDIT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def load_audit() -> list[dict[str, Any]]:
    """加载审计日志。文件不存在或为空返回空列表。"""
    path = _get_audit_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        cid = current_correlation_id.get()
        raise tool_error(
            f"audit_store: 审计日志文件损坏（JSON 解析失败）: {exc}",
            source="tool.audit_store",
            correlation_id=cid,
        ) from exc
    except OSError as exc:
        cid = current_correlation_id.get()
        raise tool_error(
            f"audit_store: 读取审计日志失败: {exc}",
            source="tool.audit_store",
            correlation_id=cid,
        ) from exc
    if not isinstance(data, list):
        return []
    return data


def append_audit(
    role: str, action: str, detail: str, correlation_id: str
) -> str:
    """追加一条审计记录（只追加，不修改已有记录）。

    Returns:
        新记录的 id（如 ``a-000001``）。
    """
    path = _get_audit_path()
    entries = load_audit()
    record_id = f"a-{len(entries) + 1:06d}"
    entry: dict[str, Any] = {
        "id": record_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "action": action,
        "detail": detail,
        "correlation_id": correlation_id,
    }
    entries.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        cid = current_correlation_id.get()
        raise tool_error(
            f"audit_store: 写入审计日志失败: {exc}",
            source="tool.audit_store",
            correlation_id=cid,
        ) from exc
    return record_id


__all__ = [
    "DEFAULT_AUDIT_DIRNAME",
    "DEFAULT_AUDIT_FILE",
    "_get_audit_path",
    "append_audit",
    "default_audit_dir",
    "load_audit",
]
