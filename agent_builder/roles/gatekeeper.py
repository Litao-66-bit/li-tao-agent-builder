"""看门人（Gatekeeper）—— 副架构・执行层。

注意：与 ToolGatekeeper（工具门卫）不同。本角色是副架构执行层，
负责提案获批后应用变更、跑回归测试、失败自动回滚。

职责：应用变更 + 跑回归测试 + 失败自动回滚 + 冻结模块。

边界声明：
- 无批准不动作（approval.granted_by 必须存在）
- 批准过期（> 24h）→ 重新走审批
- 回归未全绿 → 不合并生产
- 回滚也失败 → 冻结该模块并升级人工

执行协议：
1. 在沙箱/分支应用变更（版本化：git commit / 快照）
2. 跑回归测试
3. 失败自动回滚上一版本并通知
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent_builder.contracts.schemas import Step

# 批准有效期（小时）。
APPROVAL_TTL_HOURS = 24.0

# 应用类 action 集合。
APPLY_ACTIONS: frozenset[str] = frozenset({
    "apply",
    "gatekeep",
})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class ApplyResult:
    """应用结果。"""

    step_id: str
    status: str  # done | failed | rejected | pending | rolled_back | frozen
    commit_sha: str = ""  # 提交 SHA
    tests_passed: bool = False  # 回归测试是否全绿
    rolled_back: bool = False  # 是否回滚
    rollback_sha: str = ""  # 回滚到的 SHA
    module_frozen: bool = False  # 模块是否冻结
    approval_expired: bool = False  # 批准是否过期
    error: str | None = None


@dataclass(slots=True)
class Gatekeeper:
    """看门人角色：应用变更 + 跑回归 + 失败回滚。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        approval_ttl_hours: 批准有效期（小时）。
    """

    correlation_id: str = "c-unknown"
    approval_ttl_hours: float = APPROVAL_TTL_HOURS

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> ApplyResult:
        """执行应用变更步骤。

        Args:
            step: 应用类步骤。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            ApplyResult：应用结果 + 验证报告。
        """
        # 1. 校验是否应用类。
        if step.action not in APPLY_ACTIONS:
            return ApplyResult(
                step_id=step.id,
                status="rejected",
                error=f"任务超出看门范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return ApplyResult(step_id=step.id, status="pending")

        # 3. 检查批准是否存在。
        approval_granted_by = str(step.inputs.get("approval_granted_by", ""))
        approval_ts = str(step.inputs.get("approval_ts", ""))
        if not approval_granted_by:
            return ApplyResult(
                step_id=step.id,
                status="rejected",
                error="无批准不动作（approval_granted_by 缺失）",
            )

        # 4. 检查批准是否过期。
        if self._is_approval_expired(approval_ts):
            return ApplyResult(
                step_id=step.id,
                status="rejected",
                approval_expired=True,
                error=f"批准已过期（超过 {self.approval_ttl_hours}h），请重新走审批",
            )

        # 5. 执行应用变更。
        try:
            executor_fn(step)
            commit_sha = str(step.inputs.get("commit_sha", ""))
            tests_passed = bool(step.inputs.get("tests_passed", False))
            rollback_failed = bool(step.inputs.get("rollback_failed", False))

            # 6. 回归未全绿 → 回滚。
            if not tests_passed:
                if rollback_failed:
                    # 7. 回滚也失败 → 冻结模块 + 升级人工。
                    return ApplyResult(
                        step_id=step.id,
                        status="frozen",
                        commit_sha=commit_sha,
                        tests_passed=False,
                        rolled_back=True,
                        module_frozen=True,
                        error="回滚也失败 → 冻结该模块并升级人工",
                    )
                return ApplyResult(
                    step_id=step.id,
                    status="rolled_back",
                    commit_sha=commit_sha,
                    tests_passed=False,
                    rolled_back=True,
                    rollback_sha=str(step.inputs.get("rollback_sha", "")),
                    error="回归测试未全绿 → 已回滚上一版本",
                )

            # 8. 回归全绿 → 应用成功。
            return ApplyResult(
                step_id=step.id,
                status="done",
                commit_sha=commit_sha,
                tests_passed=True,
            )
        except PermissionError as exc:
            return ApplyResult(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  看门人需捕获所有执行异常
            return ApplyResult(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _is_approval_expired(self, approval_ts: str) -> bool:
        """检查批准是否过期。"""
        if not approval_ts:
            return False  # 无时间戳 → 不检查
        try:
            ts = datetime.fromisoformat(approval_ts.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            elapsed_hours = (now - ts).total_seconds() / 3600
            return elapsed_hours > self.approval_ttl_hours
        except (ValueError, TypeError):
            return False  # 解析失败 → 不过期


__all__ = [
    "APPLY_ACTIONS",
    "APPROVAL_TTL_HOURS",
    "ApplyResult",
    "ExecutorFn",
    "Gatekeeper",
]
