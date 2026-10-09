"""依赖注入 —— 单例 store + LLM 客户端工厂。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent_builder.api.projects import ProjectStore
from agent_builder.api.role_briefs import brief_for
from agent_builder.api.secrets import get_api_key_store
from agent_builder.api.store import InMemoryTaskStore
from agent_builder.llm.client import LLMClient
from agent_builder.llm.config import LLMConfig

# 模块级单例。
_store: InMemoryTaskStore | None = None
_project_store: ProjectStore | None = None


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


def get_project_store() -> ProjectStore:
    """获取单例项目注册表（工作区由「当前项目」决定）。"""
    global _project_store
    if _project_store is None:
        _project_store = ProjectStore()
    return _project_store


def reset_project_store() -> None:
    """重置项目注册表单例（测试用）。"""
    global _project_store
    _project_store = None


def get_llm_client(
    *,
    model: str | None = None,
    temperature: float | None = None,
    role: str | None = None,
    usage_sink: Any = None,
    role_brief_mode: str | None = None,
) -> LLMClient | None:
    """构造 LLM 客户端。

    密钥来源优先级：运行时存储（前端提交）> 环境变量 DEEPSEEK_API_KEY。
    两者皆无时返回 None（调用方降级为待确认模式）。

    Args:
        model: 模型名覆盖（前端「模型」下拉）；None 用配置默认值。
        temperature: 采样温度覆盖（前端「推理强度」滑块）；None 用客户端默认值。
        role: 角色名；给出且角色简报已启用时按该角色注入简报（阶段 1，默认关闭）。
        usage_sink: 可选的用量采集器（如 ``UsageAccumulator``），按角色累计 token。
        role_brief_mode: 角色简报档位覆盖（``off`` / ``core`` / ``full``）；
            None 用环境变量 ``AGENT_BUILDER_ROLE_BRIEF``（默认 off）。供 A/B 对照。
    """
    config = LLMConfig.from_env()
    runtime_key = get_api_key_store().get()
    if runtime_key:
        config = replace(config, api_key=runtime_key)
    if model:
        config = replace(config, model=model)
    client = LLMClient(
        config,
        temperature=temperature,
        role_brief=brief_for(role, role_brief_mode) if role else "",
        role=role or "",
        usage_sink=usage_sink,
    )
    return client if client.is_available else None


def build_llm_client_or_none(
    use_llm: bool,
    *,
    model: str | None = None,
    temperature: float | None = None,
    role: str | None = None,
    usage_sink: Any = None,
    role_brief_mode: str | None = None,
) -> Any:
    """按 use_llm 标志构造 LLM 客户端。

    use_llm=False 或无密钥时返回 None（decomposer 回退待确认逻辑）。
    model / temperature / role / usage_sink / role_brief_mode 透传到 LLM 客户端。
    """
    if not use_llm:
        return None
    return get_llm_client(
        model=model,
        temperature=temperature,
        role=role,
        usage_sink=usage_sink,
        role_brief_mode=role_brief_mode,
    )


__all__ = ["build_llm_client_or_none", "get_llm_client", "get_store", "reset_store"]
