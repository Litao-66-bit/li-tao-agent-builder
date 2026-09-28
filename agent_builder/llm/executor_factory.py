"""LLM 执行器工厂 —— 把 LLMClient 包成 executor_fn(step) -> str。

角色 execute(step, executor_fn) 期望 executor_fn 签名为 Callable[[Step], Any]。
本工厂把 LLM 调用适配成此签名。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from agent_builder.contracts.schemas import Step
from agent_builder.llm.client import LLMClient


def make_llm_executor(
    llm_client: LLMClient,
    system_prompt: str = "",
) -> Callable[[Step], str]:
    """构造 LLM 执行器。

    Args:
        llm_client: LLM 客户端。
        system_prompt: 系统提示（角色身份，可选）。

    Returns:
        executor_fn(step: Step) -> str：把 step.action + step.inputs 组装成
        user message 调 LLM，返回文本。LLM 不可用时返回 "LLM_UNAVAILABLE"。
    """

    def executor_fn(step: Step) -> str:
        if not llm_client.is_available:
            return "LLM_UNAVAILABLE"
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        user_msg = f"动作: {step.action}\n输入: {json.dumps(step.inputs, ensure_ascii=False)}"
        messages.append({"role": "user", "content": user_msg})
        return llm_client.chat(messages)

    return executor_fn


__all__ = ["make_llm_executor"]
