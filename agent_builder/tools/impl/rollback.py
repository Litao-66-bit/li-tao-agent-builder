"""rollback：回滚版本。

安全边界：副架构专用，高风险需审批。门卫校验 repo_path 沙箱 + 审批标记。
"""

from __future__ import annotations

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.impl.git_ops import run_git
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec


def rollback_version(repo_path: str, target: str) -> str:
    """回滚到指定 commit（git reset --hard）。

    Args:
        repo_path: 仓库路径（沙箱内）。
        target: 目标 commit hash 或 ref（如 HEAD~1）。

    Returns:
        确认消息：``rolled back: <old> -> <new>``。

    Raises:
        AgentError(E_VALIDATION): target 为空。
        AgentError(E_TOOL): git 命令失败（由 git_ops 抛出）。
    """
    cid = current_correlation_id.get()
    if not target or not target.strip():
        raise validation_error(
            "rollback: target 不能为空",
            source="tool.rollback",
            correlation_id=cid,
        )

    old_head = run_git(repo_path, ["rev-parse", "HEAD"]).strip()
    run_git(repo_path, ["reset", "--hard", target.strip()])
    new_head = run_git(repo_path, ["rev-parse", "HEAD"]).strip()
    return f"rolled back: {old_head[:8]} -> {new_head[:8]}"


spec = ToolSpec(
    name="rollback",
    description="回滚版本（git reset --hard），副架构专用，需审批",
    parameters={
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "仓库路径（沙箱内）"},
            "target": {"type": "string", "description": "目标 commit hash 或 ref"},
        },
        "required": ["repo_path", "target"],
        "additionalProperties": False,
    },
    risk_level="high",
    timeout_s=30.0,
    cost_band="medium",
    allowed_roles=["sub_architect"],
)

registry.register(spec, rollback_version)


__all__ = ["rollback_version", "spec"]
