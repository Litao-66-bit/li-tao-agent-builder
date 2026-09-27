"""citation_check：校验引用来源存在性（URL + DOI）。

安全边界：URL 安全校验在实现内部调 url_guard.validate_url（双层防护）。
本模块只做纯逻辑：参数校验、URL 请求、DOI 查询、结果汇总。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec
from agent_builder.tools.url_guard import validate_url

# 单次最多校验的来源数量。
MAX_SOURCES = 50
# 单个来源校验超时（秒）。
DEFAULT_TIMEOUT = 15.0
# User-Agent：部分站点拒绝无 UA 的请求。
USER_AGENT = "Mozilla/5.0 (compatible; li-tao-agent-builder/1.0)"

# DOI 模式：10.xxxx/yyyy（前缀 10. + 4 位以上数字 + / + 非空内容）。
_DOI_PATTERN = re.compile(r"^10\.\d{4,}/\S+$")


def check_citations(sources: list[str], timeout: float = DEFAULT_TIMEOUT) -> str:
    """校验引用来源存在性。

    Args:
        sources: 引用来源列表（URL 或 DOI 字符串）。
        timeout: 单个来源校验超时秒数。

    Returns:
        每行一个结果：``[OK/FAIL/SKIP] source — 详情``。
        无来源时返回空字符串。

    Raises:
        AgentError(E_VALIDATION): sources 为空 / 超过 MAX_SOURCES / timeout 非法。
        AgentError(E_TOOL): 校验过程出错。
    """
    cid = current_correlation_id.get()
    if not sources:
        raise validation_error(
            "citation_check: sources 不能为空", source="tool.citation_check", correlation_id=cid
        )
    if len(sources) > MAX_SOURCES:
        raise validation_error(
            f"citation_check: sources 数量 {len(sources)} 超过上限 {MAX_SOURCES}",
            source="tool.citation_check",
            correlation_id=cid,
        )
    if timeout <= 0:
        raise validation_error(
            f"citation_check: timeout 必须为正数: {timeout}",
            source="tool.citation_check",
            correlation_id=cid,
        )

    results: list[str] = []
    for source in sources:
        source = source.strip()
        if not source:
            results.append("[SKIP] (empty source)")
            continue
        if source.startswith(("http://", "https://")):
            results.append(_check_url(source, timeout))
        elif _DOI_PATTERN.match(source):
            results.append(_check_doi(source, timeout))
        else:
            results.append(f"[SKIP] {source} — 无法识别格式（非 URL/DOI）")

    return "\n".join(results) if results else ""


def _check_url(url: str, timeout: float) -> str:
    """校验 URL 是否可访问（GET 请求，检查状态码）。"""
    try:
        validate_url(url)
    except ValueError as exc:
        return f"[FAIL] {url} — URL 安全校验失败: {exc}"

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return f"[OK] {url} — HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return f"[FAIL] {url} — HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"[FAIL] {url} — {exc}"


def _check_doi(doi: str, timeout: float) -> str:
    """校验 DOI 是否有效（查询 doi.org 解析 API）。"""
    doi_url = f"https://doi.org/{doi}"
    req = urllib.request.Request(doi_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return f"[OK] {doi} — DOI resolved (HTTP {resp.status})"
    except urllib.error.HTTPError as exc:
        return f"[FAIL] {doi} — DOI not found (HTTP {exc.code})"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"[FAIL] {doi} — {exc}"


spec = ToolSpec(
    name="citation_check",
    description="校验引用来源存在性（URL + DOI，自动识别格式）",
    parameters={
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "引用来源列表（URL 或 DOI 字符串）",
            },
            "timeout": {
                "type": "number",
                "description": "单个来源校验超时秒数",
                "default": DEFAULT_TIMEOUT,
            },
        },
        "required": ["sources"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=120.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, check_citations)


__all__ = ["DEFAULT_TIMEOUT", "MAX_SOURCES", "USER_AGENT", "check_citations", "spec"]
