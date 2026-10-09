"""工具门卫（ToolGatekeeper）最小实现。

架构规则：所有工具调用只经门卫这一个出口（对应契约 03 消息协议 TOOL_GATEKEEPER）。
第一版职责：
1. 角色权限校验（复用 RolePerm：未列出的一律拒绝，高风险需审批标记）。
2. 文件类工具的沙箱路径校验（realpath 必须在白名单目录内）。
3. 写审计记录（追加到门卫审计列表，后续由审计员消费）。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_builder.contracts.errors import permission_error, validation_error
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.redact import redact_args
from agent_builder.tools.url_guard import validate_url

# 默认白名单目录：只有工作区内的路径可写（防路径穿越/越权写系统目录）。
WORKSPACE_DIR = Path("/home/user/Doubao/chats/38443251841377538")

# 「当前工作区」上下文：门卫用自己的 ``workspace_dir`` 校验，而**工具实现拿不到门卫实例**，
# 只能通过它把相对路径解析到同一个基准（由 ``registry.execute`` 注入）。
# 默认 None → 调用时退回进程 cwd（保持「不走门卫直接调实现」时的既有行为）。
current_workspace_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "tool_workspace_dir", default=None
)

# 工作区内可写临时目录的名字（隐藏目录：不进产物树、不进 lint、不进 git）。
WORKSPACE_TEMP_DIRNAME = ".agent-tmp"


def workspace_temp_dir() -> Path:
    """当前工作区内的可写临时目录（不存在则创建）。"""
    root = current_workspace_dir.get() or Path.cwd()
    tmp = Path(root) / WORKSPACE_TEMP_DIRNAME
    try:
        tmp.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(root)  # 建不出来就退回工作区根：至少是可写的
    return tmp


def workspace_temp_env(env: dict[str, str]) -> dict[str, str]:
    """把 ``TMP``/``TEMP``/``TMPDIR`` 指向工作区内的可写目录（原地改并返回同一个 dict）。

    为什么必须这么做：实测（用户任务 ``0ecdbfa8`` 的工作区）agent 自己写的
    ``test_cli_end_to_end`` 在沙箱下报 ``PermissionError: [Errno 13]`` —— Python 用
    ``mode=0o700`` 建临时目录时写的是**显式权限**，绕过了父目录继承下来的授权，
    连创建者自己都打不开。结果模型看到一个**它永远修不好**的失败，反复读文件烧预算。
    """
    tmp = str(workspace_temp_dir())
    for key in ("TMP", "TEMP", "TMPDIR"):
        env[key] = tmp
    return env


# 产物输出根目录的名字：**新产出的交付物**统一落在这里。
#
# 为什么需要这条策略（实测）：工作区常常**就是本项目仓库根**（默认工作区即项目目录），
# 于是 agent 写出的 `paper_agent.py` / `test_paper_agent.py` 直接躺在仓库根 —— 本地
# `ruff check .` 会被它们扫到并报错（实测 RUF022），CI 里也容易被 pytest 收集。
# 约定一个新产物落到 `outputs/` 下，就从根上避免"agent 的产出污染项目本身"。
#
# 注意这是**策略而非沙箱规则**：工具层不做拦截 —— **修改工作区已有文件**时仍应写回原路径
# （例如把仓库里那份 `paper_agent.py` 的签名改对），只有**新建**的交付物才放 `outputs/`。
OUTPUT_ROOT_DIRNAME = "outputs"


def output_root(base: Path | None = None) -> Path:
    """本次任务的产物输出根目录（工作区下的 ``outputs/``；不存在则创建）。"""
    root = base if base is not None else (current_workspace_dir.get() or Path.cwd())
    target = Path(root) / OUTPUT_ROOT_DIRNAME
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(root)  # 建不出来就退回工作区根：至少是可写的
    return target


def resolve_in_workspace(path: str | Path, base: Path | None = None) -> Path:
    """把工具路径解析到**沙箱基准**下：相对路径按工作区解析，而不是按进程 cwd。

    背景（实测 bug）：此前门卫与各实现都写 ``Path(path).resolve()`` —— 相对路径按 **cwd**
    解析。默认工作区恰好等于 cwd 时看不出问题；一旦用户切换工作区，``path="tests"`` 会指到
    cwd 下的 tests，于是① 被门卫误判「超出沙箱白名单」，② 即便放行也读写了错误的目录。

    基准优先级：显式 ``base`` > ``current_workspace_dir`` 上下文 > 进程 cwd。
    **绝对路径原样 resolve**（是否越界仍由门卫判定）；``..`` 由 resolve 归一化后再比对。
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        root = base if base is not None else current_workspace_dir.get()
        candidate = (root if root is not None else Path.cwd()) / candidate
    return candidate.resolve()


