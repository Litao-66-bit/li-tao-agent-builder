"""sandbox_run：沙箱内执行命令/脚本（禁网络外发）。

安全边界：
1. cwd 路径校验由 ToolGatekeeper._check_sandbox_path_optional 负责（path 落在 WORKSPACE_DIR 内）。
2. 网络阻断：命令黑名单 + 代理环境变量清理 + Linux unshare(CLONE_NEWNET)。
   Windows 不支持 preexec_fn，仅命令黑名单 + 环境变量清理（软隔离）。
本模块只做纯逻辑：参数校验、命令执行、输出截断、异常映射。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.gatekeeper import workspace_temp_env
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 命令输出最大字符数：防止超大输出撑爆上下文。
MAX_OUTPUT_CHARS = 50_000
# 默认执行超时（秒）。
DEFAULT_TIMEOUT = 30.0

# 网络相关命令黑名单（禁止执行）。
NETWORK_COMMANDS = frozenset({
    "curl", "dig", "ftp", "host", "masscan", "nc", "nmap",
    "netcat", "nslookup", "ping", "scp", "sftp", "ssh",
    "tcpdump", "telnet", "traceroute", "tftp", "wget",
    "wireshark",
})

# 需要清理的代理环境变量。
PROXY_ENV_KEYS = frozenset({
    "ALL_PROXY", "FTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
    "NO_PROXY", "all_proxy", "ftp_proxy", "http_proxy",
    "https_proxy", "no_proxy",
})


def run_command(command: str, *, path: str = "", timeout: float = DEFAULT_TIMEOUT) -> str:
    """在沙箱内执行命令，禁网络外发。

    Args:
        command: 要执行的命令行。
        path: 工作目录（已由门卫校验落在 WORKSPACE_DIR 内；为空则用默认）。
        timeout: 执行超时秒数。

    Returns:
        命令输出（stdout + stderr）；超过 MAX_OUTPUT_CHARS 截断。

    Raises:
        AgentError(E_VALIDATION): command 为空 / timeout 非法 / 含网络命令。
        AgentError(E_TOOL): 执行失败 / 超时。
    """
    cid = current_correlation_id.get()
    if not command or not command.strip():
        raise validation_error(
            "sandbox_run: command 不能为空", source="tool.sandbox_run", correlation_id=cid
        )
    if timeout <= 0:
        raise validation_error(
            f"sandbox_run: timeout 必须为正数: {timeout}",
            source="tool.sandbox_run",
            correlation_id=cid,
        )

    # 命令黑名单检查（取第一个 token 比对）。
    parts = command.strip().split()
    cmd_first = parts[0] if parts else ""
    if cmd_first in NETWORK_COMMANDS:
        raise validation_error(
            f"sandbox_run: 禁止执行网络命令 {cmd_first!r}",
            source="tool.sandbox_run",
            correlation_id=cid,
        )

    # 环境变量清理：移除代理。
    env = dict(os.environ)
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)

    # 子进程 PATH 补上「正在跑本应用」的解释器目录。
    #
    # 后端是用**全路径**解释器启动的，它的 PATH 里通常没有 ``python`` —— 于是生成物里
    # 一句 ``python hello.py`` 直接「'python' is not recognized」（实测模型为此烧了 8 步
    # 去找解释器）。这里把当前解释器所在目录前置进 PATH，与 ``test_run``「用同一个解释器」
    # 的口径一致：不用用户改 PATH，也不会挑到另一个 Python 环境。
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    # 临时目录同样要在工作区内可写（否则被测代码/脚本里用 tempfile 必报 PermissionError）。
    workspace_temp_env(env)

    # 构造 subprocess 参数。
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
    }
    if path:
        kwargs["cwd"] = path
    # Linux: 创建无网络命名空间（unshare CLONE_NEWNET）。
    if sys.platform != "win32":
        kwargs["preexec_fn"] = _network_sandbox_preexec

    try:
        proc = subprocess.run(
            command, shell=True, env=env, check=False, **kwargs
        )
    except subprocess.TimeoutExpired as exc:
        raise tool_error(
            f"sandbox_run: 命令超时（>{timeout}s）",
            source="tool.sandbox_run",
            correlation_id=cid,
        ) from exc
    except Exception as exc:
        raise tool_error(
            f"sandbox_run: 执行失败: {exc}",
            source="tool.sandbox_run",
            correlation_id=cid,
        ) from exc

    output = proc.stdout
    if proc.stderr:
        output += f"\n[stderr]\n{proc.stderr}"
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + f"\n…[已截断，原文 {len(output)} 字符]"
    if proc.returncode != 0:
        # 退出码非零 = 命令失败，**必须是 failed 而不是 done**。
        # 此前只看输出、不看退出码 → 失败被当成成功：实测 8 次「'python' is not recognized」
        # 全部报成「沙箱执行完成」，空转检测因此失效、一路烧到预算上限。
        # 输出原样带进错误信息：循环会把它作为「观察结果」回灌给模型（失败也要看得见）。
        raise tool_error(
            f"sandbox_run: 命令退出码 {proc.returncode}\n{output}",
            source="tool.sandbox_run",
            correlation_id=cid,
        )
    return output


def _network_sandbox_preexec() -> None:
    """Linux: 创建无网络命名空间。失败则忽略（降级为命令黑名单防护）。"""
    import ctypes

    CLONE_NEWNET = 0x40000000
    libc = ctypes.CDLL(None)
    try:
        libc.unshare(CLONE_NEWNET)
    except OSError:
        pass  # 无权限或内核不支持，降级为命令黑名单


spec = ToolSpec(
    name="sandbox_run",
    description="沙箱内执行命令/脚本（禁网络外发：命令黑名单 + 环境清理 + Linux netns）",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令行"},
            "path": {"type": "string", "description": "工作目录（必须在白名单目录内）"},
            "timeout": {
                "type": "number",
                "description": "执行超时秒数",
                "default": DEFAULT_TIMEOUT,
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    risk_level="medium",
    timeout_s=60.0,
    cost_band="medium",
    allowed_roles=["operator"],
)

registry.register(spec, run_command)


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_OUTPUT_CHARS",
    "NETWORK_COMMANDS",
    "PROXY_ENV_KEYS",
    "run_command",
    "spec",
]
