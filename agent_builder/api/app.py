"""FastAPI 应用工厂 —— create_app()。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_builder.api.routes import load_api_key_on_startup, router
from agent_builder.api.security import ALLOWED_ORIGINS, CLIENT_HEADER_NAME, OriginGuardMiddleware
from agent_builder.llm.client import LLMClient
from agent_builder.llm.config import LLMConfig

logger = logging.getLogger(__name__)

# 事件循环异常处理器要忽略的**连接类**异常。
#
# 背景（实测）：Windows 上 asyncio 的 proactor 传输层在"浏览器突然断开"时会在
# **事件循环回调里**抛 ``ConnectionResetError: [WinError 10054]``；该异常逃出回调后
# **把整个 uvicorn 进程带崩** —— 用户看到的是"切个任务/刷新一下后端就没了"，
# 而且内存里的密钥与任务状态**一起丢**（然后下一次 /run 才报 E_MODEL）。
#
# 注意这里**只列连接类异常**：其余异常必须照常上报，否则会把真 bug 一起吞掉
# （那是比崩溃更糟的失效方式）。
_CONNECTION_ERROR_NAMES = frozenset(
    {"ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError"}
)
_WINDOWS_CONNECTION_RESET = 10054


def _is_connection_noise(exc: BaseException) -> bool:
    """这个异常是不是"对端断开"造成的噪声（应当忽略）？"""
    if type(exc).__name__ in _CONNECTION_ERROR_NAMES:
        return True
    # 非 10054 的等价形式：OSError(winerror=10054) / 消息里带 10054
    winerror = getattr(exc, "winerror", None)
    if winerror == _WINDOWS_CONNECTION_RESET:
        return True
    return isinstance(exc, OSError) and _WINDOWS_CONNECTION_RESET in (exc.args or ())


def _current_loop() -> asyncio.AbstractEventLoop | None:
    """当前正在跑的循环；拿不到就返回 None（**绝不抛**）。

    为什么不能直接用 ``get_event_loop()``：uvicorn 的 lifespan 跑在 ``asyncio.run()`` 里，
    那是"有运行中的循环、但没有 current event loop"的状态 —— 直接调
    ``policy.get_event_loop()`` 会抛 ``RuntimeError: There is no current event loop``，
    于是**应用启动直接失败**（实测第一版就这么把后端整挂了）。
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        return None


def _install_connection_reset_guard() -> bool:
    """给**当前事件循环**装一个异常处理器：吞掉连接类异常，其余照常上报。

    只吞"对端断开"这一类噪声，是这套机制的全部意义 ——
    它换来的效果是：**用户切任务 / 刷新 / 关页面都不会把后端带走**，
    而后端进程本身的启停仍然**由用户说了算**（不做"没人连就自动退出"：
    那样很容易变成"前端关了、后端也关不掉/起不来"，反而更难控制 —— 用户已明确否掉那个方向）。

    Returns:
        是否装上了。**拿不到循环时返回 False 而不是抛** —— 这个守卫是"防崩溃"的，
        绝不能因为它自己失败而让应用起不来（第一版就是在这个点上把后端整挂的）。
    """
    loop = _current_loop()
    if loop is None:
        logger.warning("拿不到事件循环，跳过连接异常守卫（不影响服务启动）")
        return False
    previous = loop.get_exception_handler()

    def _handler(loop_: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, BaseException) and _is_connection_noise(exc):
            # 只记 debug：这是一次正常的"客户端断开"，不是故障（记 warning 会刷屏）。
            logger.debug(
                "忽略客户端连接断开：%s（连接类噪声，不影响后端存活）", type(exc).__name__
            )
            return
        if previous is not None:
            previous(loop_, context)
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    return True


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
        # 连接类异常守卫（见 _install_connection_reset_guard 的说明）：
        # 让"客户端断开"不再把整个后端进程带走。
        _install_connection_reset_guard()
        # 卡 47：先把密钥按「环境变量 → 落盘文件 → 不加载」装回运行时存储。
        # 此前密钥只在内存 → 后端一重启就静默失效（`configured:false`，之后 /run 才报 E_MODEL）。
        source = load_api_key_on_startup()
        logger.info("密钥装载来源：%s（内容不记录）", source)
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