"""file_read：读取白名单目录内文件的文本内容。

安全边界：路径沙箱校验由 ToolGatekeeper 负责（realpath 必须在 WORKSPACE_DIR 内）。
本模块只做纯逻辑：存在性/类型校验、UTF-8 读取、超大内容截断、异常映射。
"""

from __future__ import annotations

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.gatekeeper import resolve_in_workspace
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 输出截断阈值：超过此大小的内容截断并标记，防止上下文/token 膨胀。
# 最终入 LLM 上下文前还会过 sanitize_tool_result 再做一次边界包裹与截断。
MAX_OUTPUT_CHARS = 200_000  # 约 50k token 量级，保守值


def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    """读取白名单目录内文件的文本内容（可只读某段行范围）。

    Args:
        path: 文件路径（已由门卫校验落在 WORKSPACE_DIR 内）。
        start_line: 起始行号（1 起、含）。不传则从头读。
        end_line: 结束行号（1 起、含）。不传则读到尾。

    Returns:
        文件文本内容；给了行范围时先带一行 ``【path 第 X-Y 行 / 共 N 行】`` 表头；
        超过 MAX_OUTPUT_CHARS 时截断并附提示。

    Raises:
        AgentError(E_VALIDATION): 路径为空 / 非文件 / 不存在 / 行范围非法。
        AgentError(E_TOOL): 读取失败（权限/IO/编码）。
    """
    cid = current_correlation_id.get()
    if not path or not str(path).strip():
        raise validation_error(
            "file_read: path 不能为空",
            source="tool.file_read",
            correlation_id=cid,
        )
    # 相对路径按**当前工作区**解析（不是进程 cwd）：换工作区时才会读对目录。
    p = resolve_in_workspace(path)
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

    # 行范围读取：大文件的**尾部**否则永远进不了提示词（观察窗口只有 2000 字），
    # 模型只能反复重读同一个文件。实测见 api/deciders.py 的 _symbol_outline 注释。
    if start_line is not None or end_line is not None:
        lines = text.splitlines()
        total = len(lines)
        start = 1 if start_line is None else int(start_line)
        end = total if end_line is None else int(end_line)
        if start < 1:
            raise validation_error(
                f"file_read: start_line 必须 ≥ 1（收到 {start}）",
                source="tool.file_read",
                correlation_id=cid,
            )
        # 先查 start 是否越界：否则只传 start_line=99（文件 2 行）会撞上 end<start 的分支，
        # 报出「end_line(2) 不能小于 start_line(99)」——用户根本没传 end_line，这句毫无指向性。
        if start > total:
            raise validation_error(
                f"file_read: start_line({start}) 超出文件行数({total})",
                source="tool.file_read",
                correlation_id=cid,
            )
        if end < start:
            raise validation_error(
                f"file_read: end_line({end}) 不能小于 start_line({start})",
                source="tool.file_read",
                correlation_id=cid,
            )
        end = min(end, total)
        body = "\n".join(lines[start - 1 : end])
        header = f"【{path} 第 {start}-{end} 行 / 共 {total} 行】\n"
        if len(body) > MAX_OUTPUT_CHARS:
            body = body[:MAX_OUTPUT_CHARS] + f"\n…[已截断，原文 {len(body)} 字符]"
        return header + body

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
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "起始行号（1 起、含）；大文件只读某一段时用，不传则从头读",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "结束行号（1 起、含）；不传则读到尾",
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
