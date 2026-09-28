"""config_read：读架构配置。

安全边界：只读 permissions/registry，无副作用，无审批。
"""

from __future__ import annotations

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.permissions import get_default_role_perms
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 支持的配置段。
VALID_SECTIONS = frozenset({"roles", "tools", "all"})


def read_config(section: str = "all") -> str:
    """读架构配置。

    Args:
        section: ``roles`` / ``tools`` / ``all``。

    Returns:
        配置文本：roles 段列出角色权限矩阵；tools 段列出工具规格。

    Raises:
        AgentError(E_VALIDATION): section 非法。
    """
    cid = current_correlation_id.get()
    if section not in VALID_SECTIONS:
        raise validation_error(
            f"config_read: section 非法 {section!r}，可选: {sorted(VALID_SECTIONS)}",
            source="tool.config_read",
            correlation_id=cid,
        )

    parts: list[str] = []
    if section in ("roles", "all"):
        parts.append("## Roles")
        perms = get_default_role_perms()
        for role, perm in perms.items():
            parts.append(f"### {role}")
            parts.append(f"  allowed_tools: {', '.join(perm.allowed_tools)}")
            if perm.high_risk_tools:
                parts.append(f"  high_risk_tools: {', '.join(perm.high_risk_tools)}")
            else:
                parts.append("  high_risk_tools: (none)")
            if perm.notes:
                parts.append(f"  notes: {perm.notes}")
    if section in ("tools", "all"):
        parts.append("## Tools")
        for name in registry.list_tools():
            entry = registry.get(name)
            if entry is None:
                continue
            spec, _ = entry
            parts.append(f"### {name}")
            parts.append(f"  risk: {spec.risk_level}, timeout: {spec.timeout_s}s, cost: {spec.cost_band}")
            parts.append(f"  roles: {', '.join(spec.allowed_roles)}")

    return "\n".join(parts) if parts else "(no config)"


spec = ToolSpec(
    name="config_read",
    description="读架构配置（角色权限矩阵 + 工具规格）",
    parameters={
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "enum": ["roles", "tools", "all"],
                "description": "配置段",
                "default": "all",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, read_config)


__all__ = ["VALID_SECTIONS", "read_config", "spec"]
