"""code_search：在白名单目录内用 ripgrep 做正则搜索。

安全边界：路径沙箱校验由 ToolGatekeeper 负责（path 必须落在 WORKSPACE_DIR 内）。
本模块只做纯逻辑：参数校验、subprocess 调 rg、解析输出、截断、异常映射。
依赖系统已安装 ripgrep（rg 命令）。
"""

from __future__ import annotations

import json
import subprocess

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单次搜索返回的最大条目数：超过则截断。
MAX_RESULTS = 200
# rg 命令名。
RG_BIN = "rg"
# subprocess 超时（秒），由 ToolSpec.timeout_s 兜底，这里防止 rg 自身卡死。
RG_TIMEOUT = 25.0


def search_code(path: str, pattern: str, max_results: int = MAX_RESULTS) -> str:
    """在白名单目录内用 ripgrep 做正则搜索。

    Args:
        path: 搜索根目录（已由门卫校验落在 WORKSPACE_DIR 内）。
        pattern: 正则表达式。
        max_results: 返回的最大条目数，默认 MAX_RESULTS。

    Returns:
        每行一个匹配，格式 ``file:line:content``。
        无匹配返回空字符串。

    Raises:
        AgentError(E_VALIDATION): path/pattern 为空 / max_results 非法。
        AgentError(E_TOOL): rg 不可用 / 执行失败 / 超时。
    """
    cid = current_correlation_id.get()
    if not path or not str(path).strip():
        raise validation_error(
            "code_search: path 不能为空", source="tool.code_search", correlation_id=cid
        )
    if not pattern or not str(pattern).strip():
        raise validation_error(
            "code_search: pattern 不能为空", source="tool.code_search", correlation_id=cid
        )
    if max_results <= 0:
        raise validation_error(
            f"code_search: max_results 必须为正数: {max_results}",
            source="tool.code_search",
            correlation_id=cid,
        )

    # rg --json 输出可解析的 JSON Lines；-n 显示行号；--no-heading 紧凑输出。
    cmd = [RG_BIN, "--json", "-n", str(pattern), str(path)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise tool_error(
            f"code_search: ripgrep 未安装或不在 PATH: {exc}",
            source="tool.code_search",
            correlation_id=cid,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise tool_error(
            f"code_search: 搜索超时（>{RG_TIMEOUT}s）",
            source="tool.code_search",
            correlation_id=cid,
        ) from exc

    # rg 退出码：0=有匹配，1=无匹配，2=错误。
    if proc.returncode == 2:
        raise tool_error(
            f"code_search: rg 执行错误: {proc.stderr.strip()}",
            source="tool.code_search",
            correlation_id=cid,
        )

    lines: list[str] = []
    truncated = False
    for raw_line in proc.stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        # rg --json 的 match 记录格式：{"type":"match","data":{"path":{"text":...},"line_number":N,"lines":{"text":...}}}
        if obj.get("type") != "match":
            continue
        data = obj.get("data", {})
        file_path = data.get("path", {}).get("text", "")
        line_no = data.get("line_number", 0)
        content = data.get("lines", {}).get("text", "").rstrip("\n")
        if len(lines) >= max_results:
            truncated = True
            break
        lines.append(f"{file_path}:{line_no}:{content}")

    if truncated:
        lines.append(f"…[已截断，仅显示前 {max_results} 条匹配]")
    return "\n".join(lines)


spec = ToolSpec(
    name="code_search",
    description="在白名单目录内用 ripgrep 做正则搜索，返回 file:line:content",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "搜索根目录（必须在白名单目录内）"},
            "pattern": {"type": "string", "description": "正则表达式"},
            "max_results": {
                "type": "integer",
                "description": "返回的最大条目数",
                "default": MAX_RESULTS,
            },
        },
        "required": ["path", "pattern"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=30.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, search_code)


__all__ = ["MAX_RESULTS", "RG_BIN", "RG_TIMEOUT", "search_code", "spec"]
