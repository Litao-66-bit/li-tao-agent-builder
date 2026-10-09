"""file_edit：在白名单目录内做**精确字符串替换**（局部修改，不必整份重写）。

为什么需要它（实测，用户任务 ``088f336e`` / ``0ecdbfa8``）：要改的 ``paper_agent.py`` 有
473 行 / 16.6k 字符，而当时只有 ``file_write``（只能整份重写）—— 模型面对"重写 16.6k 字符"
直接退化成发 ``content="PLACEHOLDER"``（被占位符护栏挡下），然后放弃写、回去读了十几步，
任务以 0 产物 ``stagnant`` 结束。局部替换把这类改动的输出量从 16k 降到几行。

安全边界：
1. 路径沙箱校验由 ToolGatekeeper 负责（realpath 必须在 WORKSPACE_DIR 内）。
2. 与 file_write 同档：列入 ``high_risk_tools``，所有修改均需审批。
本模块只做纯逻辑：参数校验、唯一性校验、替换、行范围与**改后符号轮廓**回报。
"""

from __future__ import annotations

from agent_builder.contracts.errors import AgentError, tool_error, validation_error
from agent_builder.tools.gatekeeper import resolve_in_workspace
from agent_builder.tools.outline import symbol_outline
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单次替换的新文本上限：防止用"局部替换"把超大内容塞进上下文。
MAX_NEW_CHARS = 200_000


