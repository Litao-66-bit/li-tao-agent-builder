"""依赖注入 —— 单例 store + LLM 客户端工厂。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent_builder.api.secrets import get_api_key_store
from agent_builder.api.store import InMemoryTaskStore
from agent_builder.llm.client import LLMClient
from agent_builder.llm.config import LLMConfig

# 模块级单例。
_store: InMemoryTaskStore | None = None


def get_store() -> InMemoryTaskStore:
    """获取单例任务存储。"""
    global _store
    if _store is None:
        _store = InMemoryTaskStore()
    return _store


def reset_store() -> None:
    """重置单例（测试用）。"""
    global _store
    _store = None


def get_llm_client() -> LLMClient | None:
    """构造 LLM 客户端。

    密钥来源优先级：运行时存储（前端提交）> 环境变量 DEEPSEEK_API_KEY。
    两者皆无时返回 None（调用方降级为待确认模式）。
    """
    config = LLMConfig.from_env()
    runtime_key = get_api_key_store().get()
    if runtime_key:
        config = replace(config, api_key=runtime_key)
    client = LLMClient(config)
    return client if client.is_available else None


def build_llm_client_or_none(use_llm: bool) -> Any:
    """按 use_llm 标志构造 LLM 客户端。

    use_llm=False 或无密钥时返回 None（decomposer 回退待确认逻辑）。
    """
    if not use_llm:
        return None
    return get_llm_client()


__all__ = ["build_llm_client_or_none", "get_llm_client", "get_store", "reset_store"]
