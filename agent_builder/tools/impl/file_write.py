"""file_write：在白名单目录内写 / 覆盖文件。

安全边界：
1. 路径沙箱校验由 ToolGatekeeper 负责（realpath 必须在 WORKSPACE_DIR 内）。
2. 所有写操作均需审批（permissions.py 将 file_write 列入 high_risk_tools）。
本模块只做纯逻辑：参数校验、父目录创建、write_text、异常映射。
"""

from __future__ import annotations

from pathlib import Path

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单次写入的最大字符数：防止超大内容撑爆磁盘/上下文。
MAX_CONTENT_CHARS = 500_000


def write_file(path: str, content: str, *, overwrite: bool = False) -> str:
    """在白名单目录内写 / 覆盖文件。

    Args:
        path: 目标文件路径（已由门卫校验落在 WORKSPACE_DIR 内）。
        content: 要写入的文本内容（UTF-8）。
        overwrite: 文件已存在时是否覆盖。False 时已存在则报错。

    Returns:
        写入成功后的提示，格式 ``wrote <path> (<N> chars)``。

    Raises:
        AgentError(E_VALIDATION): path/content 为空 / 内容超长 / 文件已存在且未授权覆盖。
        AgentError(E_TOOL): 写入失败（权限/IO/编码）。
    """
    cid = current_correlation_id.get()
    if not path or not str(path).strip():
        raise validation_error(
            "file_write: path 不能为空", source="tool.file_write", correlation_id=cid
        )
    if content is None:
        raise validation_error(
            "file_write: content 不能为 None", source="tool.file_write", correlation_id=cid
        )
    if len(content) > MAX_CONTENT_CHARS:
        raise validation_error(
            f"file_write: 内容超长（{len(content)} > {MAX_CONTENT_CHARS}）",
            source="tool.file_write",
            correlation_id=cid,
        )
    p = Path(path)
    if p.exists() and not overwrite:
        raise validation_error(
            f"file_write: 文件已存在且未授权覆盖: {path}",
            source="tool.file_write",
            correlation_id=cid,
        )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise tool_error(
            f"file_write: 写入失败: {exc}",
            source="tool.file_write",
            correlation_id=cid,
        ) from exc
    return f"wrote {path} ({len(content)} chars)"


spec = ToolSpec(
    name="file_write",
    description="在白名单目录内写 / 覆盖文件（UTF-8）。高风险：所有写操作需审批。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径（必须在白名单目录内）"},
            "content": {"type": "string", "description": "要写入的文本内容"},
            "overwrite": {
                "type": "boolean",
                "description": "文件已存在时是否覆盖，默认 false",
                "default": False,
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    risk_level="medium",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, write_file)


__all__ = ["MAX_CONTENT_CHARS", "spec", "write_file"]
