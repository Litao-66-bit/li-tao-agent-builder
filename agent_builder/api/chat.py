"""对话入口 —— 先聊天，再干活。

背景：发送按钮此前**无条件**创建任务并触发分解，于是「你好」也会弹出一份执行计划。
本模块用**一次 LLM 调用**做意图分流：

- ``kind=chat``：打招呼 / 闲聊 / 提问 / 让你解释 → 直接回答，不建任务、不出计划；
- ``kind=task``：明确的执行诉求（做 / 写 / 改 / 删 / 查 / 跑 / 生成 / 整理…）→ 转任务链路。

设计约束：
- 拿不准时**偏向 chat**：宁可多聊一句，也不要平白弹出一份用户没要的执行计划；
- 无可用密钥时**不降级成工作流**，而是如实告知「无法对话」——回退固定工作流会让用户
  又看到那份计划，正是本模块要消除的体验；
- 模型没给出可用结果时如实说明，不臆造回答、也不擅自转任务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KIND_CHAT = "chat"
KIND_TASK = "task"

# 上下文窗口：只带最近 N 轮，避免把整段历史塞进这次意图判定。
_MAX_HISTORY_TURNS = 8
_MAX_TURN_CHARS = 800

_SCHEMA_HINT = '{"kind": "chat|task", "reply": "你要对用户说的话", "reason": "一句话判定理由"}'

_INSTRUCTIONS = """判断用户这句话属于哪一类：

- kind="chat"：打招呼、闲聊、提问、让你解释某个概念 —— 在 reply 里**直接回答**用户；
- kind="task"：明确要你**动手做事**（做/写/改/删/查/跑/生成/整理某个具体东西）——
  reply 只写一句简短承接语（如「好的，我来帮你统计，稍等」），**不要在这里展开步骤**。

硬性规则：
1. 拿不准时选 chat —— 宁可多聊一句，也不要平白弹出一份用户没要的执行计划。
2. reply 一律用中文，自然简洁；不要复述本提示，不要输出 JSON 以外的内容。
3. kind="task" 时 reply 不得包含步骤清单或执行细节。"""


@dataclass(slots=True)
class ChatOutcome:
    """一次意图分流的结果。"""

    kind: str
    reply: str
    reason: str = ""


def missing_key_outcome() -> ChatOutcome:
    """无可用密钥：如实告知，不弹工作流。"""
    return ChatOutcome(
        kind=KIND_CHAT,
        reply="还没有配置 API 密钥，暂时无法对话。请在底部「API 密钥」里填入后重试。",
        reason="no_api_key",
    )


def unavailable_outcome() -> ChatOutcome:
    """模型没给出可用结果（网络异常 / JSON 不合法）：如实说明，不臆造、不转任务。"""
    return ChatOutcome(
        kind=KIND_CHAT,
        reply="模型这次没有正常返回内容，请稍后重试。",
        reason="llm_unavailable",
    )


def build_prompt(message: str, history: Any = ()) -> str:
    """组装意图判定提示词：最近几轮上下文 + 用户这一句。"""
    lines = [_INSTRUCTIONS, ""]
    turns = list(history or [])[-_MAX_HISTORY_TURNS:]
    if turns:
        lines.append("【最近对话】")
        for turn in turns:
            role = "用户" if _role_of(turn) == "user" else "助手"
            text = _clip(_content_of(turn))
            if text:
                lines.append(f"{role}：{text}")
        lines.append("")
    lines.append(f"【用户这一句】{_clip(message)}")
    return "\n".join(lines)


def classify_intent(client: Any, message: str, history: Any = ()) -> ChatOutcome:
    """用一次 LLM 调用判定意图，并生成可直接展示的 reply。"""
    data = client.complete_json(build_prompt(message, history), schema_hint=_SCHEMA_HINT)
    kind = str(data.get("kind") or "").strip().lower()
    reply = str(data.get("reply") or "").strip()
    if kind not in (KIND_CHAT, KIND_TASK) or not reply:
        return unavailable_outcome()
    return ChatOutcome(kind=kind, reply=reply, reason=str(data.get("reason") or "").strip())


def _role_of(turn: Any) -> str:
    if isinstance(turn, dict):
        return str(turn.get("role") or "user")
    return str(getattr(turn, "role", "user") or "user")


def _content_of(turn: Any) -> str:
    if isinstance(turn, dict):
        return str(turn.get("content") or "")
    return str(getattr(turn, "content", "") or "")


def _clip(text: str) -> str:
    """单行化 + 截断（防止把超长粘贴整个塞进提示词）。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= _MAX_TURN_CHARS else flat[: _MAX_TURN_CHARS - 1] + "…"


__all__ = [
    "KIND_CHAT",
    "KIND_TASK",
    "ChatOutcome",
    "build_prompt",
    "classify_intent",
    "missing_key_outcome",
    "unavailable_outcome",
]
