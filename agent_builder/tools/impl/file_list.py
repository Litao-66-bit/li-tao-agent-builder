"""file_list：列出白名单目录内指定路径下的文件和子目录。

安全边界：路径沙箱校验由 ToolGatekeeper 负责（realpath 必须在 WORKSPACE_DIR 内）。
本模块只做纯逻辑：存在性/类型校验、iterdir 列举、排序、超大条目截断、异常映射。
"""

from __future__ import annotations

from pathlib import Path

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单次列出的最大条目数：超过则截断，防止上下文/token 膨胀。
MAX_ENTRIES = 500


def list_dir(path: str) -> str:
    """列出白名单目录内指定路径下的文件和子目录。

    Args:
        path: 目录路径（已由门卫校验落在 WORKSPACE_DIR 内）。

    Returns:
        每行一个条目，格式 ``[DIR]  name/`` 或 ``[FILE] name (N bytes)``。
        目录在前、文件在后，各自按名称排序。空目录返回空字符串。

    Raises:
        AgentError(E_VALIDATION): 路径为空 / 不存在 / 不是目录。
        AgentError(E_TOOL): 列目录失败（权限/IO）。
    """
    cid = current_correlation_id.get()
    if not path or not str(path).strip():
        raise validation_error(
            "file_list: path 不能为空",
            source="tool.file_list",
            correlation_id=cid,
        )
    p = Path(path)
    if not p.exists():
        raise validation_error(
            f"file_list: 路径不存在: {path}",
            source="tool.file_list",
            correlation_id=cid,
        )
    if not p.is_dir():
        raise validation_error(
            f"file_list: 路径不是目录: {path}",
            source="tool.file_list",
            correlation_id=cid,
        )
    try:
        entries = list(p.iterdir())
    except OSError as exc:
        raise tool_error(
            f"file_list: 列目录失败: {exc}",
            source="tool.file_list",
            correlation_id=cid,
        ) from exc

    # 分类 + 排序：目录在前，文件在后，各自按名称排序。
    dirs = sorted([e for e in entries if e.is_dir()], key=lambda e: e.name)
    files = sorted([e for e in entries if e.is_file()], key=lambda e: e.name)

    total = len(dirs) + len(files)
    truncated = False
    if total > MAX_ENTRIES:
        if len(dirs) >= MAX_ENTRIES:
            dirs = dirs[:MAX_ENTRIES]
            files = []
        else:
            files = files[: MAX_ENTRIES - len(dirs)]
        truncated = True

    lines: list[str] = []
    for e in dirs:
        lines.append(f"[DIR]  {e.name}/")
    for e in files:
        try:
            size = e.stat().st_size
        except OSError:
            size = -1
        lines.append(f"[FILE] {e.name} ({size} bytes)")

    if truncated:
        shown = len(dirs) + len(files)
        lines.append(f"…[已截断，共 {total} 条目，仅显示前 {shown} 条]")

    return "\n".join(lines)


spec = ToolSpec(
    name="file_list",
    description="列出白名单目录内指定路径下的文件和子目录",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要列出的目录路径（必须在白名单目录内）",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, list_dir)


__all__ = ["MAX_ENTRIES", "list_dir", "spec"]
