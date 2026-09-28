"""默认角色权限矩阵（最小权限原则）。

门卫按此表放行：未列入 allowed_tools 的一律拒绝；high_risk_tools 需审批。
新增工具时，在此给真正需要的角色加工具名；不要给所有角色全开。
"""

from __future__ import annotations

from agent_builder.contracts.schemas import RolePerm

# 角色 → 权限（按需扩展；未列出的角色调用任何工具都会被门卫拒绝）
DEFAULT_ROLE_PERMS: dict[str, RolePerm] = {
    "conductor": RolePerm(
        role="conductor",
        allowed_tools=[
            "plan_validate",
            "approval_request",
            "change_notify",
            "audit_log",
            "memory_read",
            "config_read",
        ],
        high_risk_tools=[],
        notes="总指挥：编排任务全程、维护状态机、管理返工/中断/审批；只编排不执行，不可直接写文件/提交代码",
    ),
    "decomposer": RolePerm(
        role="decomposer",
        allowed_tools=[
            "config_read",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="分解器：拆需求为步骤DAG、去重/标注依赖/分组/歧义检查；只规划不执行",
    ),
    "operator": RolePerm(
        role="operator",
        allowed_tools=[
            "file_read",
            "file_list",
            "code_search",
            "file_write",
            "web_fetch",
            "web_search",
            "citation_check",
            "sandbox_run",
            "test_run",
            "data_query",
            "plan_validate",
            "memory_read",
            "audit_log",
            "metric_collect",
            "config_read",
            "diff_preview",
            "approval_request",
            "change_notify",
            "git_log",
        ],
        high_risk_tools=["file_write"],
        notes="操作者：可读/列白名单文件、代码搜索、抓取/搜索网页、校验引用、沙箱执行、跑测试、读数据、校验计划DAG、只读记忆、写审计日志、采集指标、读配置、生成diff预览、发起审批、变更通知、查git历史；写文件需审批",
    ),
    "sub_architect": RolePerm(
        role="sub_architect",
        allowed_tools=["git_commit", "rollback", "git_log"],
        high_risk_tools=["git_commit", "rollback"],
        notes="副架构师：提交版本/回滚版本/查git历史；提交和回滚需审批",
    ),
    "memory_manager": RolePerm(
        role="memory_manager",
        allowed_tools=[
            "memory_read",
            "memory_write",
            "memory_forget",
        ],
        high_risk_tools=[],
        notes="记忆管家：读写/清理记忆；敏感信息加密存储（专用角色，operator 不可写/清记忆）",
    ),
}


def get_default_role_perms() -> dict[str, RolePerm]:
    """返回默认权限矩阵的深拷贝（避免调用方修改全局状态）。"""
    return {role: perm.model_copy(deep=True) for role, perm in DEFAULT_ROLE_PERMS.items()}


__all__ = ["DEFAULT_ROLE_PERMS", "get_default_role_perms"]