def _preview(text: str, limit: int = 40) -> str:
    """把要匹配 / 替换的片段压成单行短预览（报错时给模型看得见的东西）。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _newline_variants(old_string: str) -> list[str]:
    """换行等价变体（LF ↔ CRLF）：精确匹配失败时再试它们。

    为什么必须有（实测，用户任务 ``72b28fdf``）：``file_write`` 在 Windows 上用默认 newline
    写盘会把 ``\\n`` 翻成 ``\\r\\n``，而 ``file_read`` 读回来是 ``\\n``（通用换行归一）——
    于是模型照着"读到的东西"构造的 ``old_string`` **永远**匹配不上盘上的 CRLF 原文。
    实测它 4 次 ``file_edit`` 全部以「找不到 old_string」失败，而同一次 ``code_search``
    明确指出那行确实存在、文本一模一样（473 行全是 CRLF）。
    """
    variants: list[str] = []
    crlf = old_string.replace("\r\n", "\n").replace("\n", "\r\n")
    if crlf != old_string:
        variants.append(crlf)
    lf = old_string.replace("\r\n", "\n")
    if lf != old_string:
        variants.append(lf)
    return variants


def edit_file(path: str, old_string: str, new_string: str, *, replace_all: bool = False) -> str:
    """把文件里**唯一的** ``old_string`` 替换成 ``new_string``（局部修改）。

    Args:
        path: 目标文件路径（已由门卫校验落在 WORKSPACE_DIR 内）。
        old_string: 要被替换的原文，必须与文件里的字符**完全一致**（含缩进/空白）。
        new_string: 替换成的新文本。
        replace_all: ``old_string`` 出现多次时是否全部替换（默认 False = 要求唯一）。

    Returns:
        ``edited <path> (第 X-Y 行，替换 N 处)`` + **改后全文符号轮廓**。

    Raises:
        AgentError(E_VALIDATION): path/old_string 为空、old==new、文件不存在/非文件、
            old_string 找不到、出现多次却未开 ``replace_all``。
        AgentError(E_TOOL): 读取 / 写入失败。
    """
    cid = current_correlation_id.get()
    if not path or not str(path).strip():
        raise validation_error(
            "file_edit: path 不能为空", source="tool.file_edit", correlation_id=cid
        )
    if not old_string:
        raise AgentError(
            "E_VALIDATION",
            "file_edit: old_string 不能为空 —— 局部替换必须给出要被替换的原文",
            source="tool.file_edit",
            correlation_id=cid,
            retryable=False,
        )
    if old_string == new_string:
        raise AgentError(
            "E_VALIDATION",
            "file_edit: old_string 与 new_string 相同，这次编辑不会产生任何变化",
            source="tool.file_edit",
            correlation_id=cid,
            retryable=False,
        )
    if len(new_string) > MAX_NEW_CHARS:
        raise validation_error(
            f"file_edit: new_string 超长（{len(new_string)} > {MAX_NEW_CHARS}）",
            source="tool.file_edit",
            correlation_id=cid,
        )
    p = resolve_in_workspace(path)
    if not p.exists():
        raise validation_error(
            f"file_edit: 文件不存在: {path}", source="tool.file_edit", correlation_id=cid
        )
    if not p.is_file():
        raise validation_error(
            f"file_edit: 路径不是文件: {path}", source="tool.file_edit", correlation_id=cid
        )
    try:
        # newline="" 读写：**原样**保留文件既有换行（不做任何翻译，避免整份文件的假 diff）。
        with p.open("r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError as exc:
        raise tool_error(
            f"file_edit: 文件不是 UTF-8 文本（疑似二进制）: {path}",
            source="tool.file_edit",
            correlation_id=cid,
        ) from exc
    except OSError as exc:
        raise tool_error(
            f"file_edit: 读取失败: {exc}", source="tool.file_edit", correlation_id=cid
        ) from exc

    count = text.count(old_string)
    if count == 0:
        # 精确匹配失败 → 再试换行等价变体（LF ↔ CRLF，实测背景见 _newline_variants）。
        for variant in _newline_variants(old_string):
            hits = text.count(variant)
            if hits:
                old_string = variant
                count = hits
                # 让新文本沿用文件既有换行，避免改出一份混合换行的文件。
                if "\r\n" in old_string:
                    new_string = new_string.replace("\r\n", "\n").replace("\n", "\r\n")
                break
    if count == 0:
        # 确定性失败：同一 inputs 重派必然再失败 → 不可重试，并给出可执行的修复提示。
        raise AgentError(
            "E_VALIDATION",
            f"file_edit: 在 {path} 里找不到 old_string（{_preview(old_string)!r}）——"
            f"请先用 file_read（必要时带 start_line/end_line）确认原文，"
            f"空白与缩进必须完全一致",
            source="tool.file_edit",
            correlation_id=cid,
            retryable=False,
        )
    if count > 1 and not replace_all:
        raise AgentError(
            "E_VALIDATION",
            f"file_edit: old_string 在 {path} 里出现 {count} 次，无法确定改哪一处 —— "
            f"请带上更多上下文让它唯一，或显式传 replace_all=true 全部替换",
            source="tool.file_edit",
            correlation_id=cid,
            retryable=False,
        )

    start_line = text[: text.index(old_string)].count("\n") + 1
    end_line = start_line + old_string.count("\n")
    updated = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    try:
        with p.open("w", encoding="utf-8", newline="") as fh:
            fh.write(updated)
    except OSError as exc:
        raise tool_error(
            f"file_edit: 写入失败: {exc}", source="tool.file_edit", correlation_id=cid
        ) from exc

    scope = f"第 {start_line}-{end_line} 行" if end_line > start_line else f"第 {start_line} 行"
    replaced = f"{count} 处" if replace_all else "1 处"
    # 改完立刻把**新接口**摆回给模型：改签名却忘了同步调用方（常见是它自己刚写的测试）
    # 是实测反复出现的失败模式，见 agent_builder/tools/outline.py。
    outline = symbol_outline(updated) if str(path).endswith(".py") else ""
    return f"edited {path} ({scope}，替换 {replaced})" + outline


spec = ToolSpec(
    name="file_edit",
    description="局部修改文件：把唯一的 old_string 精确替换成 new_string（无需整份重写）",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径（必须在白名单目录内）"},
            "old_string": {
                "type": "string",
                "description": "要被替换的原文，必须与文件内容完全一致（含缩进/空白）",
            },
            "new_string": {"type": "string", "description": "替换成的新文本"},
            "replace_all": {
                "type": "boolean",
                "description": "old_string 出现多次时是否全部替换，默认 false（要求唯一）",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
        "additionalProperties": False,
    },
    risk_level="medium",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, edit_file)


__all__ = ["MAX_NEW_CHARS", "edit_file", "spec"]
