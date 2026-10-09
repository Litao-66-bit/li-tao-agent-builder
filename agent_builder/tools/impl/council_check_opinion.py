"""council_check_opinion：校验并规范化评审会的一条参会意见。

安全边界：纯计算，无文件/网络访问，无审批。门卫只做角色权限校验。

用途：评审会（run_council）在写入纪要前统一校验每条表态，保证结构一致、
长度可控，并剥离疑似提示词注入行（复用 tools/guard 的注入模式）。
"""

from __future__ import annotations

from typing import Any

from agent_builder.contracts.errors import AgentError, validation_error
from agent_builder.tools.guard import INJECTION_PATTERNS
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单条意见正文上限（字符）。
MAX_CONTENT_CHARS = 2000
# 证据条目上限与单条长度。
MAX_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_CHARS = 300
# 角色名上限与轮次上限。
MAX_ROLE_CHARS = 64
MAX_ROUND = 5
# 判定为「疑似注入的短行」的行长阈值（与 tools/guard 的剥离规则一致）。
INJECTION_LINE_CHARS = 120

# 合法立场。
STANCES: tuple[str, ...] = ("support", "oppose", "neutral")


def _validation(message: str) -> AgentError:
    """构造 E_VALIDATION（统一 source 与 correlation_id）。"""
    return validation_error(
        f"council_check_opinion: {message}",
        source="tool.council_check_opinion",
        correlation_id=current_correlation_id.get(),
    )


def _strip_injection(content: str) -> tuple[str, bool]:
    """剥离疑似注入指令的短行；返回 (清洗后正文, 是否剥离过)。"""
    kept: list[str] = []
    stripped = False
    for line in content.splitlines():
        low = line.strip().lower()
        if len(line.strip()) < INJECTION_LINE_CHARS and any(
            pattern in low for pattern in INJECTION_PATTERNS
        ):
            stripped = True
            continue
        kept.append(line)
    return "\n".join(kept), stripped


def check_opinion(
    role: str,
    stance: str,
    content: str,
    evidence: list[str] | None = None,
    round_index: int = 1,
) -> dict[str, Any]:
    """校验并规范化一条参会意见。

    Args:
        role: 参会角色名（如 proposer / impact_analyzer）。
        stance: 表态立场，取值 support | oppose | neutral。
        content: 表态正文。
        evidence: 可选，支撑证据列表（每条为一段文本）。
        round_index: 轮次（1 起，上限 5）。

    Returns:
        规范化后的意见字典：role / stance / content / evidence / round_index /
        content_chars / sanitized。

    Raises:
        AgentError(E_VALIDATION): 角色名为空或过长或含空白 / 立场非法 /
            正文为空或超长 / 证据条数或单条长度超限 / 轮次非法 /
            清洗后正文为空（疑似仅含注入指令）。
    """
    cleaned_role = (role or "").strip()
    if not cleaned_role:
        raise _validation("role 不能为空")
    if len(cleaned_role) > MAX_ROLE_CHARS:
        raise _validation(f"role 过长（最多 {MAX_ROLE_CHARS} 字符）")
    if any(char.isspace() for char in cleaned_role):
        raise _validation("role 不能包含空白字符")

    if stance not in STANCES:
        raise _validation(f"stance 非法（可选：{' | '.join(STANCES)}）")

    cleaned_content = (content or "").strip()
    if not cleaned_content:
        raise _validation("content 不能为空")
    if len(cleaned_content) > MAX_CONTENT_CHARS:
        raise _validation(
            f"content 过长（{len(cleaned_content)} 字符，上限 {MAX_CONTENT_CHARS}）"
        )

    if not isinstance(round_index, int) or isinstance(round_index, bool):
        raise _validation("round_index 必须是整数")
    if round_index < 1 or round_index > MAX_ROUND:
        raise _validation(f"round_index 非法（应在 1–{MAX_ROUND}）")

    items = evidence if evidence is not None else []
    if not isinstance(items, list):
        raise _validation("evidence 必须是字符串列表")
    if len(items) > MAX_EVIDENCE_ITEMS:
        raise _validation(f"evidence 条数超限（{len(items)} > {MAX_EVIDENCE_ITEMS}）")
    cleaned_evidence: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise _validation("evidence 每项都必须是字符串")
        text = item.strip()
        if not text:
            raise _validation("evidence 不允许空字符串")
        if len(text) > MAX_EVIDENCE_CHARS:
            raise _validation(f"evidence 单条过长（上限 {MAX_EVIDENCE_CHARS} 字符）")
        cleaned_evidence.append(text)

    sanitized_content, stripped = _strip_injection(cleaned_content)
    final_content = sanitized_content.strip()
    if not final_content:
        raise _validation("content 清洗后为空（疑似仅含注入指令）")

    return {
        "role": cleaned_role,
        "stance": stance,
        "content": final_content,
        "evidence": cleaned_evidence,
        "round_index": round_index,
        "content_chars": len(final_content),
        "sanitized": stripped,
    }


spec = ToolSpec(
    name="council_check_opinion",
    description="校验并规范化评审会的一条参会意见（结构 / 长度 / 注入清洗）",
    parameters={
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "参会角色名"},
            "stance": {
                "type": "string",
                "enum": list(STANCES),
                "description": "表态立场：support | oppose | neutral",
            },
            "content": {"type": "string", "description": "表态正文"},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "支撑证据列表（可选）",
            },
            "round_index": {
                "type": "integer",
                "description": f"轮次（1 起，上限 {MAX_ROUND}）",
            },
        },
        "required": ["role", "stance", "content"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=5.0,
    cost_band="low",
    allowed_roles=["conductor"],
)

registry.register(spec, check_opinion)


__all__ = [
    "INJECTION_LINE_CHARS",
    "MAX_CONTENT_CHARS",
    "MAX_EVIDENCE_CHARS",
    "MAX_EVIDENCE_ITEMS",
    "MAX_ROLE_CHARS",
    "MAX_ROUND",
    "STANCES",
    "check_opinion",
    "spec",
]
