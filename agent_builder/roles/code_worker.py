"""代码执行者（CodeWorker）—— 主架构・执行层。

职责：写/改代码，遵守既有目录结构与命名规范，自带最小自测。
所有外部动作走工具门卫（不绕过）。

边界声明：
- 需要新依赖 → 先写入"待批准"（不擅自装包）
- 任务超出代码范围 → 拒绝并说明
- 自测失败 → 先自查修复，2 次仍失败 → 如实上报（不交半成品）

执行协议：
1. 读取任务上下文与相关代码文件（经记忆管家检索）
2. 写/改代码，遵守既有目录结构与命名规范
3. 自带最小自测（能跑通再交）；所有外部动作走工具门卫
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 自测失败最大自查次数。
MAX_SELF_CHECK_RETRIES = 2

# 代码类 action 集合。
CODE_ACTIONS: frozenset[str] = frozenset({
    "code_search",
    "file_write",
    "file_read",
    "file_list",
})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class CodeResult:
    """代码执行结果。"""

    step_id: str
    status: str  # done | failed | pending_approval | rejected | pending
    files_changed: list[str] = field(default_factory=list)
    change_desc: str = ""
    run_instructions: str = ""
    self_check_passed: bool = False
    error: str | None = None
    pending_dependencies: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CodeWorker:
    """代码执行者角色：写/改代码 + 自测 + 变更说明。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        max_self_check_retries: 自测失败最大自查次数。
    """

    correlation_id: str = "c-unknown"
    max_self_check_retries: int = MAX_SELF_CHECK_RETRIES

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> CodeResult:
        """执行代码类步骤。

        Args:
            step: 代码类步骤（action 必须在 CODE_ACTIONS 中）。
            executor_fn: 执行函数（接收 Step，返回结果）；
                None 则只校验不执行（返回 pending 状态）。
            context: 任务上下文（可选，如记忆管家检索的历史代码）。

        Returns:
            CodeResult：代码 + 变更说明 + 运行方式 + 自检声明。
        """
        # 1. 校验是否代码类。
        if step.action not in CODE_ACTIONS:
            return CodeResult(
                step_id=step.id,
                status="rejected",
                error=f"任务超出代码范围: {step.action}",
            )

        # 2. 检查是否有新依赖（不擅自装包）。
        deps = step.inputs.get("dependencies", [])
        if deps:
            return CodeResult(
                step_id=step.id,
                status="pending_approval",
                pending_dependencies=list(deps),
            )

        # 3. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return CodeResult(
                step_id=step.id,
                status="pending",
            )

        # 4. 执行 + 自测（失败自查修复，2 次仍失败如实上报）。
        last_error: str = ""
        for attempt in range(self.max_self_check_retries + 1):
            try:
                executor_fn(step)
                # 自测通过。
                files_changed = self._extract_files_changed(step)
                return CodeResult(
                    step_id=step.id,
                    status="done",
                    files_changed=files_changed,
                    change_desc=step.inputs.get("change_desc", ""),
                    run_instructions=step.inputs.get("run_instructions", ""),
                    self_check_passed=True,
                )
            except PermissionError as exc:
                # 权限不足 → 转发审批门（不重试）。
                return CodeResult(
                    step_id=step.id,
                    status="pending_approval",
                    error=f"权限不足: {exc}",
                )
            except Exception as exc:  # noqa: BLE001  执行者需捕获所有执行异常
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_self_check_retries:
                    continue
        # 自测 2 次仍失败 → 如实上报（不交半成品）。
        return CodeResult(
            step_id=step.id,
            status="failed",
            error=last_error,
        )

    def _extract_files_changed(self, step: Step) -> list[str]:
        """从步骤 inputs 提取变更的文件列表。"""
        files = step.inputs.get("files")
        if files:
            return list(files)
        path = step.inputs.get("path")
        if path:
            return [str(path)]
        return []


__all__ = [
    "CODE_ACTIONS",
    "MAX_SELF_CHECK_RETRIES",
    "CodeResult",
    "CodeWorker",
    "ExecutorFn",
]
