"""git_log：查询版本历史。

安全边界：只读，无审批。门卫校验 repo_path 沙箱。
"""

from __future__ import annotations

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.impl.git_ops import run_git
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

DEFAULT_LIMIT = 10
MAX_LIMIT = 100


def query_log(
    repo_path: str, limit: int = DEFAULT_LIMIT, oneline: bool = True
) -> str:
    """查询 git 版本历史。

    Args:
        repo_path: 仓库路径（沙箱内）。
        limit: 返回条数（上限 MAX_LIMIT）。
        oneline: 单行格式（--oneline）。

    Returns:
        git log 输出文本；无 commit 返回 ``(no commits)``。

    Raises:
        AgentError(E_VALIDATION): limit 非正。
        AgentError(E_TOOL): git 命令失败（由 git_ops 抛出）。
    """
    cid = current_correlation_id.get()
    if limit <= 0:
        raise validation_error(
            f"git_log: limit 必须为正数: {limit}",
            source="tool.git_log",
            correlation_id=cid,
        )
    limit = min(limit, MAX_LIMIT)
    args = ["log", f"-n{limit}"]
    if oneline:
        args.append("--oneline")
    output = run_git(repo_path, args)
    return output.strip() if output.strip() else "(no commits)"


spec = ToolSpec(
    name="git_log",
    description="查询版本历史（git log，只读）",
    parameters={
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "仓库路径（沙箱内）"},
            "limit": {
                "type": "integer",
                "description": "返回条数",
                "default": DEFAULT_LIMIT,
            },
            "oneline": {
                "type": "boolean",
                "description": "单行格式",
                "default": True,
            },
        },
        "required": ["repo_path"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=15.0,
    cost_band="low",
    allowed_roles=["operator", "sub_architect"],
)

registry.register(spec, query_log)


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "query_log", "spec"]
