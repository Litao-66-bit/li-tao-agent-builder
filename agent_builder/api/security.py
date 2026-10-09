"""本机 API 访问边界控制 —— 密钥安全 P0。

威胁：8000 端口原先对「任意来源」开放。
1. 任意网页可借浏览器跨域调用（原 CORS ``allow_origins=["*"]`` +
   ``allow_credentials=True`` 组合本身也不合规）；
2. 本机其它进程可直接访问（HTTP 层无法与同权限级本地进程隔离）。

本模块提供两层控制：
1. ``OriginGuardMiddleware``：凡是带 ``Origin`` 头的请求，来源必须在
   前端白名单内 —— 拦截浏览器跨站调用与 DNS rebinding；
2. ``require_local_client``：敏感端点（密钥、工作区文件）必须携带自定义头
   ``X-Agent-Builder-Client``。浏览器跨站请求需预检才能携带自定义头，
   表单型跨站提交则完全无法设置头。

残余风险（明示，不在 HTTP 层解决）：与本机后端同 OS 用户/同权限级的
本地进程仍可伪造上述请求头；真正的安全边界是操作系统用户账号，
正式部署必须叠加 HTTPS 与真实鉴权。
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

# 前端来源白名单：仅本机 8080 静态页（127.0.0.1 与 localhost 两种写法）。
ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:8080",
    "http://localhost:8080",
)

# 敏感端点的本机客户端标识头（前端 api.js 统一携带）。
CLIENT_HEADER_NAME = "X-Agent-Builder-Client"
CLIENT_HEADER_VALUE = "web"


def is_allowed_origin(origin: str | None) -> bool:
    """判断请求来源是否在白名单内。

    无 ``Origin`` 头（curl / 同源导航 / 测试客户端）视为放行，
    由 ``require_local_client`` 继续把关敏感端点。
    """
    if not origin:
        return True
    return origin.rstrip("/") in ALLOWED_ORIGINS


class OriginGuardMiddleware(BaseHTTPMiddleware):
    """拦截来源不在白名单的跨站请求（带 Origin 头即校验）。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        origin = request.headers.get("origin")
        if not is_allowed_origin(origin):
            # 不返回任何 CORS 头：浏览器侧同样读不到响应内容。
            return JSONResponse(status_code=403, content={"detail": f"来源未授权：{origin}"})
        return await call_next(request)


def require_local_client(request: Request) -> None:
    """敏感端点依赖项：必须携带本机客户端标识头。

    应用于 ``/settings/api-key*`` 与 ``/workspace/file*``；
    缺头一律 403，错误信息不含任何请求内容。
    """
    if request.headers.get(CLIENT_HEADER_NAME) != CLIENT_HEADER_VALUE:
        raise HTTPException(status_code=403, detail=f"缺少本机客户端标识头 {CLIENT_HEADER_NAME}")


__all__ = [
    "ALLOWED_ORIGINS",
    "CLIENT_HEADER_NAME",
    "CLIENT_HEADER_VALUE",
    "OriginGuardMiddleware",
    "is_allowed_origin",
    "require_local_client",
]
