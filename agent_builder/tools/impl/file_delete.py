"""file_delete：删除白名单目录内的单个文件（不递归、不删目录）。

安全边界：
1. 路径沙箱校验由 ToolGatekeeper 负责（realpath 必须在 WORKSPACE_DIR 内）。
2. 属破坏性写操作：`permissions.py` 将 file_delete 列入高风险的 high_risk_tools，
   所有删除均需审批（approval.granted_by 非空）。
3. **仅删除常规文件**：目录一律拒绝（不提供递归删除，避免误删整棵目录树）。
本模块只做纯逻辑：参数校验、类型校验、unlink、异常映射。
"""

from __future__ import annotations

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.gatekeeper import resolve_in_workspace
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec


def delete_file(path: str) -> str:
    """删除白名单目录内的单个文件。

    Args:
        path: 目标文件路径（已由门卫校验落在 WORKSPACE_DIR 内）。

    Returns:
        删除成功后的提示，格式 ``deleted <path>``。

    Raises:
        AgentError(E_VALIDATION): path 为空 / 路径不存在 / 目标是目录（拒绝递归删除）。
        AgentError(E_TOOL): 删除失败（权限/IO）。
    """
    cid = current_correlation_id.get()
    if not path or not str(path).strip():
        raise validation_error(
            "file_delete: path 不能为空",
            source="tool.file_delete",
            correlation_id=cid,
        )
    # 相对路径按**当前工作区**解析（不是进程 cwd）。
    p = resolve_in_workspace(path)
    if not p.exists():
        raise validation_error(
            f"file_delete: 文件不存在: {path}",
            source="tool.file_delete",
            correlation_id=cid,
        )
    if p.is_dir():
        raise validation_error(
            f"file_delete: 目标是目录，拒绝删除（不支持递归删除）: {path}",
            source="tool.file_delete",
            correlation_id=cid,
        )
    try:
        p.unlink()
    except OSError as exc:
        raise tool_error(
            f"file_delete: 删除失败: {exc}",
            source="tool.file_delete",
            correlation_id=cid,
        ) from exc
    return f"deleted {path}"


spec = ToolSpec(
    name="file_delete",
    description="删除白名单目录内的单个文件（不递归、不删目录）。高风险：所有删除需审批。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要删除的文件路径（必须在白名单目录内）",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    risk_level="high",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, delete_file)


__all__ = ["delete_file", "spec"]
