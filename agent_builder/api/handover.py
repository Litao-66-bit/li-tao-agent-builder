"""交接提示（handover hint）—— 规则式判定，不调 LLM。

会话（对话轮次 / token 量 / 字符量）超过阈值时，检测是否需要建立交接提示以开启下一轮新对话。

归属层：编排层 ``api/``（不碰 ``roles/``，角色评分零影响）。
产物：后端结构化交接对象 + 前端卡片（``generated_by='rule'``）。
行为：仅提示；用户手动新开对话（不自动清空、不自动建 task）。

门控（任一命中即进入判定）：
- 对话轮次（用户消息数）> ``TURNS``
- 累计 token ≥ ``TOKENS``
- 累计字符 ≥ ``CHARS``（无 token 计量时的廉价兜底，第三条件）

判定（门控命中 + 未决项 + 任务状态综合）：
- ``delivered`` → ``none``（已闭环不必交接）
- 提示次数达上限 → ``none``（限次）
- 上次被忽略 → ``none``（去重）
- 门控命中 且 有未决项 → ``strong``
- 门控命中 且 无未决项 → ``suggest``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from agent_builder.narrate import action_title, summarize_result

# ── 阈值（环境变量可调，非法值回落默认） ──────────────────────────────────

TURNS_ENV = "AGENT_BUILDER_HANDOVER_TURNS"
TOKENS_ENV = "AGENT_BUILDER_HANDOVER_TOKENS"
CHARS_ENV = "AGENT_BUILDER_HANDOVER_CHARS"
MAX_PROMPTS_ENV = "AGENT_BUILDER_HANDOVER_MAX_PROMPTS"

DEFAULT_TURNS = 5
DEFAULT_TOKENS = 12000
DEFAULT_CHARS = 24000
DEFAULT_MAX_PROMPTS = 3

# 机器未决的「非完成」状态集合（execution_results.status）。
MACHINE_OPEN_STATUSES = frozenset({"failed", "pending_approval", "pending", "skipped"})

# ack 状态。
ACK_NONE = "none"
ACK_SEEN = "seen"
ACK_IGNORED = "ignored"

# 强度。
STRENGTH_STRONG = "strong"
STRENGTH_SUGGEST = "suggest"
STRENGTH_NONE = "none"


def _int_env(name: str, default: int) -> int:
    """读整型环境变量；缺失/非法回落默认值。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, TypeError):
        return default
    return value if value >= 0 else default


def thresholds() -> dict[str, int]:
    """当前阈值（每次调用实时读环境变量，便于测试与运行时调整）。"""
    return {
        "turns": _int_env(TURNS_ENV, DEFAULT_TURNS),
        "tokens": _int_env(TOKENS_ENV, DEFAULT_TOKENS),
        "chars": _int_env(CHARS_ENV, DEFAULT_CHARS),
        "max_prompts": _int_env(MAX_PROMPTS_ENV, DEFAULT_MAX_PROMPTS),
    }


@dataclass(slots=True)
class HandoverDecision:
    """交接判定结果。"""

    should_suggest: bool
    strength: str  # strong | suggest | none
    reasons: list[str] = field(default_factory=list)
    turns_hit: bool = False
    tokens_hit: bool = False
    chars_hit: bool = False
    exhausted: bool = False


def should_suggest_handover(
    *,
    turns: int,
    context_tokens: int,
    context_chars: int,
    task_status: str,
    prompts_shown: int,
    ack_status: str,
    open_items: int,
) -> HandoverDecision:
    """规则式判定是否需要交接提示。

    Args:
        turns: 对话轮次（用户消息数）。
        context_tokens: 累计 token 量。
        context_chars: 累计字符量。
        task_status: 任务状态字符串（如 ``executing`` / ``delivered``）。
        prompts_shown: 本会话已提示次数。
        ack_status: 上次 ack 状态（``none`` / ``seen`` / ``ignored``）。
        open_items: 未决项总数（人的未决 + 机器未决）。

    Returns:
        ``HandoverDecision``：是否提示 + 强度 + 原因。
    """
    t = thresholds()
    turns_hit = turns > t["turns"]
    tokens_hit = context_tokens >= t["tokens"]
    chars_hit = context_chars >= t["chars"]
    gate = turns_hit or tokens_hit or chars_hit
    exhausted = prompts_shown >= t["max_prompts"]

    reasons: list[str] = []
    if task_status == "delivered":
        reasons.append("任务已交付（delivered），无需交接")
        return HandoverDecision(False, STRENGTH_NONE, reasons, exhausted=exhausted)
    if exhausted:
        reasons.append(f"本会话已提示 {prompts_shown} 次，达上限（{t['max_prompts']}）")
        return HandoverDecision(
            False, STRENGTH_NONE, reasons, turns_hit, tokens_hit, chars_hit, exhausted=True
        )
    if ack_status == ACK_IGNORED:
        reasons.append("上一次交接提示已被忽略（不重复打扰）")
        return HandoverDecision(
            False, STRENGTH_NONE, reasons, turns_hit, tokens_hit, chars_hit, exhausted
        )

    hits = []
    if turns_hit:
        hits.append(f"对话轮次 {turns} > {t['turns']}")
    if tokens_hit:
        hits.append(f"累计 token {context_tokens} ≥ {t['tokens']}")
    if chars_hit:
        hits.append(f"累计字符 {context_chars} ≥ {t['chars']}")
    reasons.append(hits and f"门控命中：{' 或 '.join(hits)}" or "门控未命中（轮次 / token / 字符均未达阈值）")

    if gate and open_items > 0:
        reasons.append(f"存在 {open_items} 项未决，建议交接以便在新对话中接续处理")
        return HandoverDecision(
            True, STRENGTH_STRONG, reasons, turns_hit, tokens_hit, chars_hit, exhausted
        )
    if gate:
        reasons.append("无未决项，可安全开新对话")
        return HandoverDecision(
            True, STRENGTH_SUGGEST, reasons, turns_hit, tokens_hit, chars_hit, exhausted
        )
    return HandoverDecision(
        False, STRENGTH_NONE, reasons, turns_hit, tokens_hit, chars_hit, exhausted
    )


