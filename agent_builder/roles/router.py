"""路由者（Router）—— 主架构・执行层。

职责：按任务类型匹配专业执行者，派发步骤，监控进度与超时，
回收各执行者产出，汇总为执行结果集。

边界声明：
- 只路由不执行：不直接调用工具（由执行者调用）
- 步骤失败 → 重派 1 次；仍失败 → 上报总指挥
- 不可重试错误（`AgentError.retryable=False`，或编排层在异常上标注的 `retryable=False`）→ **不重派**，直接上报
- 执行者返回"权限不足" → 转发审批门请求用户
- 无匹配执行者 → 上报总指挥

执行协议：
1. 接收已确认的 Plan + steps
2. 按步骤的 action 匹配执行者类型
3. 派发步骤（executor_fn 执行；None 则只匹配不执行）
4. 监控进度（超时 → 标记失败重派）
5. 回收产出 → 汇总执行结果集
6. 失败/超时/权限不足 → 分别走重派/上报/审批门
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.errors import AgentError, validation_error
from agent_builder.contracts.schemas import Plan, Step

# 步骤失败最大重派次数。
MAX_RETRIES = 1

# action → 执行者类型映射。
EXECUTOR_MAP: dict[str, str] = {
    # 代码类。
    "code_search": "code_executor",
    "file_write": "code_executor",
    "file_delete": "code_executor",
    "file_read": "code_executor",
    "file_list": "code_executor",
    # 检索类。
    "web_search": "search_executor",
    # 文档类。
    "web_fetch": "doc_executor",
    "citation_check": "doc_executor",
    # 数据类。
    "data_query": "data_executor",
    "sandbox_run": "data_executor",
    "test_run": "data_executor",
    # 记忆类。
    "memory_read": "memory_executor",
    "memory_write": "memory_executor",
    "memory_forget": "memory_executor",
    # 治理类。
    "audit_log": "governance_executor",
    "metric_collect": "governance_executor",
    "config_read": "governance_executor",
    "diff_preview": "governance_executor",
    "plan_validate": "governance_executor",
    # 副架构类。
    "git_commit": "sub_arch_executor",
    "rollback": "sub_arch_executor",
    "git_log": "sub_arch_executor",
    # 框架类。
    "approval_request": "framework_executor",
    "change_notify": "framework_executor",
}

# 默认执行者（类型不明时）。
DEFAULT_EXECUTOR = "general_executor"

# 执行函数类型：接收 Step，返回结果或抛异常。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class ExecutionResult:
    """单步执行结果。"""

    step_id: str
    status: str  # done | failed | skipped | pending_approval | pending
    result: Any | None = None
    error: str | None = None
    executor: str = ""
    retries: int = 0


@dataclass(slots=True)
class RouteResult:
    """路由结果：执行结果集 + 待上报 + 待审批。"""

    results: dict[str, ExecutionResult] = field(default_factory=dict)
    pending_escalation: list[str] = field(default_factory=list)
    pending_approval: list[str] = field(default_factory=list)

    @property
    def all_done(self) -> bool:
        """所有步骤是否完成（或明确失败上报）。"""
        return not self.pending_escalation and not self.pending_approval


@dataclass(slots=True)
class Router:
    """路由者角色：匹配执行者 + 派发 + 监控 + 回收。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        max_retries: 步骤失败最大重派次数。
    """

    correlation_id: str = "c-unknown"
    max_retries: int = MAX_RETRIES

    def route(
        self,
        plan: Plan,
        steps: dict[str, Step],
        executor_fn: ExecutorFn | None = None,
    ) -> RouteResult:
        """路由执行计划。

        Args:
            plan: 已确认的执行计划（confirmed_by_user=True）。
            steps: 步骤字典。
            executor_fn: 执行函数（接收 Step，返回结果）；
                None 则只匹配执行者不执行（返回 pending 状态）。

        Returns:
            RouteResult：执行结果集 + 待上报 + 待审批。

        Raises:
            AgentError(E_VALIDATION): plan 未确认 / steps 为空。
        """
        cid = self.correlation_id
        if not plan.confirmed_by_user:
            raise validation_error(
                "router: 执行计划未经用户确认（confirmed_by_user=False）",
                source="router",
                correlation_id=cid,
            )
        if not steps:
            return RouteResult()

        results: dict[str, ExecutionResult] = {}
        pending_escalation: list[str] = []
        pending_approval: list[str] = []

        # 按 parallel_groups 顺序执行（同组内顺序执行，后续可并行化）。
        for group in plan.parallel_groups:
            for sid in group:
                if sid not in steps:
                    pending_escalation.append(sid)
                    continue
                step = steps[sid]
                executor = self._match_executor(step.action)

                if executor_fn is None:
                    # 只匹配执行者，不执行。
                    results[sid] = ExecutionResult(
                        step_id=sid,
                        status="pending",
                        executor=executor,
                    )
                    continue

                # 实际执行（含重派逻辑）。
                result = self._execute_step(sid, step, executor, executor_fn)
                results[sid] = result

                # 分类处理失败。
                if result.status == "pending_approval":
                    pending_approval.append(sid)
                elif result.status == "failed":
                    # 失败即上报：可重试的已耗尽重派；不可重试的立即上报（retries 可能为 0）。
                    pending_escalation.append(sid)

        return RouteResult(
            results=results,
            pending_escalation=pending_escalation,
            pending_approval=pending_approval,
        )

    # ── 内部方法 ──────────────────────────────────────────────

    def _match_executor(self, action: str) -> str:
        """按 action 匹配执行者类型。"""
        return EXECUTOR_MAP.get(action, DEFAULT_EXECUTOR)

    def _execute_step(
        self,
        sid: str,
        step: Step,
        executor: str,
        executor_fn: ExecutorFn,
    ) -> ExecutionResult:
        """执行单步（含重派逻辑）。

        超时 → 标记失败重派；权限不足 → 转发审批门；其他异常 → 重派 1 次。
        """
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                result = executor_fn(step)
                return ExecutionResult(
                    step_id=sid,
                    status="done",
                    result=result,
                    executor=executor,
                    retries=attempt,
                )
            except TimeoutError as exc:
                last_error = f"超时: {exc}"
                if attempt < self.max_retries:
                    continue
            except PermissionError as exc:
                # 权限不足 → 转发审批门（不重派）。
                return ExecutionResult(
                    step_id=sid,
                    status="pending_approval",
                    error=f"权限不足: {exc}",
                    executor=executor,
                    retries=attempt,
                )
            except AgentError as exc:
                # 契约（contracts/errors）：按 ``retryable`` 决定是否重派。
                # 非可重试错误（E_PERMISSION / E_COST / E_INTERNAL / E_USER_CANCEL）
                # 属确定性失败，重派无意义 → 直接记为 failed（retries=attempt）。
                labeled = f"{exc.error_name}: {exc.info.message}"
                if not exc.retryable:
                    return ExecutionResult(
                        step_id=sid,
                        status="failed",
                        error=labeled,
                        executor=executor,
                        retries=attempt,
                    )
                last_error = labeled
                if attempt < self.max_retries:
                    continue
            except Exception as exc:  # noqa: BLE001  路由者需捕获所有执行异常
                last_error = f"{type(exc).__name__}: {exc}"
                # 编排层可在异常上标注 retryable=False（角色已判定确定性失败）→ 不重派。
                if getattr(exc, "retryable", True) is False:
                    return ExecutionResult(
                        step_id=sid,
                        status="failed",
                        error=last_error,
                        executor=executor,
                        retries=attempt,
                    )
                if attempt < self.max_retries:
                    continue
        return ExecutionResult(
            step_id=sid,
            status="failed",
            error=last_error,
            executor=executor,
            retries=self.max_retries,
        )


__all__ = [
    "DEFAULT_EXECUTOR",
    "EXECUTOR_MAP",
    "MAX_RETRIES",
    "ExecutionResult",
    "ExecutorFn",
    "RouteResult",
    "Router",
]
