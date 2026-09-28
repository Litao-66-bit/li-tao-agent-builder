"""git_commit：提交版本（合并生产）。

安全边界：副架构专用，高风险需审批。门卫校验 repo_path 沙箱 + 审批标记。
"""

from __future__ import annotations

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.impl.git_ops import run_git
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

MAX_MESSAGE_CHARS = 500


def commit_version(
    repo_path: str, message: str, files: list[str] | None = None
) -> str:
    """提交版本到 git 仓库。

    Args:
        repo_path: 仓库路径（沙箱内）。
        message: 提交信息。
        files: 指定文件列表（空则 git add -A 全部）。

    Returns:
        确认消息：``committed <hash>``。

    Raises:
        AgentError(E_VALIDATION): message 为空 / 超长。
        AgentError(E_TOOL): git 命令失败（由 git_ops 抛出）。
    """
    cid = current_correlation_id.get()
    if not message or not message.strip():
        raise validation_error(
            "git_commit: message 不能为空",
            source="tool.git_commit",
            correlation_id=cid,
        )
    if len(message) > MAX_MESSAGE_CHARS:
        raise validation_error(
            f"git_commit: message 长度 {len(message)} 超过上限 {MAX_MESSAGE_CHARS}",
            source="tool.git_commit",
            correlation_id=cid,
        )

    add_args = ["add"] + (files if files else ["-A"])
    run_git(repo_path, add_args)
    run_git(repo_path, ["commit", "-m", message.strip()])
    head = run_git(repo_path, ["rev-parse", "HEAD"]).strip()
    return f"committed {head}"


spec = ToolSpec(
    name="git_commit",
    description="提交版本（合并生产），副架构专用，需审批",
    parameters={
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "仓库路径（沙箱内）"},
            "message": {"type": "string", "description": "提交信息"},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "指定文件（空则全部）",
            },
        },
        "required": ["repo_path", "message"],
        "additionalProperties": False,
    },
    risk_level="high",
    timeout_s=30.0,
    cost_band="medium",
    allowed_roles=["sub_architect"],
)

registry.register(spec, commit_version)


__all__ = ["MAX_MESSAGE_CHARS", "commit_version", "spec"]
