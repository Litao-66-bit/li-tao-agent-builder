"""记录员（Historian）—— 副架构・记录层。

职责：记录提案→审批→应用→验证全流程 + 维护版本历史 + 生成变更说明。

边界声明：
- 日志必须完整不可篡改（audit_log 只追加）
- 审批被拒也记录（approved=False）
- 记录冲突 → 以时间戳为准并标注

执行协议：
1. 记录提案→审批→应用→验证全流程
2. 维护版本历史（版本号、变更 diff、审批人、时间）
3. 生成给编程者的变更说明（含 diff 摘要）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 记录类 action 集合。
RECORD_ACTIONS: frozenset[str] = frozenset({
    "record",
    "log_change",
})

# 流程阶段。
PIPELINE_STAGES = frozenset({
    "propose",
    "approve",
    "apply",
    "verify",
})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class VersionRecord:
    """版本记录项。"""

    version: str  # 版本号
    commit_sha: str  # 提交 SHA
    change_desc: str  # 变更描述
    diff_summary: str  # diff 摘要
    approver: str  # 审批人
    timestamp: str  # 时间
    approved: bool = False  # 是否批准（False 也记录）
    stage: str = "propose"  # 流程阶段


@dataclass(slots=True)
class ChangeLog:
    """变更日志。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    records: list[VersionRecord] = field(default_factory=list)
    change_notice: str = ""  # 给编程者的变更说明
    conflict_marked: bool = False  # 记录冲突 → 以时间戳为准并标注
    error: str | None = None


@dataclass(slots=True)
class Historian:
    """记录员角色：记录全流程 + 维护版本历史。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
    """

    correlation_id: str = "c-unknown"

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> ChangeLog:
        """执行记录步骤。

        Args:
            step: 记录类步骤。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            ChangeLog：变更日志 + 版本记录。
        """
        # 1. 校验是否记录类。
        if step.action not in RECORD_ACTIONS:
            return ChangeLog(
                step_id=step.id,
                status="rejected",
                error=f"任务超出记录范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return ChangeLog(step_id=step.id, status="pending")

        # 3. 执行记录。
        try:
            executor_fn(step)
            # 从 inputs 提取版本记录。
            raw_records = step.inputs.get("records", [])
            records = self._parse_records(raw_records)

            # 4. 检测记录冲突。
            conflict_marked = bool(step.inputs.get("conflict_marked", False))

            # 5. 生成变更说明（含 diff 摘要）。
            change_notice = self._build_change_notice(records)

            return ChangeLog(
                step_id=step.id,
                status="done",
                records=records,
                change_notice=change_notice,
                conflict_marked=conflict_marked,
            )
        except PermissionError as exc:
            return ChangeLog(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  记录员需捕获所有执行异常
            return ChangeLog(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _parse_records(self, raw_records: list[dict[str, Any]]) -> list[VersionRecord]:
        """解析版本记录清单。"""
        records: list[VersionRecord] = []
        for raw in raw_records:
            if not isinstance(raw, dict) or "version" not in raw:
                continue
            stage = str(raw.get("stage", "propose"))
            if stage not in PIPELINE_STAGES:
                stage = "propose"
            records.append(
                VersionRecord(
                    version=str(raw["version"]),
                    commit_sha=str(raw.get("commit_sha", "")),
                    change_desc=str(raw.get("change_desc", "")),
                    diff_summary=str(raw.get("diff_summary", "")),
                    approver=str(raw.get("approver", "")),
                    timestamp=str(raw.get("timestamp", "")),
                    approved=bool(raw.get("approved", False)),
                    stage=stage,
                )
            )
        # 记录冲突 → 以时间戳为准排序。
        if records:
            records.sort(key=lambda r: r.timestamp)
        return records

    def _build_change_notice(self, records: list[VersionRecord]) -> str:
        """生成给编程者的变更说明（含 diff 摘要）。"""
        if not records:
            return ""
        lines: list[str] = []
        for r in records:
            status = "批准" if r.approved else "拒绝"
            lines.append(
                f"[{r.version}] {r.stage} - {status} by {r.approver} @ {r.timestamp}\n"
                f"  变更: {r.change_desc}\n"
                f"  diff: {r.diff_summary}"
            )
        return "\n".join(lines)


__all__ = [
    "PIPELINE_STAGES",
    "RECORD_ACTIONS",
    "ChangeLog",
    "ExecutorFn",
    "Historian",
    "VersionRecord",
]
