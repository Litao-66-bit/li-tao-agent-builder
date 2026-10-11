"""工作区级「项目进度」记忆：**跨任务、持久、由后端自动维护**。

为什么要有（实测 2026-10-11，用户第二次报"还是没有记忆"）—— 三层根因：
1. 任务历史只存内存（``InMemoryTaskStore`` 首版不做持久化）→ **后端一重启全没**；
2. 前端的"续接"是拼一段字符串（卡 36）→ 只在 ``currentTaskId`` 存在时生效，
   刷新 / 切项目 / 重启后即失效；
3. 规划与执行提示词里**没有工作区现状** → 模型不知道上一个任务产出过什么、
   也不会去读它（用户原话："不会访问上一个对话它产出的文件"）。

所以把"这个工作区做过什么"做成**落在工作区里的持久记录**：

- 任务收尾时由**后端自动写入**（不依赖模型主动调 ``memory_write``）；
- 新任务创建时由**后端自动读取并注入提示词**（不依赖前端、不依赖重启）；
- 落点 ``<工作区>/.agent-memory/project_log.jsonl``（该目录已在 ``.gitignore``）。

设计口径：只记**客观事实**（需求原文 / 结论 / 产出文件 / 状态），不做推断；
**读侧全部容错**（文件不存在、读坏、单行坏都不致命）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_DIRNAME = ".agent-memory"
DEFAULT_PROJECT_LOG = "project_log.jsonl"
# 日志最多保留多少条（更早的靠任务列表回看）。
MAX_LOG_ENTRIES = 20
# 注入提示词的上限（与 routes 的 MAX_CONTEXT_CHARS 同一尺度）。
MAX_PROJECT_CONTEXT_CHARS = 4000
# 工作区文件清单的限制：**以条数封顶**，深度给足。
# 实测调参过程：深度 2 只看到 `outputs/paper_research_agent/`，深度 3 仍漏掉
# `outputs/paper_research_agent/web/index.html`（第 4 层）—— 而那正是"上一个任务产出的前端"。
# 真正的护栏是**条数**（80 条 ≈ 2~3KB），不是深度。
MAX_SNAPSHOT_ENTRIES = 80
MAX_SNAPSHOT_DEPTH = 5
# 清单里跳过的目录（运行时目录 + 缓存）。
SKIP_DIRS = frozenset(
    {
        ".git", "__pycache__", ".agent-tmp", ".agent-memory",
        ".pytest_cache", ".ruff_cache", ".venv", "node_modules",
    }
)


@dataclass(slots=True)
class ProjectLogEntry:
    """工作区里做过的一个任务（只记客观事实）。"""

    task_id: str
    requirement: str = ""
    title: str = ""
    status: str = ""
    conclusion: str = ""
    artifacts: list[str] = field(default_factory=list)
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_log_path(workspace: str | Path) -> Path:
    """项目日志路径（工作区内，随工作区走）。"""
    return Path(workspace) / DEFAULT_PROJECT_DIRNAME / DEFAULT_PROJECT_LOG


def read_project_log(workspace: str | Path) -> list[ProjectLogEntry]:
    """读工作区项目日志；**任何读取问题都返回空表，绝不抛**。"""
    try:
        raw = project_log_path(workspace).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    entries: list[ProjectLogEntry] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or not item.get("task_id"):
            continue
        entries.append(
            ProjectLogEntry(
                task_id=str(item["task_id"]),
                requirement=str(item.get("requirement") or ""),
                title=str(item.get("title") or ""),
                status=str(item.get("status") or ""),
                conclusion=str(item.get("conclusion") or ""),
                artifacts=[str(x) for x in (item.get("artifacts") or []) if x],
                updated_at=str(item.get("updated_at") or ""),
            )
        )
    return entries


def record_project_log(workspace: str | Path, entry: ProjectLogEntry) -> None:
    """按 ``task_id`` **upsert** 一条记录（同一任务多次收尾只留最新），写失败只记日志。"""
    if not entry.updated_at:
        entry.updated_at = _now()
    rows = [item for item in read_project_log(workspace) if item.task_id != entry.task_id]
    rows.append(entry)
    rows.sort(key=lambda item: item.updated_at)
    rows = rows[-MAX_LOG_ENTRIES:]
    path = project_log_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        body = "\n".join(json.dumps(asdict(row), ensure_ascii=False) for row in rows) + "\n"
        tmp.write_text(body, encoding="utf-8", newline="")
        tmp.replace(path)
    except OSError:
        logger.warning("项目日志写入失败（忽略）：%s", path, exc_info=True)


def build_project_context(
    entries: list[ProjectLogEntry], *, exclude_task_id: str = ""
) -> str:
    """把项目进度压成可注入提示词的一段（最新在前）；没有内容返回空串。

    ``exclude_task_id``：排除**当前这个任务**自己（新建任务时就已排除，防御性再排一次）。
    """
    # 跳过"什么都没干成"的记录（没有结论、也没有产出文件）：记忆里不该塞噪声，
    # 否则一个工作区跑过几次空任务之后，注入的上下文全是"状态：executing"这种废话。
    rows = [
        item
        for item in entries
        if item.task_id != exclude_task_id and (item.conclusion or item.artifacts)
    ]
    rows.sort(key=lambda item: item.updated_at, reverse=True)
    rows = rows[:MAX_LOG_ENTRIES]
    if not rows:
        return ""
    lines: list[str] = []
    for index, item in enumerate(rows, 1):
        lines.append(f"{index}. 需求：{item.requirement or item.title or '(无)'}")
        if item.title and item.requirement and item.title != item.requirement:
            lines.append(f"   标题：{item.title}")
        if item.conclusion:
            lines.append(f"   结论：{item.conclusion}")
        if item.artifacts:
            lines.append(f"   产出文件：{'、'.join(item.artifacts[:20])}")
        lines.append(f"   状态：{item.status or '未知'}")
    text = "\n".join(lines)
    if len(text) > MAX_PROJECT_CONTEXT_CHARS:
        text = text[:MAX_PROJECT_CONTEXT_CHARS] + f"…（本项目历史过长，已截断；原文 {len(text)} 字）"
    return text


def workspace_file_snapshot(
    workspace: str | Path,
    *,
    max_entries: int = MAX_SNAPSHOT_ENTRIES,
    max_depth: int = MAX_SNAPSHOT_DEPTH,
) -> str:
    """列出工作区**现有文件**（限深限条），供模型知道"已经有什么"。

    规划/执行时把这份清单给模型，它才不会把已经做完的事再排一遍、也不会从零重写
    （实测 2026-10-11：用户说"接着做前端"，模型不知道后端长什么样）。
    """
    root = Path(workspace)
    if not root.is_dir():
        return ""
    rows: list[str] = []

    def walk(folder: Path, depth: int, prefix: str) -> None:
        if depth > max_depth or len(rows) >= max_entries:
            return
        try:
            items = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for path in items:
            if len(rows) >= max_entries:
                return
            if path.name.startswith(".") or path.name in SKIP_DIRS:
                continue
            rel = f"{prefix}{path.name}"
            if path.is_dir():
                rows.append(f"{rel}/")
                walk(path, depth + 1, f"{rel}/")
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = None
                rows.append(rel if size is None else f"{rel} ({size}B)")

    walk(root, 1, "")
    if not rows:
        return "（空工作区：还没有任何产出）"
    if len(rows) > max_entries:
        rows = rows[:max_entries]
    if len(rows) >= max_entries:
        rows.append(f"…（仅列出前 {max_entries} 项）")
    return "\n".join(rows)


__all__ = [
    "DEFAULT_PROJECT_DIRNAME",
    "DEFAULT_PROJECT_LOG",
    "MAX_LOG_ENTRIES",
    "MAX_PROJECT_CONTEXT_CHARS",
    "ProjectLogEntry",
    "build_project_context",
    "project_log_path",
    "read_project_log",
    "record_project_log",
    "workspace_file_snapshot",
]
