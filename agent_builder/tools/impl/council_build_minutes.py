"""council_build_minutes：把评审会各方意见收敛为结构化纪要（规则式，不调 LLM）。

安全边界：纯计算，无文件/网络访问，无审批。门卫只做角色权限校验。

职责：把「多角色表态」按立场分组，输出一致点 / 分歧点 / 未决项 / 缺席者，
并给出规则式的建议决议骨架；不引入 LLM 判断（需要润色时由上层用注入的
LLM 客户端处理，避免工具层反向依赖配置层）。
"""

from __future__ import annotations

from typing import Any

from agent_builder.contracts.errors import AgentError, validation_error
from agent_builder.tools.impl.council_check_opinion import check_opinion
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单场会议的议题长度上限、意见条数上限、缺席名单上限。
MAX_TOPIC_CHARS = 300
MAX_OPINIONS = 50
MAX_ABSENT = 20

# 建议决议的三种结论（规则式，非 LLM 判断）。
DECISION_PROCEED = "可推进（无反对意见）"
DECISION_REVIEW = "建议推进前先裁决反对意见"
DECISION_HOLD = "建议暂缓：反对意见多于支持意见"


def _validation(message: str) -> AgentError:
    """构造 E_VALIDATION（统一 source 与 correlation_id）。"""
    return validation_error(
        f"council_build_minutes: {message}",
        source="tool.council_build_minutes",
        correlation_id=current_correlation_id.get(),
    )


def _brief(opinion: dict[str, Any]) -> dict[str, str]:
    """把规范化意见压成纪要里的短条目（角色 + 正文）。"""
    return {"role": str(opinion["role"]), "content": str(opinion["content"])}


def _normalize_opinions(raw_opinions: list[Any]) -> list[dict[str, Any]]:
    """逐条校验并规范化参会意见（复用 council_check_opinion 的规则）。"""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_opinions):
        if not isinstance(item, dict):
            raise _validation(f"第 {index + 1} 条意见必须是对象")
        try:
            normalized.append(
                check_opinion(
                    role=item.get("role", ""),
                    stance=item.get("stance", ""),
                    content=item.get("content", ""),
                    evidence=item.get("evidence"),
                    round_index=item.get("round_index", 1),
                )
            )
        except AgentError as exc:
            raise _validation(f"第 {index + 1} 条意见非法：{exc.info.message}") from exc
    return normalized


def build_minutes(
    topic: str,
    opinions: list[dict[str, Any]],
    absent: list[str] | None = None,
) -> dict[str, Any]:
    """把参会意见收敛为结构化纪要。

    Args:
        topic: 会议议题。
        opinions: 参会意见列表，每项含 role / stance / content（可带 evidence / round_index）。
        absent: 可选，缺席角色名单（失败或未表态的角色）。

    Returns:
        纪要字典：topic / participants / absent / support / oppose / neutral /
        agreements / disagreements / unresolved / suggested_decision / opinion_count。

    Raises:
        AgentError(E_VALIDATION): 议题为空或超长 / 无任何意见 / 意见条数超限 /
            单条意见非法 / 缺席名单非法或超限。
    """
    cleaned_topic = (topic or "").strip()
    if not cleaned_topic:
        raise _validation("topic 不能为空")
    if len(cleaned_topic) > MAX_TOPIC_CHARS:
        raise _validation(f"topic 过长（最多 {MAX_TOPIC_CHARS} 字符）")
    if not isinstance(opinions, list) or not opinions:
        raise _validation("opinions 不能为空")
    if len(opinions) > MAX_OPINIONS:
        raise _validation(f"opinions 条数超限（{len(opinions)} > {MAX_OPINIONS}）")

    normalized = _normalize_opinions(opinions)

    absent_list: list[str] = []
    if absent is not None:
        if not isinstance(absent, list):
            raise _validation("absent 必须是字符串列表")
        if len(absent) > MAX_ABSENT:
            raise _validation(f"absent 条数超限（{len(absent)} > {MAX_ABSENT}）")
        for item in absent:
            if not isinstance(item, str) or not item.strip():
                raise _validation("absent 每项都必须是非空字符串")
            absent_list.append(item.strip())

    support = [item for item in normalized if item["stance"] == "support"]
    oppose = [item for item in normalized if item["stance"] == "oppose"]
    neutral = [item for item in normalized if item["stance"] == "neutral"]

    # 一致点：只有支持、没有反对时才成立；出现反对即转为分歧。
    agreements = [_brief(item) for item in support] if not oppose else []
    # 分歧点：支持与反对并存时，把双方意见并列出来。
    disagreements = (
        [_brief(item) for item in support] + [_brief(item) for item in oppose]
        if support and oppose
        else []
    )
    # 未决项：反对意见（待裁决）+ 中立意见（信息不足）。
    unresolved = [_brief(item) for item in oppose] + [_brief(item) for item in neutral]

    if not oppose:
        decision = DECISION_PROCEED
    elif len(oppose) > len(support):
        decision = DECISION_HOLD
    else:
        decision = DECISION_REVIEW

    return {
        "topic": cleaned_topic,
        "participants": sorted({str(item["role"]) for item in normalized}),
        "absent": absent_list,
        "support": [_brief(item) for item in support],
        "oppose": [_brief(item) for item in oppose],
        "neutral": [_brief(item) for item in neutral],
        "agreements": agreements,
        "disagreements": disagreements,
        "unresolved": unresolved,
        "suggested_decision": decision,
        "opinion_count": len(normalized),
    }


spec = ToolSpec(
    name="council_build_minutes",
    description="把评审会各方意见收敛为结构化纪要（一致 / 分歧 / 未决 / 建议决议）",
    parameters={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "会议议题"},
            "opinions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "stance": {"type": "string"},
                        "content": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "round_index": {"type": "integer"},
                    },
                    "required": ["role", "stance", "content"],
                },
                "description": "参会意见列表",
            },
            "absent": {
                "type": "array",
                "items": {"type": "string"},
                "description": "缺席角色名单（可选）",
            },
        },
        "required": ["topic", "opinions"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["conductor"],
)

registry.register(spec, build_minutes)


__all__ = [
    "DECISION_HOLD",
    "DECISION_PROCEED",
    "DECISION_REVIEW",
    "MAX_ABSENT",
    "MAX_OPINIONS",
    "MAX_TOPIC_CHARS",
    "build_minutes",
    "spec",
]
