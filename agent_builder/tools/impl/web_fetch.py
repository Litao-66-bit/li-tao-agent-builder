"""web_fetch：抓取页面 / 文档内容（SSRF 防护）。

安全边界：URL 安全校验由 ToolGatekeeper._check_url_safety 调用 url_guard.validate_url。
本模块只做纯逻辑：参数校验、urllib 请求、内容截断、异常映射。
"""

from __future__ import annotations

import urllib.request

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 抓取内容最大字符数：防止超大页面撑爆上下文。
MAX_OUTPUT_CHARS = 100_000
# 默认请求超时（秒），可由调用方在 args.timeout 覆盖。
DEFAULT_TIMEOUT = 20.0
# User-Agent：部分站点拒绝无 UA 的请求。
USER_AGENT = "Mozilla/5.0 (compatible; li-tao-agent-builder/1.0)"


def fetch_url(url: str, timeout: float = DEFAULT_TIMEOUT, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """抓取 URL 的文本内容。

    Args:
        url: 完整 URL（已由门卫 url_guard 校验通过）。
        timeout: 请求超时秒数。
        max_chars: 返回内容最大字符数，超过则截断。

    Returns:
        页面文本内容；超过 max_chars 时截断并附提示。

    Raises:
        AgentError(E_VALIDATION): url 为空 / timeout/max_chars 非法。
        AgentError(E_TOOL): 请求失败（网络/HTTP/解码）。
    """
    cid = current_correlation_id.get()
    if not url or not url.strip():
        raise validation_error(
            "web_fetch: url 不能为空", source="tool.web_fetch", correlation_id=cid
        )
    if timeout <= 0:
        raise validation_error(
            f"web_fetch: timeout 必须为正数: {timeout}",
            source="tool.web_fetch",
            correlation_id=cid,
        )
    if max_chars <= 0:
        raise validation_error(
            f"web_fetch: max_chars 必须为正数: {max_chars}",
            source="tool.web_fetch",
            correlation_id=cid,
        )

    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            charset = "utf-8"
            # 从 Content-Type 解析 charset，失败时回退 utf-8。
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise tool_error(
            f"web_fetch: HTTP 错误: {exc.code} {exc.reason}",
            source="tool.web_fetch",
            correlation_id=cid,
        ) from exc
    except urllib.error.URLError as exc:
        raise tool_error(
            f"web_fetch: URL 错误: {exc}",
            source="tool.web_fetch",
            correlation_id=cid,
        ) from exc
    except TimeoutError as exc:
        raise tool_error(
            f"web_fetch: 请求超时（>{timeout}s）",
            source="tool.web_fetch",
            correlation_id=cid,
        ) from exc

    try:
        text = raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError) as exc:
        raise tool_error(
            f"web_fetch: 解码失败（charset={charset}）: {exc}",
            source="tool.web_fetch",
            correlation_id=cid,
        ) from exc

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…[已截断，原文 {len(text)} 字符]"
    return text


spec = ToolSpec(
    name="web_fetch",
    description="抓取页面/文档文本内容（SSRF 防护：仅 http/https，禁私有网段）",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "完整 URL（http/https）"},
            "timeout": {
                "type": "number",
                "description": "请求超时秒数",
                "default": DEFAULT_TIMEOUT,
            },
            "max_chars": {
                "type": "integer",
                "description": "返回内容最大字符数",
                "default": MAX_OUTPUT_CHARS,
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=30.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, fetch_url)


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_OUTPUT_CHARS",
    "USER_AGENT",
    "fetch_url",
    "spec",
]