def collect_open_items(entry: Any) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """从任务条目收集未决项：人的未决（优先）+ 机器未决。

    - 人的未决：``pending_questions``（来自规划）+ ``self_check.issues``（副结构自检），
      每项标 ``source``。
    - 机器未决：``execution_results`` 中状态非 ``done`` 的步骤。
      ``action`` 原样保留（审计用），另给 ``title``（中文动作名）与
      人话 ``detail``，避免卡片直接展示英文 action 与异常原文。
    """
    human: list[dict[str, str]] = []
    for q in getattr(entry, "pending_questions", None) or []:
        if q:
            human.append({"text": str(q), "source": "pending_questions"})
    self_check = getattr(entry, "self_check", None)
    if isinstance(self_check, dict):
        for issue in self_check.get("issues") or []:
            if issue:
                human.append({"text": str(issue), "source": "self_check"})

    machine: list[dict[str, str]] = []
    for r in getattr(entry, "execution_results", None) or []:
        status = str(r.get("status") or "")
        if status and status != "done":
            action = str(r.get("action") or "")
            detail = str(r.get("summary") or "").strip() or summarize_result(
                action=action,
                status=status,
                result=r.get("result"),
                error=r.get("error"),
            )
            machine.append(
                {
                    "step_id": str(r.get("step_id") or ""),
                    "action": action,
                    "title": action_title(action),
                    "status": status,
                    "detail": detail,
                }
            )
    return human, machine


def build_handover_brief(
    entry: Any,
    decision: HandoverDecision,
    *,
    turns: int,
    context_tokens: int,
    context_chars: int,
) -> dict[str, Any]:
    """组装交接响应对象（结构化，前端直接渲染）。

    ``generated_by='rule'``：本版不调 LLM。
    """
    t = thresholds()
    human, machine = collect_open_items(entry)
    open_items = len(human) + len(machine)
    ack = getattr(entry, "handover_ack", None) or {}
    ack_status = ack.get("status") or ACK_NONE

    # next_actions：规则式建议下一步（不调 LLM）。
    next_actions: list[str] = []
    if human:
        next_actions.append("先答复「人的未决」，再继续规划或执行")
    if machine:
        next_actions.append("处理机器未决步骤（重试失败项 / 放行待审批项）")
    if not human and not machine and decision.should_suggest:
        next_actions.append("当前无未决项，可安全开新对话接续")
    next_actions.append("把本卡片内容粘贴到新对话首条消息，作为接续上下文")

    return {
        "task_id": getattr(entry.conductor, "task_state", None).task_id if entry.conductor else "",
        "task_status": (
            getattr(getattr(entry.conductor, "task_state", None), "status", None).value
            if getattr(getattr(entry.conductor, "task_state", None), "status", None)
            else "unknown"
        ),
        "should_suggest": decision.should_suggest,
        "strength": decision.strength,
        "reasons": decision.reasons,
        "generated_by": "rule",
        "metric": {
            "turns": turns,
            "context_tokens": context_tokens,
            "context_chars": context_chars,
        },
        "gate": {
            "turns": turns,
            "turns_threshold": t["turns"],
            "turns_hit": decision.turns_hit,
            "tokens": context_tokens,
            "tokens_threshold": t["tokens"],
            "tokens_hit": decision.tokens_hit,
            "chars": context_chars,
            "chars_threshold": t["chars"],
            "chars_hit": decision.chars_hit,
            "prompts_shown": getattr(entry, "handover_prompts_shown", 0) or 0,
            "max_prompts": t["max_prompts"],
            "exhausted": decision.exhausted,
        },
        "ack": {
            "status": ack_status,
            "at": ack.get("at"),
        },
        "human_open_questions": human,
        "machine_open_items": machine,
        "open_items_count": open_items,
        "next_actions": next_actions,
    }


__all__ = [
    "ACK_IGNORED",
    "ACK_NONE",
    "ACK_SEEN",
    "CHARS_ENV",
    "DEFAULT_CHARS",
    "DEFAULT_MAX_PROMPTS",
    "DEFAULT_TOKENS",
    "DEFAULT_TURNS",
    "MACHINE_OPEN_STATUSES",
    "MAX_PROMPTS_ENV",
    "STRENGTH_NONE",
    "STRENGTH_STRONG",
    "STRENGTH_SUGGEST",
    "TOKENS_ENV",
    "TURNS_ENV",
    "HandoverDecision",
    "build_handover_brief",
    "collect_open_items",
    "should_suggest_handover",
    "thresholds",
]