@dataclass(slots=True)
class ToolAudit:
    """一次工具调用的门卫审计记录。"""

    audit_id: str
    role: str
    tool: str
    args: dict[str, Any]
    allowed: bool
    reason: str
    correlation_id: str


class ToolGatekeeper:
    """唯一执行出口。check() 不通过直接抛 E_PERMISSION（永不重试）。"""

    def __init__(
        self,
        role_perms: dict[str, RolePerm],
        *,
        workspace_dir: Path = WORKSPACE_DIR,
        correlation_id: str = "c-unknown",
    ) -> None:
        self.role_perms = role_perms
        self.workspace_dir = workspace_dir.resolve()
        self.correlation_id = correlation_id
        self.audit_log: list[ToolAudit] = []

    # ── 入口 ─────────────────────────────────────────────────────

    def check(self, tool_call: ToolCall) -> ToolCall:
        """校验一次工具调用；返回审批后的调用记录（状态置 executed）。"""
        perm = self.role_perms.get(tool_call.role)
        if perm is None:
            self._reject(tool_call, f"角色 {tool_call.role!r} 未注册权限矩阵")
            raise permission_error(
                f"角色 {tool_call.role!r} 未注册权限矩阵，工具调用被拒绝",
                source="tool_gatekeeper",
                correlation_id=self.correlation_id,
            )
        if not perm.is_allowed(tool_call.tool):
            self._reject(tool_call, f"工具 {tool_call.tool!r} 未列入角色白名单")
            raise permission_error(
                f"工具 {tool_call.tool!r} 未列入角色 {tool_call.role!r} 白名单",
                source="tool_gatekeeper",
                correlation_id=self.correlation_id,
            )

        # 高风险工具：审批门（contracts/schemas.Approval.required）。
        if perm.is_high_risk(tool_call.tool) and not tool_call.is_approved():
            self._reject(tool_call, "高风险工具缺少审批")
            raise permission_error(
                f"高风险工具 {tool_call.tool!r} 需要用户审批，当前未授权",
                source="tool_gatekeeper",
                correlation_id=self.correlation_id,
            )

        # 文件类工具：沙箱路径校验（新增工具按需加入元组，不改已有校验逻辑）。
        if tool_call.tool in ("file_write", "file_delete", "file_read", "file_edit", "file_list", "code_search", "data_query", "git_commit", "rollback", "git_log"):
            self._check_sandbox_path(tool_call)

        # sandbox_run：可选 path 校验（path 存在则校验沙箱，不存在则跳过）。
        if tool_call.tool == "sandbox_run":
            self._check_sandbox_path_optional(tool_call)

        # test_run：target 混合校验（URL → url_guard，路径 → 沙箱）。
        if tool_call.tool == "test_run":
            self._check_target_safety(tool_call)

        # web 类工具：URL 安全校验（防 SSRF，新增分支，不影响文件类校验）。
        if tool_call.tool in ("web_fetch", "web_search"):
            self._check_url_safety(tool_call)

        tool_call.status = "executed"
        self.audit_log.append(
            ToolAudit(
                audit_id=tool_call.audit_id,
                role=tool_call.role,
                tool=tool_call.tool,
                args=redact_args(tool_call.args),  # P0-3：审计留档前脱敏，防密钥泄漏
                allowed=True,
                reason="permission_matrix_ok",
                correlation_id=self.correlation_id,
            )
        )
        return tool_call

    # ── 内部校验 ─────────────────────────────────────────────────

    def _check_sandbox_path(self, tool_call: ToolCall) -> None:
        path_str = str(tool_call.args.get("path") or tool_call.args.get("repo_path") or "")
        if not path_str:
            # 参数缺失 = E_VALIDATION，不是权限问题。
            # 此前报 E_PERMISSION，把「模型忘了给参数」说成「没权限」—— 实测中模型因此
            # 误判为「换个动作」，用户/审计也会把它记成权限被拒。同口径见 registry 的
            # 「缺少必填参数」分支（那也是 E_VALIDATION）。
            self._reject(tool_call, "文件工具缺少 path/repo_path 参数")
            raise validation_error(
                "文件工具缺少 path/repo_path 参数", source="tool_gatekeeper", correlation_id=self.correlation_id
            )
        candidate = resolve_in_workspace(path_str, self.workspace_dir)
        try:
            candidate.relative_to(self.workspace_dir)
        except ValueError:
            self._reject(tool_call, f"路径 {path_str} 超出沙箱白名单 {self.workspace_dir}")
            raise permission_error(
                f"路径 {path_str} 超出沙箱白名单",
                source="tool_gatekeeper",
                correlation_id=self.correlation_id,
            ) from None

    def _reject(self, tool_call: ToolCall, reason: str) -> None:
        tool_call.status = "denied"
        self.audit_log.append(
            ToolAudit(
                audit_id=tool_call.audit_id,
                role=tool_call.role,
                tool=tool_call.tool,
                args=redact_args(tool_call.args),  # P0-3：拒绝记录同样脱敏
                allowed=False,
                reason=reason,
                correlation_id=self.correlation_id,
            )
        )

    def _check_url_safety(self, tool_call: ToolCall) -> None:
        """web 工具 URL 安全校验（防 SSRF）。

        web_search 的 URL 在实现内部构造（args 无 url），门卫跳过；
        web_fetch 的 url 在 args 里，门卫校验。
        实现内部对内部构造的 URL 二次校验（双层防护）。
        """
        url = tool_call.args.get("url")
        if url is None:
            return  # 无 url 字段（如 web_search），由实现内部校验构造的 URL
        if not str(url).strip():
            # 同 _check_sandbox_path：缺参数是 E_VALIDATION，不是权限问题。
            self._reject(tool_call, "web 工具缺少 url 参数")
            raise validation_error(
                "web 工具缺少 url 参数",
                source="tool_gatekeeper",
                correlation_id=self.correlation_id,
            )
        try:
            validate_url(str(url))
        except ValueError as exc:
            self._reject(tool_call, f"URL 安全校验失败: {exc}")
            raise permission_error(
                f"web 工具 URL 校验失败: {exc}",
                source="tool_gatekeeper",
                correlation_id=self.correlation_id,
            ) from exc

    def _check_sandbox_path_optional(self, tool_call: ToolCall) -> None:
        """sandbox_run 的 path 参数可选校验：存在则校验沙箱，不存在则跳过。"""
        path_str = str(tool_call.args.get("path", "")).strip()
        if not path_str:
            return  # 可选参数，无值则跳过
        candidate = resolve_in_workspace(path_str, self.workspace_dir)
        try:
            candidate.relative_to(self.workspace_dir)
        except ValueError:
            self._reject(tool_call, f"路径 {path_str} 超出沙箱白名单 {self.workspace_dir}")
            raise permission_error(
                f"路径 {path_str} 超出沙箱白名单",
                source="tool_gatekeeper",
                correlation_id=self.correlation_id,
            ) from None

    def _check_target_safety(self, tool_call: ToolCall) -> None:
        """test_run 的 target 混合校验：URL → url_guard，路径 → 沙箱。"""
        target = str(tool_call.args.get("target", "")).strip()
        if not target:
            # 同 _check_sandbox_path：缺参数是 E_VALIDATION，不是权限问题。
            self._reject(tool_call, "test_run 缺少 target 参数")
            raise validation_error(
                "test_run 缺少 target 参数",
                source="tool_gatekeeper",
                correlation_id=self.correlation_id,
            )
        if target.startswith(("http://", "https://")):
            try:
                validate_url(target)
            except ValueError as exc:
                self._reject(tool_call, f"URL 安全校验失败: {exc}")
                raise permission_error(
                    f"test_run URL 校验失败: {exc}",
                    source="tool_gatekeeper",
                    correlation_id=self.correlation_id,
                ) from exc
        else:
            candidate = resolve_in_workspace(target, self.workspace_dir)
            try:
                candidate.relative_to(self.workspace_dir)
            except ValueError:
                self._reject(tool_call, f"路径 {target} 超出沙箱白名单 {self.workspace_dir}")
                raise permission_error(
                    f"路径 {target} 超出沙箱白名单",
                    source="tool_gatekeeper",
                    correlation_id=self.correlation_id,
                ) from None

    # ── 查询 ─────────────────────────────────────────────────────

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "audit_id": a.audit_id,
                "role": a.role,
                "tool": a.tool,
                "allowed": a.allowed,
                "reason": a.reason,
            }
            for a in self.audit_log
        ]


__all__ = [
    "OUTPUT_ROOT_DIRNAME",
    "WORKSPACE_DIR",
    "ToolAudit",
    "ToolGatekeeper",
    "current_workspace_dir",
    "output_root",
    "resolve_in_workspace",
]
