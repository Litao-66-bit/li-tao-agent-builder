"""默认角色权限矩阵（最小权限原则）。

门卫按此表放行：未列入 allowed_tools 的一律拒绝；high_risk_tools 需审批。
新增工具时，在此给真正需要的角色加工具名；不要给所有角色全开。
"""

from __future__ import annotations

from agent_builder.contracts.schemas import RolePerm

# 角色 → 权限（按需扩展；未列出的角色调用任何工具都会被门卫拒绝）
DEFAULT_ROLE_PERMS: dict[str, RolePerm] = {
    "operator": RolePerm(
        role="operator",
        allowed_tools=["file_read", "file_list"],
        high_risk_tools=[],
        notes="操作者：可读取白名单内文件和列目录；无写权限",
    ),
}


def get_default_role_perms() -> dict[str, RolePerm]:
    """返回默认权限矩阵的深拷贝（避免调用方修改全局状态）。"""
    return {role: perm.model_copy(deep=True) for role, perm in DEFAULT_ROLE_PERMS.items()}


__all__ = ["DEFAULT_ROLE_PERMS", "get_default_role_perms"]
