"""FastAPI 应用工厂 —— create_app()。"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_builder.api.routes import router
from agent_builder.llm.client import LLMClient
from agent_builder.llm.config import LLMConfig

logger = logging.getLogger(__name__)


def _cors_origins() -> list[str]:
    """允许跨域的来源：读 CORS_ALLOW_ORIGINS（逗号分隔），默认本地前端开发地址。"""
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins or ["http://localhost:8080", "http://127.0.0.1:8080"]


def create_app() -> FastAPI:
    """构造 FastAPI 应用实例。"""
    app = FastAPI(
        title="Agent Builder API",
        description="Agent Builder 的 HTTP 接口层：创建任务、驱动状态机、触发分解。",
        version="0.1.0",
    )

    # CORS：默认仅允许本地前端（8080）跨域；用 CORS_ALLOW_ORIGINS 覆盖（逗号分隔）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
