"""六类核心 Schema（docs/contracts/01-schemas.md 的实现）。

Step / Plan / ToolCall / TaskState / ChangeProposal / RolePerm。
编码基准：全部角色间通信与工具参数校验均以本模块模型为准。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from agent_builder.contracts.state_machine import MAX_RETRY, TaskStatus

# ── 1. Step（步骤）─────────────────────────────────────────────


class Step(BaseModel):
    """单个执行步骤。depends_on 引用完整性由 Plan.validate_steps 校验。"""

    id: str = Field(description="步骤唯一 ID，如 step-001")
    action: str = Field(description="动作类型，如 web_search / file_write / code_gen")
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    status: str = "pending"  # pending | running | done | failed | skipped
    error_code: int | None = None
    assignee: str | None = None  # 由路由者填写

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        allowed = {"pending", "running", "done", "failed", "skipped"}
        if v not in allowed:
            raise ValueError(f"非法步骤状态 {v!r}，允许: {sorted(allowed)}")
        return v


# ── 2. Plan（执行计划）──────────────────────────────────────────


class Plan(BaseModel):
    """执行计划。confirmed_by_user=false 时路由者不得启动执行。"""

    task_id: str
    order: list[str] = Field(default_factory=list)
    parallel_groups: list[list[str]] = Field(default_factory=list)
    fallback: dict[str, dict[str, Any]] = Field(default_factory=dict)
    confirmed_by_user: bool = False

    def validate_steps(self, steps: dict[str, Step]) -> None:
        """跨对象校验：引用完整性 + DAG 无环（契约约束 1）。"""
        missing = [s for s in self.order if s not in steps]
        if missing:
            raise ValueError(f"order 引用了不存在的步骤: {missing}")
        for group in self.parallel_groups:
            for s in group:
                if s not in steps:
                    raise ValueError(f"parallel_groups 引用了不存在的步骤: {s}")
        for step_id, step in steps.items():
            for dep in step.depends_on:
                if dep not in steps:
                    raise ValueError(f"步骤 {step_id} 的 depends_on 引用了不存在的步骤: {dep}")
        self._detect_cycle(steps)

    @staticmethod
    def _detect_cycle(steps: dict[str, Step]) -> None:
        """基于 depends_on 做 DFS 环检测。"""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {sid: WHITE for sid in steps}

        def dfs(sid: str, path: list[str]) -> None:
            color[sid] = GRAY
            path.append(sid)
            for dep in steps[sid].depends_on:
                if color[dep] == GRAY:
                    cycle = path[path.index(dep):] + [dep]
                    raise ValueError(f"步骤 DAG 存在环依赖: {' -> '.join(cycle)}")
                if color[dep] == WHITE:
                    dfs(dep, path)
            path.pop()
            color[sid] = BLACK

        for sid in steps:
            if color[sid] == WHITE:
                dfs(sid, [])


# ── 3. ToolCall（工具调用 · 门卫审计记录）───────────────────────


class Approval(BaseModel):
    """审批记录。required=true 且未 granted_by 时门卫必须拒绝。"""

    required: bool = False
    granted_by: str | None = None
    ts: str | None = None


class ToolCall(BaseModel):
    """所有工具调用必须落此记录（只追加、不可篡改）。"""

    audit_id: str
    role: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str = "approved"  # approved | executed | denied
    result: Any | None = None
    approval: Approval = Field(default_factory=Approval)
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def is_approved(self) -> bool:
        """契约约束：approval.required=true 且未 granted_by → 门卫必须拒绝。"""
        return not (self.approval.required and not self.approval.granted_by)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        allowed = {"approved", "executed", "denied"}
        if v not in allowed:
            raise ValueError(f"非法 ToolCall 状态 {v!r}，允许: {sorted(allowed)}")
        return v


# ── 4. TaskState（任务状态快照）─────────────────────────────────


class Interruption(BaseModel):
    """中断点：进入 interrupted 时必须记录。"""

    resume_point: str = ""
    reason: str = "user_stop"


class TaskState(BaseModel):
    """任务状态快照。Conductor 维护的唯一状态权威来源。"""

    task_id: str
    status: TaskStatus = TaskStatus.RECEIVED
    current_stage: str = "received"
    plan: Plan | None = None
    retry_count: int = 0
    interrupted: Interruption | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode="after")
    def _enforce_retry_limit(self) -> TaskState:
        # 契约：retry_count 上限 2，超限由验证组/Conductor 转 failed。
        if self.retry_count > MAX_RETRY:
            raise ValueError(f"retry_count 超过上限 {MAX_RETRY}")
        return self


# ── 5. ChangeProposal（变更提案 · 副架构）───────────────────────


class ChangeProposal(BaseModel):
    """副架构变更提案。status=applied 前，看门人不得触碰任何代码。"""

    proposal_id: str
    target_module: str
    change_desc: str
    diff_preview: str | None = None
    impacted_files: list[str] = Field(default_factory=list)
    risk: str = "low"  # low | mid | high
    verification_plan: str | None = None
    cost_estimate: str | None = None
    status: str = "proposed"  # proposed | approved | rejected | applied | rolled_back
    approved_by: str | None = None
    version: str | None = None  # rolled_back 必须保留上一版本号
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("risk")
    @classmethod
    def _check_risk(cls, v: str) -> str:
        allowed = {"low", "mid", "high"}
        if v not in allowed:
            raise ValueError(f"非法风险等级 {v!r}，允许: {sorted(allowed)}")
        return v

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        allowed = {"proposed", "approved", "rejected", "applied", "rolled_back"}
        if v not in allowed:
            raise ValueError(f"非法提案状态 {v!r}，允许: {sorted(allowed)}")
        return v


# ── 6. RolePerm（角色 → 工具授权）───────────────────────────────


class RolePerm(BaseModel):
    """角色最小权限矩阵。门卫按此表放行；未列出的一律拒绝。"""

    role: str
    allowed_tools: list[str] = Field(default_factory=list)
    high_risk_tools: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _high_risk_within_allowed(self) -> RolePerm:
        # high_risk_tools 必须是 allowed_tools 的子集。
        extra = [t for t in self.high_risk_tools if t not in self.allowed_tools]
        if extra:
            raise ValueError(f"high_risk_tools 超出 allowed_tools: {extra}")
        return self

    def is_allowed(self, tool: str) -> bool:
        """门卫按此放行：未列出的工具一律拒绝。"""
        return tool in self.allowed_tools

    def is_high_risk(self, tool: str) -> bool:
        return tool in self.high_risk_tools


__all__ = [
    "Approval",
    "ChangeProposal",
    "Interruption",
    "Plan",
    "RolePerm",
    "Step",
    "TaskState",
    "ToolCall",
]
