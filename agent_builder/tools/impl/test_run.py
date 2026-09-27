"""test_run：跑 pytest 测试或抓取文档（自动识别 target）。

安全边界：
1. URL target 由门卫 _check_target_safety 校验（调 url_guard.validate_url）。
2. 路径 target 由门卫 _check_target_safety 校验（沙箱内）。
本模块只做纯逻辑：自动识别 target 类型、执行 pytest 或抓取文档、截断、异常映射。
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 默认执行超时（秒）—— pytest 可能较慢。
DEFAULT_TIMEOUT = 60.0
# 输出最大字符数。
MAX_OUTPUT_CHARS = 50_000
# User-Agent：部分站点拒绝无 UA 的请求。
USER_AGENT = "Mozilla/5.0 (compatible; li-tao-agent-builder/1.0)"


def run_test(target: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """跑 pytest 测试或抓取文档（自动识别 target）。

    Args:
        target: 测试路径（文件/目录）或文档 URL。
        timeout: 执行超时秒数。

    Returns:
        pytest 输出或页面文本；超过 MAX_OUTPUT_CHARS 截断。

    Raises:
        AgentError(E_VALIDATION): target 为空 / timeout 非法。
        AgentError(E_TOOL): pytest 不可用 / 执行失败 / 抓取失败。
    """
    cid = current_correlation_id.get()
    if not target or not target.strip():
        raise validation_error(
            "test_run: target 不能为空", source="tool.test_run", correlation_id=cid
        )
    if timeout <= 0:
        raise validation_error(
            f"test_run: timeout 必须为正数: {timeout}",
            source="tool.test_run",
            correlation_id=cid,
        )

    target = target.strip()
    if target.startswith(("http://", "https://")):
        return _fetch_doc(target, timeout, cid)
    return _run_pytest(target, timeout, cid)


def _fetch_doc(url: str, timeout: float, cid: str) -> str:
    """抓取文档内容。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].split(";")[0].strip()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise tool_error(
            f"test_run: HTTP 错误: {exc.code} {exc.reason}",
            source="tool.test_run",
            correlation_id=cid,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise tool_error(
            f"test_run: 抓取失败: {exc}",
            source="tool.test_run",
            correlation_id=cid,
        ) from exc

    text = raw.decode(charset or "utf-8", errors="replace")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n…[已截断，原文 {len(text)} 字符]"
    return text


def _run_pytest(path: str, timeout: float, cid: str) -> str:
    """运行 pytest 测试。"""
    try:
        proc = subprocess.run(
            ["pytest", path, "-v", "--tb=short"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise tool_error(
            f"test_run: pytest 未安装或不在 PATH: {exc}",
            source="tool.test_run",
            correlation_id=cid,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise tool_error(
            f"test_run: 测试超时（>{timeout}s）",
            source="tool.test_run",
            correlation_id=cid,
        ) from exc

    output = proc.stdout
    if proc.stderr:
        output += f"\n[stderr]\n{proc.stderr}"
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + f"\n…[已截断，原文 {len(output)} 字符]"
    return output


spec = ToolSpec(
    name="test_run",
    description="跑 pytest 测试或抓取文档（自动识别 target：URL→文档，路径→pytest）",
    parameters={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "测试路径（文件/目录）或文档 URL"},
            "timeout": {
                "type": "number",
                "description": "执行超时秒数",
                "default": DEFAULT_TIMEOUT,
            },
        },
        "required": ["target"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=120.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, run_test)


__all__ = ["DEFAULT_TIMEOUT", "MAX_OUTPUT_CHARS", "USER_AGENT", "run_test", "spec"]
