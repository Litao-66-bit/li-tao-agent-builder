"""工作区级「已放行能力」记忆：跨任务、跨重启，**按工作区**记住用户的放行决定。

为什么要有（卡 44 验收标准 4）—— 现有 ``TaskEntry.approved_tools`` 有两个硬限制：

1. **只在内存**：后端一重启就没了（与内存密钥同源的坑）；
2. **只按任务**：新任务从空集合开始，于是同一类动作要**逐任务重批**
   （实测：用户每写一次文件就要点一次「放行」）。

所以把"这个工作区里，用户已经放行过哪些高风险能力"落成**工作区内的持久记录**：

- 落点 ``<工作区>/.agent-memory/approved_tools.json``（该目录已在 ``.gitignore``）；
- 与 ``project_log.py`` 同口径：**读侧全部容错**（文件不存在 / 读坏 / 形状不对都不致命），
  写侧原子替换 + 失败只记日志，绝不因为记账失败而中断任务；
- 只记**工具名**（如 ``file_delete``），不记"哪个任务的哪一步" —— 这是工作区级授权，
  与具体需求无关；删掉这个文件即等于撤销全部工作区授权。

安全口径：**越界写不在此列**。工作区边界由门卫路径沙箱硬拒绝（``E_PERMISSION``、
不可放行），所以这里没有、也不该有"把越界写放行掉"的开关。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: 与 project_log 共用同一个工作区元数据目录。
DEFAULT_APPROVAL_DIRNAME = ".agent-memory"
DEFAULT_APPROVAL_FILE = "approved_tools.json"

# 只认注册表里真实存在的工具名形状（防手改文件塞进奇怪内容）。
_MAX_TOOL_NAME = 64


def approval_path(workspace: str | Path) -> Path:
    """工作区级放行记录的路径。"""
    return Path(workspace) / DEFAULT_APPROVAL_DIRNAME / DEFAULT_APPROVAL_FILE


def _clean(tools: object) -> set[str]:
    """把任意输入收敛成合法的工具名集合（读侧容错）。"""
    if not isinstance(tools, (list, tuple, set, frozenset)):
        return set()
    cleaned: set[str] = set()
    for item in tools:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or len(name) > _MAX_TOOL_NAME:
            continue
        cleaned.add(name)
    return cleaned


def read_workspace_approvals(workspace: str | Path) -> set[str]:
    """读该工作区已放行的工具集合；**任何读取问题都返回空集，绝不抛**。"""
    try:
        raw = approval_path(workspace).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, dict):
        return set()
    return _clean(payload.get("approved_tools"))


def grant_workspace_approvals(workspace: str | Path, tools: set[str]) -> set[str]:
    """把 ``tools`` 并入工作区放行记录（**累积**），返回合并后的集合。

    写失败只记日志并返回当前已读到的集合 —— 任务照常执行，只是"下次不再问"没记住。
    """
    granted = read_workspace_approvals(workspace) | _clean(tools)
    if not granted:
        return granted
    path = approval_path(workspace)
    body = json.dumps({"approved_tools": sorted(granted)}, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8", newline="")
        tmp.replace(path)
    except OSError:
        logger.warning("工作区放行记录写入失败（忽略）：%s", path, exc_info=True)
    return granted


def revoke_workspace_approvals(workspace: str | Path) -> None:
    """清空该工作区的放行记录（供显式撤销；文件不存在也算成功）。"""
    try:
        approval_path(workspace).unlink()
    except OSError:
        return


__all__ = [
    "DEFAULT_APPROVAL_DIRNAME",
    "DEFAULT_APPROVAL_FILE",
    "approval_path",
    "grant_workspace_approvals",
    "read_workspace_approvals",
    "revoke_workspace_approvals",
]
