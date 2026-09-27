"""工具门卫（ToolGatekeeper）最小实现。

架构规则：所有工具调用只经门卫这一个出口（对应契约 03 消息协议 TOOL_GATEKEEPER）。
第一版职责：
1. 角色权限校验（复用 RolePerm：未列出的一律拒绝，高风险需审批标记）。
2. 文件类工具的沙箱路径校验（realpath 必须在白名单目录内）。
3. 写审计记录（追加到门卫审计列表，后续由审计员消费）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_builder.contracts.errors import permission_error
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.redact import redact_args
from agent_builder.tools.url_guard import validate_url

# 默认白名单目录：只有工作区内的路径可写（防路径穿越/越权写系统目录）。
WORKSPACE_DIR = Path("/home/user/Doubao/chats/38443251841377538")


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
        if tool_call.tool in ("file_write", "file_read", "file_edit", "file_list", "code_search"):
            self._check_sandbox_path(tool_call)

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
        path_str = str(tool_call.args.get("path", ""))
        if not path_str:
            self._reject(tool_call, "文件工具缺少 path 参数")
            raise permission_error(
                "文件工具缺少 path 参数", source="tool_gatekeeper", correlation_id=self.correlation_id
            )
        candidate = Path(path_str).resolve()
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
            self._reject(tool_call, "web 工具缺少 url 参数")
            raise permission_error(
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


__all__ = ["WORKSPACE_DIR", "ToolAudit", "ToolGatekeeper"]
