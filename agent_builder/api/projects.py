"""项目（工作区）注册表。

把「工作区根目录」从模块常量（gatekeeper.WORKSPACE_DIR）提升为可切换的项目实体：
- 每个项目 = 一个本地目录（绝对路径）；当前项目决定 agent 与文件树的工作区；
- 进程内注册 + 落盘 ``data/projects.json``（含本机绝对路径，故不纳入版本控制）；
- 文件级访问边界仍由 ``routes._safe_workspace_path`` 保证（只接受工作区相对路径 + 防穿越）；
  本模块负责「工作区根目录本身」是否允许。

边界规则（已与用户确认）：
- 任意本地目录可选，但禁止驱动器根与系统保护目录（黑名单）；
- 新建文件夹：先选父目录（同样受边界校验），再输入子目录名。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# ── 常量 ─────────────────────────────────────────────────────────

# Windows 系统保护目录（相对系统盘根）。
_WINDOWS_BLOCKED_NAMES: tuple[str, ...] = (
    "$Recycle.Bin",
    "System Volume Information",
    "Recovery",
    "PerfLogs",
)

# POSIX 系统保护目录。
_POSIX_BLOCKED: tuple[str, ...] = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    "/var/lib",
)

# 文件夹名非法字符（Windows 保留字符 + 路径分隔符）与长度上限。
_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')
MAX_FOLDER_NAME_CHARS = 64

# 系统文件夹选择对话框的最长等待时间（秒）：用户在对话框停留时间计入此上限。
PICK_DIALOG_TIMEOUT_S = 600


def _now() -> str:
    """当前 UTC 时间的 ISO 字符串（秒级精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_workspace_dir() -> Path:
    """默认工作区目录。

    优先 gatekeeper.WORKSPACE_DIR；该路径不存在时（如 Windows 开发环境）
    回退到项目根目录。
    """
    from agent_builder.tools.gatekeeper import WORKSPACE_DIR

    if WORKSPACE_DIR.exists():
        return WORKSPACE_DIR.resolve()
    # agent_builder/api/projects.py → 上溯 2 层 = 项目根。
    return Path(__file__).resolve().parents[2]


def default_data_file() -> Path:
    """项目注册表的落盘位置（项目根 data/projects.json）。

    可用环境变量 ``AGENT_BUILDER_PROJECTS_FILE`` 覆盖（测试隔离用）。
    """
    override = os.environ.get("AGENT_BUILDER_PROJECTS_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "data" / "projects.json"


def _blocked_dirs() -> list[Path]:
    """系统保护目录列表（按当前平台）。"""
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        blocked = [
            system_root,
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
            Path(os.environ.get("ProgramData", r"C:\ProgramData")),
        ]
        drive = system_root.drive or "C:"
        blocked.extend(Path(f"{drive}/{name}") for name in _WINDOWS_BLOCKED_NAMES)
        return blocked
    return [Path(p) for p in _POSIX_BLOCKED]


def validate_workspace_root(raw_path: str) -> Path:
    """校验并规范化「工作区根目录」；不合法抛 ValueError（由路由层转 400）。

    规则：绝对路径 · 存在 · 是目录 · 非驱动器根 · 不落在系统保护目录内。
    """
    text = (raw_path or "").strip()
    if not text:
        raise ValueError("路径不能为空")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError("必须是绝对路径")
    resolved = candidate.resolve()
    if not resolved.exists():
        raise ValueError(f"目录不存在：{text}")
    if not resolved.is_dir():
        raise ValueError(f"不是目录：{text}")
    if resolved.parent == resolved:
        raise ValueError("不允许把驱动器根/文件系统根作为工作区")
    for blocked in _blocked_dirs():
        try:
            blocked_resolved = blocked.resolve()
        except OSError:
            continue
        if resolved == blocked_resolved or resolved.is_relative_to(blocked_resolved):
            raise ValueError(f"该目录属于系统保护目录，禁止作为工作区：{text}")
    return resolved


def create_workspace_folder(parent_raw: str, name: str) -> Path:
    """在已校验的父目录下新建子目录，返回新目录路径。

    父目录同样走 ``validate_workspace_root``（边界一致）。
    """
    parent = validate_workspace_root(parent_raw)
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("文件夹名不能为空")
    if cleaned in (".", "..") or cleaned.startswith("."):
        raise ValueError("文件夹名不能以点开头")
    if any(char in _INVALID_NAME_CHARS for char in cleaned):
        raise ValueError('文件夹名不能包含 < > : " / \\ | ? * 等字符')
    if len(cleaned) > MAX_FOLDER_NAME_CHARS:
        raise ValueError(f"文件夹名过长（最多 {MAX_FOLDER_NAME_CHARS} 字符）")
    target = parent / cleaned
    if target.exists():
        raise ValueError(f"同名文件夹已存在：{cleaned}")
    try:
        target.mkdir()
    except OSError as exc:
        raise ValueError(f"创建文件夹失败：{cleaned}") from exc
    return target.resolve()


# ── 系统文件夹选择对话框 ─────────────────────────────────────────

_POWERSHELL_FOLDER_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
    "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
    "$dialog.Description = '选择工作区文件夹'; "
    "$dialog.ShowNewFolderButton = $true; "
    "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
    "Write-Output $dialog.SelectedPath }"
)


