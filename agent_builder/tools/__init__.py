"""工具层：门卫（唯一执行出口）+ 注册表 + 权限矩阵预置。"""

from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS, get_default_role_perms
from agent_builder.tools.registry import registry
from agent_builder.tools.spec import ToolSpec

# 触发各工具模块的注册（导入即注册到 registry）
from agent_builder.tools import impl  # noqa: F401

__all__ = [
    "DEFAULT_ROLE_PERMS",
    "ToolGatekeeper",
    "ToolSpec",
    "get_default_role_perms",
    "registry",
]
