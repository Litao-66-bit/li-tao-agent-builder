"""web_search：网页搜索（DuckDuckGo Instant Answer API，免 key）。

安全边界：URL 安全校验由 ToolGatekeeper._check_url_safety 调用 url_guard.validate_url。
本模块只做纯逻辑：参数校验、urllib 请求、JSON 解析、结果格式化、异常映射。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# DuckDuckGo Instant Answer API（免 key）。
DDG_API_URL = "https://api.duckduckgo.com/"
# 默认返回条目数。
DEFAULT_MAX_RESULTS = 10
# 绝对上限：DuckDuckGo IA 返回的相关主题通常不多，这里设个保守上限。
ABSOLUTE_MAX = 50
# 请求超时（秒）。
DEFAULT_TIMEOUT = 20.0
USER_AGENT = "Mozilla/5.0 (compatible; li-tao-agent-builder/1.0)"


def search_web(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
    """用 DuckDuckGo Instant Answer API 做网页搜索。

    Args:
        query: 搜索关键词。
        max_results: 返回的最大条目数。

    Returns:
        每行一个结果，格式 ``[N] title — abstract (url)``。
        无结果返回空字符串。

    Raises:
        AgentError(E_VALIDATION): query 为空 / max_results 非法。
        AgentError(E_TOOL): 请求失败（网络/HTTP/JSON 解析）。
    """
    cid = current_correlation_id.get()
    if not query or not str(query).strip():
        raise validation_error(
            "web_search: query 不能为空", source="tool.web_search", correlation_id=cid
        )
    if max_results <= 0:
        raise validation_error(
            f"web_search: max_results 必须为正数: {max_results}",
            source="tool.web_search",
            correlation_id=cid,
        )
    max_results = min(max_results, ABSOLUTE_MAX)

    params = urllib.parse.urlencode({"q": query, "format": "json", "no_html": 1})
    url = f"{DDG_API_URL}?{params}"
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise tool_error(
            f"web_search: HTTP 错误: {exc.code} {exc.reason}",
            source="tool.web_search",
            correlation_id=cid,
        ) from exc
    except urllib.error.URLError as exc:
        raise tool_error(
            f"web_search: 网络错误: {exc}",
            source="tool.web_search",
            correlation_id=cid,
        ) from exc
    except TimeoutError as exc:
        raise tool_error(
            f"web_search: 请求超时（>{DEFAULT_TIMEOUT}s）",
            source="tool.web_search",
            correlation_id=cid,
        ) from exc

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise tool_error(
            f"web_search: JSON 解析失败: {exc}",
            source="tool.web_search",
            correlation_id=cid,
        ) from exc

    # DuckDuckGo IA API 返回结构：RelatedTopics 列表 + AbstractText/AbstractURL。
    results: list[str] = []
    abstract_text = data.get("AbstractText", "")
    abstract_url = data.get("AbstractURL", "")
    if abstract_text:
        heading = data.get("Heading", query)
        results.append(f"[1] {heading} — {abstract_text} ({abstract_url})")

    related = data.get("RelatedTopics", [])
    idx = len(results) + 1
    truncated = False
    for item in related:
        if len(results) >= max_results:
            truncated = True
            break
        # RelatedTopics 可能含嵌套 Topics（如类别分组），跳过无 text 的项。
        if not isinstance(item, dict):
            continue
        text = item.get("Text", "")
        first_url = item.get("FirstURL", "")
        if not text:
            # 可能是 {Topics: [...]} 的分组节点，递归取子项。
            sub_topics = item.get("Topics", [])
            for sub in sub_topics:
                if len(results) >= max_results:
                    truncated = True
                    break
                if not isinstance(sub, dict):
                    continue
                sub_text = sub.get("Text", "")
                sub_url = sub.get("FirstURL", "")
                if sub_text:
                    results.append(f"[{idx}] {sub_text} ({sub_url})")
                    idx += 1
            continue
        results.append(f"[{idx}] {text} ({first_url})")
        idx += 1

    # 循环结束后，如果 related 还有未处理的条目，也标记截断。
    if not truncated and len(related) > 0 and len(results) >= max_results:
        truncated = True

    if truncated:
        results = results[:max_results]
        results.append(f"…[已截断，仅显示前 {max_results} 条]")
    return "\n".join(results)


spec = ToolSpec(
    name="web_search",
    description="网页搜索（DuckDuckGo Instant Answer API，免 key，禁内网）",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {
                "type": "integer",
                "description": "返回的最大条目数",
                "default": DEFAULT_MAX_RESULTS,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=30.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, search_web)


__all__ = [
    "ABSOLUTE_MAX",
    "DDG_API_URL",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_TIMEOUT",
    "USER_AGENT",
    "search_web",
    "spec",
]
