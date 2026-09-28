"""LLM 客户端 —— 封装 langchain_openai.ChatOpenAI 指向 DeepSeek。

提供：
- chat(messages) -> str：纯文本对话。
- complete_json(prompt, schema_hint) -> dict：要求 LLM 返回 JSON 并解析。

失败降级：无密钥或调用异常时返回空值 / 空字典，不抛错。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent_builder.llm.config import LLMConfig

logger = logging.getLogger(__name__)


class LLMClient:
    """DeepSeek LLM 客户端（OpenAI 兼容格式）。

    无密钥时 _client=None，所有方法返回空值（降级）。
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: Any = None
        if config.is_available:
            try:
                from langchain_openai import ChatOpenAI

                self._client = ChatOpenAI(
                    model=config.model,
                    base_url=config.base_url,
                    api_key=config.api_key,
                    temperature=0.3,
                )
            except Exception as exc:  # noqa: BLE001  降级：实例化失败不阻断
                logger.warning("LLM 客户端初始化失败，降级为不可用: %s", exc)
                self._client = None

    @property
    def is_available(self) -> bool:
        """客户端是否就绪。"""
        return self._client is not None

    def chat(self, messages: list[dict[str, str]]) -> str:
        """纯文本对话。

        Args:
            messages: OpenAI 消息格式 [{"role": "user", "content": "..."}]。

        Returns:
            LLM 回复文本；不可用或异常时返回空串。
        """
        if not self.is_available:
            return ""
        try:
            resp = self._client.invoke(messages)
            content = getattr(resp, "content", "")
            return str(content) if content else ""
        except Exception as exc:  # noqa: BLE001  降级：调用失败返回空串
            logger.warning("LLM chat 调用失败: %s", exc)
            return ""

    def complete_json(self, prompt: str, schema_hint: str = "") -> dict[str, Any]:
        """要求 LLM 返回 JSON 并解析。

        Args:
            prompt: 用户提示（描述要 LLM 做什么）。
            schema_hint: 期望的 JSON 结构提示（可选）。

        Returns:
            解析后的字典；不可用或解析失败返回 {}。
        """
        if not self.is_available:
            return {}
        system = "你是一个严格的结构化输出器。只返回纯 JSON，不要任何额外文字或解释。"
        if schema_hint:
            system += f"\n期望结构：{schema_hint}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        raw = self.chat(messages)
        if not raw:
            return {}
        return self._extract_json(raw)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """从可能含 markdown 代码块的文本中提取 JSON。"""
        # 尝试直接解析。
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 代码块。
        match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试提取首个 {...} 块。
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {}


__all__ = ["LLMClient"]
