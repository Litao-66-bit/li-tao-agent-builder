"""FastAPI 应用工厂 —— create_app()。"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_builder.api.routes import router
from agent_builder.api.security import ALLOWED_ORIGINS, CLIENT_HEADER_NAME, OriginGuardMiddleware
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

    # CORS：只允许本机前端（8080）跨域调用后端（8000）。
    # 安全约束：来源白名单 + 不共享凭据（不用 Cookie 鉴权，故 allow_credentials=False）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(ALLOWED_ORIGINS),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", CLIENT_HEADER_NAME],
    )
    # 来源守卫：带 Origin 头且不在白名单的请求直接 403（拦截浏览器跨站与 DNS rebinding）。
    # 注意注册顺序：后注册的中间件在最外层，故守卫先于 CORS 执行。
    app.add_middleware(OriginGuardMiddleware)

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
