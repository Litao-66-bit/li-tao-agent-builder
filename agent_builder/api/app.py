"""FastAPI 应用工厂 —— create_app()。"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from agent_builder.api.routes import router
from agent_builder.llm.client import LLMClient
from agent_builder.llm.config import LLMConfig

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """构造 FastAPI 应用实例。"""
    app = FastAPI(
        title="Agent Builder API",
        description="Agent Builder 的 HTTP 接口层：创建任务、驱动状态机、触发分解。",
        version="0.1.0",
    )
    app.include_router(router)

    # 启动时检查 LLM 是否可用（不抛错）。
    @app.on_event("startup")
    def _check_llm() -> None:
        config = LLMConfig.from_env()
        if config.is_available:
            client = LLMClient(config)
            if client.is_available:
                logger.info("LLM 客户端就绪: model=%s", config.model)
            else:
                logger.warning("LLM 客户端初始化失败，将降级为待确认模式")
        else:
            logger.info("未配置 DEEPSEEK_API_KEY，LLM 功能不可用（降级为待确认模式）")

    return app


__all__ = ["create_app"]