def _pick_with_tkinter(initial_dir: str | None) -> str | None:
    """进程内 tkinter 对话框（跨平台）。GUI 不可用时抛异常由上层回退。"""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.update()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=initial_dir or "",
            mustexist=True,
            title="选择工作区文件夹",
        )
    finally:
        root.destroy()
    return selected or None


def _pick_with_powershell() -> str | None:
    """Windows 回退方案：独立 PowerShell 进程 + WinForms（STA 线程）。"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", _POWERSHELL_FOLDER_SCRIPT],
        capture_output=True,
        text=True,
        timeout=PICK_DIALOG_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or "文件夹选择对话框调用失败")
    return (proc.stdout or "").strip() or None


def pick_directory(initial_dir: str | None = None) -> str | None:
    """弹系统文件夹选择对话框，返回绝对路径；用户取消返回 None。

    优先 tkinter（进程内）；失败时 Windows 回退 PowerShell + WinForms。
    两种方式都不可用则抛 RuntimeError。
    """
    try:
        return _pick_with_tkinter(initial_dir)
    except Exception as exc:  # noqa: BLE001 - GUI 不可用时需静默回退
        last_error: Exception = exc
    if os.name == "nt":
        try:
            return _pick_with_powershell()
        except Exception as exc:  # noqa: BLE001 - 回退失败统一报错
            last_error = exc
    raise RuntimeError(f"无法打开系统文件夹选择对话框：{last_error}")


# ── 项目实体与注册表 ─────────────────────────────────────────────


@dataclass(slots=True)
class Project:
    """一个项目 = 一个本地工作区目录。"""

    project_id: str
    name: str
    path: str
    created_at: str
    last_opened_at: str


class ProjectStore:
    """项目注册表：线程安全，变更即落盘（落盘失败不影响内存态）。"""

    DEFAULT_ID = "default"

    def __init__(self, data_file: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._data_file = data_file or default_data_file()
        self._projects: dict[str, Project] = {}
        self._current_id: str | None = None
        self._load()

    # ── 查询 ──

    def list_projects(self) -> list[Project]:
        """按最近打开时间倒序返回全部项目。"""
        with self._lock:
            return sorted(
                self._projects.values(), key=lambda item: item.last_opened_at, reverse=True
            )

    def get(self, project_id: str) -> Project | None:
        with self._lock:
            return self._projects.get(project_id)

    def current(self) -> Project | None:
        """当前项目；未显式切换过时回落到默认工作区项目。"""
        with self._lock:
            if self._current_id:
                project = self._projects.get(self._current_id)
                if project is not None:
                    return project
            return self._projects.get(self.DEFAULT_ID)

    def current_workspace_dir(self) -> Path:
        """当前工作区目录；项目目录已失效时回退默认工作区。"""
        project = self.current()
        if project is not None:
            path = Path(project.path)
            if path.is_dir():
                return path.resolve()
        return default_workspace_dir()

    # ── 变更 ──

    def register(self, path: Path) -> Project:
        """登记目录为项目并设为当前；同路径已存在则复用（仅刷新最近打开时间）。"""
        stored = str(path)
        with self._lock:
            for project in self._projects.values():
                if Path(project.path) == path:
                    project.last_opened_at = _now()
                    self._current_id = project.project_id
                    self._save()
                    return project
            project = Project(
                project_id=uuid.uuid4().hex[:8],
                name=self._unique_name(path.name or stored),
                path=stored,
                created_at=_now(),
                last_opened_at=_now(),
            )
            self._projects[project.project_id] = project
            self._current_id = project.project_id
            self._save()
            return project

    def open(self, project_id: str) -> Project:
        """切换到指定项目；不存在抛 KeyError（由路由层转 404）。"""
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise KeyError(project_id)
            project.last_opened_at = _now()
            self._current_id = project_id
            self._save()
            return project

    def _unique_name(self, base: str) -> str:
        """项目重名时追加序号，便于在列表里区分。"""
        existing = {project.name for project in self._projects.values()}
        if base not in existing:
            return base
        index = 2
        while f"{base} ({index})" in existing:
            index += 1
        return f"{base} ({index})"

    # ── 持久化 ──

    def _load(self) -> None:
        try:
            data = json.loads(self._data_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            for item in data.get("projects") or []:
                try:
                    project = Project(**item)
                except TypeError:
                    continue
                self._projects[project.project_id] = project
            current = data.get("current")
            if isinstance(current, str) and current in self._projects:
                self._current_id = current
        # 默认工作区项目始终存在，保证「使用默认工作区」可用。
        if self.DEFAULT_ID not in self._projects:
            self._projects[self.DEFAULT_ID] = Project(
                project_id=self.DEFAULT_ID,
                name="默认工作区",
                path=str(default_workspace_dir()),
                created_at=_now(),
                last_opened_at=_now(),
            )

    def _save(self) -> None:
        payload = {
            "current": self._current_id,
            "projects": [asdict(project) for project in self._projects.values()],
        }
        try:
            self._data_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._data_file.with_name(f"{self._data_file.name}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._data_file)
        except OSError:
            # 落盘失败不阻断本次会话（内存态仍可用）。
            pass


__all__ = [
    "MAX_FOLDER_NAME_CHARS",
    "PICK_DIALOG_TIMEOUT_S",
    "Project",
    "ProjectStore",
    "create_workspace_folder",
    "default_data_file",
    "default_workspace_dir",
    "pick_directory",
    "validate_workspace_root",
]
