"""工具门卫（ToolGuardian）—— 主架构・工具层。

职责：校验工具白名单 + 参数安全 + 审计日志（唯一出口，不可绕过）。

与 Gatekeeper（看门人）的区别：本角色是所有角色发起工具调用的**唯一出入口**，
负责放行 / 拒绝 / 转审批；Gatekeeper 属副架构执行层，负责获批后的变更应用与回归回滚。

边界声明：
- 唯一出口：任何角色发起的工具调用都必须经过本角色，不可绕过
- 白名单：角色未注册 / 工具未列入该角色白名单 → 拒绝（E_PERMISSION，永不重试）
- 高风险动作（删除 / 覆盖 / 外发）→ 转审批门，等用户确认
- 怀疑注入 → 拒绝 + 标记事件上报审计员

执行协议：
1. 注入预检：args 命中注入模式 → 拒绝并标记事件上报审计员
2. 高风险动作（删除 / 覆盖 / 外发）且无审批人 → 转审批门，等用户确认
3. 白名单 + 参数安全：复用 tools/gatekeeper.ToolGatekeeper
   （角色权限 / 沙箱路径 realpath / URL 域名 / 目标命令）
4. 放行后交给 executor_fn 执行，并写审计日志（谁调的、参数、结果、耗时）
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import WORKSPACE_DIR, ToolGatekeeper
from agent_builder.tools.guard import INJECTION_PATTERNS
from agent_builder.tools.permissions import get_default_role_perms

# 注入检测的行长阈值：仅"短行"视为可疑指令，长行更可能是正文（与 tools/guard.py 同口径）。
INJECTION_LINE_MAX = 120

# 执行函数类型：接收 ToolCall，返回工具结果。
ExecutorFn = Callable[[ToolCall], Any]


@dataclass(slots=True)
class GuardResult:
    """一次工具调用的门卫结果（完成标志：工具结果 或 拒绝原因）。"""

    audit_id: str
    role: str
    tool: str
    status: str  # executed | allowed | denied | pending_approval | failed
    reason: str = ""
    result: Any | None = None
    elapsed_ms: float = 0.0  # 执行耗时（毫秒）
    injection_suspected: bool = False  # 是否怀疑注入（已标记事件上报审计员）


@dataclass(slots=True)
class ToolGuardian:
    """工具门卫角色：白名单 + 参数安全 + 审计日志。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        workspace_dir: 沙箱白名单目录（文件类工具的 realpath 必须落在其内）。
        role_perms: 角色权限矩阵（默认取全量默认矩阵）。
    """

    correlation_id: str = "c-unknown"
    workspace_dir: Path = WORKSPACE_DIR
    role_perms: dict[str, RolePerm] = field(default_factory=get_default_role_perms)
    engine: ToolGatekeeper = field(init=False)
    audit_log: list[GuardResult] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        # 校验内核复用工具层实现：唯一出口不可绕过。
        self.engine = ToolGatekeeper(
            role_perms=self.role_perms,
            workspace_dir=self.workspace_dir,
            correlation_id=self.correlation_id,
        )

    def guard(self, call: ToolCall, executor_fn: ExecutorFn | None = None) -> GuardResult:
        """校验一次工具调用；通过则（可选）执行并记录审计。

        Args:
            call: 工具调用记录（角色 / 工具 / 参数 / 审批）。
            executor_fn: 执行函数（接收 ToolCall 返回结果）；None 则只校验不执行。

        Returns:
            GuardResult：工具结果（executed / allowed）或拒绝原因（denied / pending_approval）。
        """
        # 1. 注入预检（在权限校验之前拦截，避免可疑参数进入执行链）。
        if self._suspected_injection(call.args):
            return self._record(
                call,
                status="denied",
                reason="参数命中注入模式 → 拒绝并标记事件上报审计员",
                injection_suspected=True,
            )

        # 2. 高风险动作（删除 / 覆盖 / 外发）→ 转审批门，等用户确认。
        if self._needs_approval(call):
            return self._record(
                call,
                status="pending_approval",
                reason="高风险动作需用户审批 → 转审批门",
            )

        # 3. 白名单 + 参数安全（角色权限 / 沙箱路径 / URL / 目标）。
        try:
            self.engine.check(call)
        except AgentError as exc:
            return self._record(call, status="denied", reason=str(exc))

        # 4. 放行：无执行函数 → 只校验不执行。
        if executor_fn is None:
            return self._record(call, status="allowed", reason="校验通过（未执行）")

        # 5. 执行并记录结果与耗时（异常不吞：转 failed 并留痕）。
        start = time.perf_counter()
        try:
            result = executor_fn(call)
        except Exception as exc:  # noqa: BLE001  门卫需捕获所有执行异常并留痕
            return self._record(
                call,
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
                elapsed_ms=self._elapsed_ms(start),
            )
        call.result = result
        return self._record(
            call,
            status="executed",
            result=result,
            elapsed_ms=self._elapsed_ms(start),
        )

    def snapshot(self) -> list[dict[str, Any]]:
        """角色层审计快照（含结果与耗时，补充门卫内核的审计记录）。"""
        return [
            {
                "audit_id": r.audit_id,
                "role": r.role,
                "tool": r.tool,
                "status": r.status,
                "reason": r.reason,
                "elapsed_ms": r.elapsed_ms,
                "injection_suspected": r.injection_suspected,
            }
            for r in self.audit_log
        ]

    # ── 内部 ─────────────────────────────────────────────────────

    def _suspected_injection(self, args: dict[str, Any]) -> bool:
        """递归扫描参数中的字符串，命中注入模式即视为可疑。"""
        return self._scan(args)

    @classmethod
    def _scan(cls, value: Any) -> bool:
        if isinstance(value, str):
            return cls._has_injection(value)
        if isinstance(value, dict):
            return any(cls._scan(v) for v in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(cls._scan(v) for v in value)
        return False

    @staticmethod
    def _has_injection(text: str) -> bool:
        """仅"短行"命中模式才判可疑，避免误伤含关键词的长正文。"""
        for line in text.splitlines():
            stripped = line.strip()
            if len(stripped) < INJECTION_LINE_MAX and any(
                p in stripped.lower() for p in INJECTION_PATTERNS
            ):
                return True
        return False

    def _needs_approval(self, call: ToolCall) -> bool:
        """高风险动作且尚无审批人 → 应转审批门（等用户确认）。"""
        perm = self.role_perms.get(call.role)
        if perm is None:
            return False
        return perm.is_high_risk(call.tool) and not call.approval.granted_by

    def _record(
        self,
        call: ToolCall,
        *,
        status: str,
        reason: str = "",
        result: Any | None = None,
        elapsed_ms: float = 0.0,
        injection_suspected: bool = False,
    ) -> GuardResult:
        entry = GuardResult(
            audit_id=call.audit_id,
            role=call.role,
            tool=call.tool,
            status=status,
            reason=reason,
            result=result,
            elapsed_ms=elapsed_ms,
            injection_suspected=injection_suspected,
        )
        self.audit_log.append(entry)
        return entry

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        return round((time.perf_counter() - start) * 1000, 3)


__all__ = [
    "INJECTION_LINE_MAX",
    "ExecutorFn",
    "GuardResult",
    "ToolGuardian",
]
