"""Git 操作共享后端（git_commit / rollback / git_log 共享）。

不注册为工具，只供上述模块导入。
封装 git 命令执行，二次校验 repo 路径在沙箱内（门卫已校验，纵深防御）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.gatekeeper import WORKSPACE_DIR
from agent_builder.tools.registry import current_correlation_id

GIT_BIN = "git"
GIT_TIMEOUT = 30.0


def validate_repo_path(repo_path: str) -> Path:
    """二次校验 repo 路径在沙箱白名单内（门卫已校验，纵深防御）。"""
    cid = current_correlation_id.get()
    if not repo_path or not repo_path.strip():
        raise validation_error(
            "git_ops: repo_path 不能为空",
            source="tool.git_ops",
            correlation_id=cid,
        )
    candidate = Path(repo_path).resolve()
    try:
        candidate.relative_to(WORKSPACE_DIR.resolve())
    except ValueError:
        raise validation_error(
            f"git_ops: repo 路径 {repo_path} 超出沙箱白名单",
            source="tool.git_ops",
            correlation_id=cid,
        ) from None
    return candidate


def run_git(repo_path: str, args: list[str]) -> str:
    """在 repo_path 执行 git 命令，返回 stdout。失败抛 E_TOOL。"""
    repo = validate_repo_path(repo_path)
    cmd = [GIT_BIN, "-C", str(repo)] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        cid = current_correlation_id.get()
        raise tool_error(
            f"git_ops: git 命令未找到: {exc}",
            source="tool.git_ops",
            correlation_id=cid,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        cid = current_correlation_id.get()
        raise tool_error(
            f"git_ops: git 命令超时（>{GIT_TIMEOUT}s）",
            source="tool.git_ops",
            correlation_id=cid,
        ) from exc
    if proc.returncode != 0:
        cid = current_correlation_id.get()
        raise tool_error(
            f"git_ops: git 命令失败: {proc.stderr.strip()}",
            source="tool.git_ops",
            correlation_id=cid,
        )
    return proc.stdout


__all__ = ["GIT_BIN", "GIT_TIMEOUT", "run_git", "validate_repo_path"]
