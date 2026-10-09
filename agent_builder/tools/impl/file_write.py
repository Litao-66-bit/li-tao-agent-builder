"""file_write：在白名单目录内写 / 覆盖文件。

安全边界：
1. 路径沙箱校验由 ToolGatekeeper 负责（realpath 必须在 WORKSPACE_DIR 内）。
2. 所有写操作均需审批（permissions.py 将 file_write 列入 high_risk_tools）。
本模块只做纯逻辑：参数校验、父目录创建、write_text、异常映射。
"""

from __future__ import annotations

from agent_builder.contracts.errors import AgentError, tool_error, validation_error
from agent_builder.tools.gatekeeper import resolve_in_workspace
from agent_builder.tools.outline import symbol_outline
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单次写入的最大字符数：防止超大内容撑爆磁盘/上下文。
MAX_CONTENT_CHARS = 500_000

# 「占位符覆盖」护栏 —— 实测（用户任务复现 run-2 / run-3）：模型两次在**重写实现**时
# 发出 ``content="PLACEHOLDER"`` 的覆盖写，把上一轮已经写好、并且通过真实运行验证的
# 实现**整个抹掉**（随后那轮决策又解析失败，整个任务就此死掉）。
# 这是**破坏性且可确定性防住**的事故：写入之前直接拒绝，逼模型给出真内容。
# 只认「整份内容就是一句占位符」，内容里顺带出现这些词不受影响（不误伤）。
_PLACEHOLDER_TOKENS = frozenset(
    {
        "placeholder", "todo", "tbd", "tba", "xxx", "fixme", "wip", "stub",
        "same as above", "占位", "占位符", "待补充", "待填写", "同上",
    }
)
_PLACEHOLDER_MAX_CHARS = 24


def _detect_placeholder(content: str) -> str:
    """整份内容就是占位符时返回其原文（截断），否则返回空串。"""
    stripped = content.strip()
    if not stripped or len(stripped) > _PLACEHOLDER_MAX_CHARS:
        return ""
    token = stripped.strip("\"'` \t").strip(".#/*-— ").strip().rstrip(".:：。")
    if token.lower() in _PLACEHOLDER_TOKENS:
        return stripped
    # 纯省略号 / 纯点号同样没有信息量。
    if set(stripped) <= set(".…·"):
        return stripped
    return ""


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
    placeholder = _detect_placeholder(content)
    if placeholder:
        # 确定性失败（同一 inputs 重派必然再失败）→ 显式标记不可重试，并给出可执行提示。
        raise AgentError(
            "E_VALIDATION",
            f"file_write: 拒绝写入占位符内容（content={placeholder!r}）——"
            f"这会把已有文件写坏；请把**完整内容**放进 content 再写",
            source="tool.file_write",
            correlation_id=cid,
            retryable=False,
        )
    # 相对路径按**当前工作区**解析（不是进程 cwd）—— 返回值仍是调用方给的写法，摘要不变。
    p = resolve_in_workspace(path)
    if p.exists() and not overwrite:
        # 确定性失败：同一 inputs 重派必然再失败 → 显式标记不可重试，并给出可执行的修复提示。
        raise AgentError(
            "E_VALIDATION",
            f"file_write: 文件已存在且未授权覆盖: {path}"
            f"（如需覆盖请在 inputs 传 overwrite=true）",
            source="tool.file_write",
            correlation_id=cid,
            retryable=False,
        )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline=""：**不做平台换行翻译**。默认写法会把 `\n` 翻成 Windows 的 CRLF，而
        # `file_read` 读回来又是 `\n` —— 模型照"读到的文本"构造 file_edit 的 old_string 时
        # 就永远匹配不上（实测：473 行全是 CRLF 的文件上，4 次 file_edit 全部「找不到 old_string」）。
        with p.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    except OSError as exc:
        raise tool_error(
            f"file_write: 写入失败: {exc}",
            source="tool.file_write",
            correlation_id=cid,
        ) from exc
    # 把**刚写入内容的符号轮廓**一并带回：模型写完实现、下一步写测试时会按"想象"的 API 写
    # （实测：rank_papers(topic=) vs keywords、add_source vs collect_sources、survey vs
    # searcher…），自己写的测试和自己的实现自相矛盾。当场把真接口摆出来，只多几百字。
    outline = symbol_outline(content) if str(path).endswith(".py") else ""
    return f"wrote {path} ({len(content)} chars)" + outline


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
