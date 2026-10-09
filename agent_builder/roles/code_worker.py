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

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Step

# 自测失败最大自查次数。
MAX_SELF_CHECK_RETRIES = 2

# 代码类 action 集合。
CODE_ACTIONS: frozenset[str] = frozenset({
    "code_search",
    "file_write",
    "file_edit",
    "file_read",
    "file_list",
})

# 真正会写盘的动作 —— 只有这些才谈得上「变更的文件」。
#
# 只读动作（`file_list` / `file_read` / `code_search`）的 `inputs.path` 是「被访问的
# 对象」，把它报成 `files_changed` 会让编排层的 `artifacts_of()` 当成产物：前端于是给
# 「列出文件」也渲染「查看产物」，点开必然报「路径不是文件: <目录>」。
# 单测只覆盖了「纯工具」路径（result 是 str），角色派发路径（result 是本结果类）此前漏了。
FILE_PRODUCING_ACTIONS: frozenset[str] = frozenset({"file_write", "file_edit"})

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
    retryable: bool = True
    pending_dependencies: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CodeWorker:
    """代码执行者角色：写/改代码 + 自测 + 变更说明。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        max_self_check_retries: 自测失败最大自查次数。
        llm_client: LLM 客户端（可选；由运行时密钥工厂注入）。
            可用时用于生成变更说明，不可用时只取步骤显式声明的说明。
    """

    correlation_id: str = "c-unknown"
    max_self_check_retries: int = MAX_SELF_CHECK_RETRIES
    llm_client: Any = None

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
                    change_desc=step.inputs.get("change_desc") or self._llm_change_desc(step),
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
            except AgentError as exc:
                # 契约异常：非可重试（如 file_write 未授权覆盖）→ 不自查重试，直接上报。
                # ``E_VALIDATION`` 同样不重试：参数类失败是**确定性**的，重试只会白跑 ——
                # 实测 file_read 传错路径被重试 3 次，gatekeeper 因此留下 3 条一模一样的审计行。
                labeled = f"{exc.error_name}: {exc.info.message}"
                if not exc.retryable or exc.error_name == "E_VALIDATION":
                    return CodeResult(
                        step_id=step.id,
                        status="failed",
                        error=labeled,
                        retryable=False,
                    )
                last_error = labeled
                if attempt < self.max_self_check_retries:
                    continue
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

    def _llm_ready(self) -> bool:
        """LLM 客户端是否可用（鸭子类型判定，不依赖具体类型）。"""
        return self.llm_client is not None and bool(getattr(self.llm_client, "is_available", False))

    def _llm_change_desc(self, step: Step) -> str:
        """由 LLM 依据步骤上下文生成变更说明；不可用/失败返回空串。"""
        if not self._llm_ready():
            return ""
        prompt = (
            "用一句话说明下面这次代码变更做了什么（不夸大、不编造）：\n"
            f"动作：{step.action}\n"
            f"输入：{json.dumps(step.inputs, ensure_ascii=False)}"
        )
        try:
            return self.llm_client.chat([{"role": "user", "content": prompt}]) or ""
        except Exception:  # noqa: BLE001  LLM 调用失败降级为空说明
            return ""

    def _extract_files_changed(self, step: Step) -> list[str]:
        """从步骤 inputs 提取**真正变更**的文件列表。

        只读动作（列出文件 / 读取文件 / 搜索代码）一律返回空 —— 它们没有改动任何文件，
        把 `inputs.path` 报成变更会让「列出 tests」长出「查看产物」按钮。
        """
        if step.action not in FILE_PRODUCING_ACTIONS:
            return []
        files = step.inputs.get("files")
        if files:
            return list(files)
        path = step.inputs.get("path")
        if path:
            return [str(path)]
        return []


__all__ = [
    "CODE_ACTIONS",
    "FILE_PRODUCING_ACTIONS",
    "MAX_SELF_CHECK_RETRIES",
    "CodeResult",
    "CodeWorker",
    "ExecutorFn",
]
