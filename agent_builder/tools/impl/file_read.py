"""file_read：读取白名单目录内文件的文本内容。

安全边界：路径沙箱校验由 ToolGatekeeper 负责（realpath 必须在 WORKSPACE_DIR 内）。
本模块只做纯逻辑：存在性/类型校验、UTF-8 读取、超大内容截断、异常映射。
"""

from __future__ import annotations

from pathlib import Path

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 输出截断阈值：超过此大小的内容截断并标记，防止上下文/token 膨胀。
# 最终入 LLM 上下文前还会过 sanitize_tool_result 再做一次边界包裹与截断。
MAX_OUTPUT_CHARS = 200_000  # 约 50k token 量级，保守值


def read_file(path: str) -> str:
    """读取白名单目录内文件的文本内容。

    Args:
        path: 文件路径（已由门卫校验落在 WORKSPACE_DIR 内）。

    Returns:
        文件文本内容；超过 MAX_OUTPUT_CHARS 时截断并附提示。

    Raises:
        AgentError(E_VALIDATION): 路径为空 / 非文件 / 不存在。
        AgentError(E_TOOL): 读取失败（权限/IO/编码）。
    """
    cid = current_correlation_id.get()
    if not path or not str(path).strip():
        raise validation_error(
            "file_read: path 不能为空",
            source="tool.file_read",
            correlation_id=cid,
        )
    p = Path(path)
    if not p.exists():
        raise validation_error(
            f"file_read: 文件不存在: {path}",
            source="tool.file_read",
            correlation_id=cid,
        )
    if not p.is_file():
        raise validation_error(
            f"file_read: 路径不是文件: {path}",
            source="tool.file_read",
            correlation_id=cid,
        )
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise tool_error(
            f"file_read: 文件不是 UTF-8 文本（疑似二进制）: {path}",
            source="tool.file_read",
            correlation_id=cid,
        ) from exc
    except OSError as exc:
        raise tool_error(
            f"file_read: 读取失败: {exc}",
            source="tool.file_read",
            correlation_id=cid,
        ) from exc

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n…[已截断，原文 {len(text)} 字符]"
    return text


spec = ToolSpec(
    name="file_read",
    description="读取白名单目录内指定文件的文本内容（UTF-8）",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径（必须在白名单目录内）",
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

registry.register(spec, read_file)


__all__ = ["MAX_OUTPUT_CHARS", "read_file", "spec"]
