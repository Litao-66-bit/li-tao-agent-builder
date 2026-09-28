"""调度器（Scheduler）—— 主架构・规划层。

职责：把步骤 DAG 转成执行计划（顺序 + 并行组 + 失败预案），
考虑资源上限（MAX_PARALLEL）和并行收益 vs 协调成本。

边界声明：
- 只调度不执行：不可直接写文件/提交代码/执行命令
- 发现环依赖 → 返回分解器要求修正（不自行处理）
- 输出计划需用户确认后才执行

执行协议：
1. 接收 DecomposeResult（步骤 DAG）
2. 环依赖检测 → 有环则返回修正请求
3. 拓扑排序 + 分组（每层不超过 MAX_PARALLEL）
4. 并行收益判断（收益 < 协调成本 → 改顺序）
5. 失败预案（每步标 skip/retry/switch_tool）
6. 输出 Plan（展示给用户确认）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.errors import validation_error
from agent_builder.contracts.schemas import Plan
from agent_builder.roles.decomposer import DecomposeResult

# 资源上限：单批最多并行 4 个步骤。
MAX_PARALLEL = 4

# 快速操作：组内只有 2 个快速操作时，并行收益 < 协调成本 → 改顺序。
QUICK_ACTIONS: frozenset[str] = frozenset({
    "file_read",
    "file_list",
    "code_search",
    "config_read",
    "data_query",
    "memory_read",
})

# 失败预案策略映射。
FALLBACK_POLICY: dict[str, dict[str, Any]] = {
    "web_search": {"action": "retry", "max_retries": 1},
    "web_fetch": {"action": "retry", "max_retries": 1},
    "file_read": {"action": "retry", "max_retries": 1},
    "file_list": {"action": "skip", "reason": "目录列表失败跳过"},
    "file_write": {"action": "skip", "reason": "写操作不重试（防数据损坏）"},
    "code_search": {"action": "retry", "max_retries": 1},
    "sandbox_run": {"action": "retry", "max_retries": 1},
    "test_run": {"action": "skip", "reason": "测试失败跳过，不阻塞"},
    "data_query": {"action": "retry", "max_retries": 1},
    "citation_check": {"action": "skip", "reason": "引用校验失败跳过"},
    "plan_validate": {"action": "skip", "reason": "计划校验失败跳过"},
    "memory_write": {"action": "skip", "reason": "记忆写入失败跳过"},
    "memory_forget": {"action": "skip", "reason": "记忆清理失败跳过"},
    "audit_log": {"action": "skip", "reason": "审计日志失败跳过"},
    "metric_collect": {"action": "skip", "reason": "指标采集失败跳过"},
    "diff_preview": {"action": "skip", "reason": "diff预览失败跳过"},
    "git_commit": {"action": "skip", "reason": "提交失败跳过（需人工介入）"},
    "rollback": {"action": "skip", "reason": "回滚失败跳过（需人工介入）"},
    "git_log": {"action": "retry", "max_retries": 1},
    "approval_request": {"action": "skip", "reason": "审批请求失败跳过"},
    "change_notify": {"action": "retry", "max_retries": 1},
    "config_read": {"action": "skip", "reason": "配置读取失败跳过"},
}

# 默认失败预案（未知 action）。
DEFAULT_FALLBACK: dict[str, Any] = {"action": "skip", "reason": "未知操作，默认跳过"}


@dataclass(slots=True)
class ScheduleResult:
    """调度结果：执行计划 + 待确认。"""

    plan: Plan
    pending_questions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Scheduler:
    """调度器角色：步骤 DAG → 执行计划。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        max_parallel: 资源上限（单批最大并行数）。
    """

    correlation_id: str = "c-unknown"
    max_parallel: int = MAX_PARALLEL

    def schedule(self, task_id: str, decompose_result: DecomposeResult) -> ScheduleResult:
        """把步骤 DAG 转成执行计划。

        Args:
            task_id: 任务唯一 ID。
            decompose_result: 分解器的输出（步骤 DAG）。

        Returns:
            ScheduleResult：执行计划 + 待确认清单。

        Raises:
            AgentError(E_VALIDATION): task_id 为空 / decompose_result 无步骤。
        """
        cid = self.correlation_id
        if not task_id or not task_id.strip():
            raise validation_error(
                "scheduler: task_id 不能为空",
                source="scheduler",
                correlation_id=cid,
            )
        if not decompose_result.steps:
            return ScheduleResult(
                plan=Plan(task_id=task_id, order=[], parallel_groups=[], fallback={}),
                pending_questions=["分解器未产出步骤（等 LLM 拆分）"],
            )

        steps = decompose_result.steps

        # 1. 环依赖检测。
        cycle = self._detect_cycle(steps)
        if cycle is not None:
            return ScheduleResult(
                plan=Plan(task_id=task_id, order=[], parallel_groups=[], fallback={}),
                pending_questions=[
                    f"发现环依赖: {' -> '.join(cycle)}，请分解器修正"
                ],
            )

        # 2. 拓扑排序 + 分组（带资源上限裁剪）。
        groups = self._schedule_groups(steps)

        # 3. 并行收益判断。
        groups = self._optimize_parallelism(groups, steps)

        # 4. 失败预案。
        fallback = self._build_fallback(steps)

        # 5. 顺序（拓扑排序扁平化）。
        order = [sid for group in groups for sid in group]

        plan = Plan(
            task_id=task_id,
            order=order,
            parallel_groups=groups,
            fallback=fallback,
            confirmed_by_user=False,
        )

        return ScheduleResult(plan=plan, pending_questions=[])

    # ── 内部方法 ──────────────────────────────────────────────

    def _detect_cycle(self, steps: dict) -> list[str] | None:
        """环依赖检测：DFS 三色法。

        Returns:
            环路径列表（如 ["s1", "s2", "s1"]），无环返回 None。
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {sid: WHITE for sid in steps}

        def dfs(sid: str, path: list[str]) -> list[str] | None:
            color[sid] = GRAY
            path.append(sid)
            for dep in steps[sid].depends_on:
                if dep not in steps:
                    continue  # 未定义依赖，由歧义检查处理
                if color[dep] == GRAY:
                    # 找到环。
                    cycle = path[path.index(dep):] + [dep]
                    return cycle
                if color[dep] == WHITE:
                    result = dfs(dep, path)
                    if result is not None:
                        return result
            path.pop()
            color[sid] = BLACK
            return None

        for sid in steps:
            if color[sid] == WHITE:
                result = dfs(sid, [])
                if result is not None:
                    return result
        return None

    def _schedule_groups(self, steps: dict) -> list[list[str]]:
        """拓扑排序分层：每层不超过 max_parallel。"""
        # 构建依赖图（只保留指向已知步骤的依赖）。
        step_ids = set(steps.keys())
        remaining: dict[str, set[str]] = {}
        for sid, step in steps.items():
            deps = {d for d in step.depends_on if d in step_ids}
            remaining[sid] = deps

        groups: list[list[str]] = []
        while remaining:
            # 找出所有依赖已满足的步骤。
            ready = sorted(sid for sid, deps in remaining.items() if not deps)
            if not ready:
                # 有环（不应到这里，_detect_cycle 已拦截）。
                ready = sorted(remaining.keys())

            # 资源上限裁剪：每层不超过 max_parallel。
            if len(ready) > self.max_parallel:
                ready = ready[: self.max_parallel]

            groups.append(ready)
            for sid in ready:
                del remaining[sid]
            # 从剩余步骤的依赖中移除已完成的。
            for deps in remaining.values():
                deps -= set(ready)

        return groups

    def _optimize_parallelism(self, groups: list[list[str]], steps: dict) -> list[list[str]]:
        """并行收益判断：组内 2 个快速操作 → 改顺序执行。"""
        optimized: list[list[str]] = []
        for group in groups:
            if len(group) == 2:
                # 检查是否都是快速操作。
                actions = {steps[sid].action for sid in group}
                if actions.issubset(QUICK_ACTIONS):
                    # 并行收益 < 协调成本 → 改顺序。
                    optimized.append([group[0]])
                    optimized.append([group[1]])
                    continue
            optimized.append(group)
        return optimized

    def _build_fallback(self, steps: dict) -> dict[str, dict[str, Any]]:
        """为每步生成失败预案。"""
        fallback: dict[str, dict[str, Any]] = {}
        for sid, step in steps.items():
            action = step.action
            if action in FALLBACK_POLICY:
                fallback[sid] = dict(FALLBACK_POLICY[action])
            else:
                fallback[sid] = dict(DEFAULT_FALLBACK)
        return fallback


__all__ = [
    "DEFAULT_FALLBACK",
    "FALLBACK_POLICY",
    "MAX_PARALLEL",
    "QUICK_ACTIONS",
    "ScheduleResult",
    "Scheduler",
]
