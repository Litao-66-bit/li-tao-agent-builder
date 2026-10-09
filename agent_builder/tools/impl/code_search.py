"""code_search：在白名单目录内用 ripgrep 做正则搜索。

安全边界：路径沙箱校验由 ToolGatekeeper 负责（path 必须落在 WORKSPACE_DIR 内）。
本模块只做纯逻辑：参数校验、subprocess 调 rg、解析输出、截断、异常映射。
优先用系统装的 ripgrep（``AGENT_BUILDER_RG`` 可显式指定路径）；**找不到就退到内置
纯 Python 扫描器**，保证「搜代码」这项核心能力在任何环境都可用（实测见 ``_python_search``）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.gatekeeper import current_workspace_dir, resolve_in_workspace
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单次搜索返回的最大条目数：超过则截断。
MAX_RESULTS = 200
# rg 命令名；可用环境变量显式指定完整路径（部署侧覆盖）。
RG_BIN = "rg"
RG_ENV_VAR = "AGENT_BUILDER_RG"
# subprocess 超时（秒），由 ToolSpec.timeout_s 兜底，这里防止 rg 自身卡死。
RG_TIMEOUT = 25.0

# 内置扫描器（rg 不可用时的兜底）的边界：避免在大仓库上把任务卡死。
SCAN_MAX_FILES = 2000
SCAN_MAX_FILE_BYTES = 1_000_000
# 与 rg 默认行为对齐：跳过隐藏文件/目录，外加本项目自己的运行时目录。
SCAN_SKIP_DIRS = frozenset(
    {".deps", ".venv", "venv", "node_modules", "__pycache__", ".dsh-scratch"}
)


def _resolve_rg() -> str | None:
    """找 ripgrep：``AGENT_BUILDER_RG`` 环境变量 > PATH。找不到返回 None。"""
    override = os.environ.get(RG_ENV_VAR, "").strip()
    if override:
        return override if Path(override).is_file() else None
    return shutil.which(RG_BIN)


def _iter_scan_files(root: Path) -> list[Path]:
    """内置扫描器要看的文件（有界；跳过隐藏目录与依赖目录，与 rg 默认一致）。"""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SCAN_SKIP_DIRS and not name.startswith(".")
        )
        for name in sorted(filenames):
            found.append(Path(dirpath) / name)
            if len(found) >= SCAN_MAX_FILES:
                return found
    return found


def _python_search(root: Path, pattern: str, max_results: int, base: Path) -> str:
    """rg 不可用时的**内置扫描器**：纯 Python 正则逐行扫描，输出与 rg 同格式。

    为什么必须有它（实测）：``code_search`` 原先把「环境里装了 rg」当**硬依赖** ——
    没装 rg 的环境里，模型一切"搜代码定位函数"的正常动作都直接 ``E_TOOL`` 失败。
    实测一轮任务因此连撞两个失败（``test_run`` + ``code_search``）→ 两轮无产出 →
    空转闸门在**第 3 步**就把整轮掐死。搜代码是核心能力，不该被一个可选外部二进制
    整体废掉。
    """
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise validation_error(
            f"code_search: 正则表达式非法: {exc}",
            source="tool.code_search",
            correlation_id=current_correlation_id.get(),
        ) from exc

    files = [root] if root.is_file() else _iter_scan_files(root)
    lines: list[str] = []
    truncated = False
    for file in files:
        try:
            if file.stat().st_size > SCAN_MAX_FILE_BYTES:
                continue
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # 二进制 / 无权限 / 超大：跳过，不打断整次搜索
        try:
            shown: Path | str = file.relative_to(base)
        except ValueError:
            shown = file
        for lineno, content in enumerate(text.splitlines(), 1):
            if not rx.search(content):
                continue
            if len(lines) >= max_results:
                truncated = True
                break
            lines.append(f"{shown}:{lineno}:{content}")
        if truncated:
            break
    if truncated:
        lines.append(f"…[已截断，仅显示前 {max_results} 条匹配]")
    return "\n".join(lines)


def _builtin_fallback(path: str, pattern: str, max_results: int) -> str:
    """走内置扫描器，并在结果里**说明**换了扫描器（免得模型误以为是 rg 的输出）。"""
    base = current_workspace_dir.get() or Path.cwd()
    root = resolve_in_workspace(path, base)
    note = "（注：未找到 ripgrep，已用内置扫描器；与 rg 默认一致，跳过隐藏文件/目录）"
    found = _python_search(root, pattern, max_results, base) if root.exists() else ""
    return f"{found}\n{note}" if found else note


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
    rg = _resolve_rg()
    if rg is None:
        return _builtin_fallback(path, str(pattern), max_results)

    cmd = [rg, "--json", "-n", str(pattern), str(path)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        # 解析出来的 rg 也起不来（被移走 / 无执行权限）→ 同样退到内置扫描器，
        # 而不是让整轮任务在这一步挂掉。
        return _builtin_fallback(path, str(pattern), max_results)
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


__all__ = [
    "MAX_RESULTS",
    "RG_BIN",
    "RG_ENV_VAR",
    "RG_TIMEOUT",
    "search_code",
    "spec",
]
