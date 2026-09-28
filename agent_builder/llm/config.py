"""LLM 配置 —— 从 .env 读取 DeepSeek API 凭据。

配置项：
- DEEPSEEK_API_KEY：API 密钥（必填，空则 is_available=False）
- DEEPSEEK_BASE_URL：API 基址（默认 https://api.deepseek.com/v1）
- DEEPSEEK_MODEL：模型名（默认 deepseek-chat）
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(slots=True)
class LLMConfig:
    """DeepSeek LLM 配置。"""

    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"

    @property
    def is_available(self) -> bool:
        """是否配置了有效密钥。"""
        return bool(self.api_key and self.api_key.strip())

    @classmethod
    def from_env(cls) -> LLMConfig:
        """从 .env + 环境变量加载配置。

        优先读 os.environ（dotenv 已加载或系统已注入），
        无则用默认值。
        """
        load_dotenv()
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        )


__all__ = ["LLMConfig"]
