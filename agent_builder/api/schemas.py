"""API 请求/响应模型 —— Pydantic schema。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    """创建任务请求。"""

    requirement: str = Field(description="用户需求文本")
    task_id: str | None = Field(default=None, description="可选任务 ID，不传则自动生成")


class PlanRequest(BaseModel):
    """触发分解/规划请求。"""

    use_llm: bool = Field(default=False, description="是否调用 LLM 拆分需求")


class InterruptRequest(BaseModel):
    """中断请求。"""

    reason: str = Field(default="user_stop", description="中断原因")


class StepResult(BaseModel):
    """单个步骤的执行结果（用于回传给前端）。"""

    step_id: str
    action: str
    status: str  # done | failed | skipped | pending_approval | pending
    executor: str
    result: str | None = None
    error: str | None = None
    retries: int = 0


class TaskResponse(BaseModel):
    """任务状态响应。"""

    task_id: str
    status: str
    current_stage: str
    retry_count: int
    interrupted: dict[str, Any] | None = None
    created_at: str
    updated_at: str
    plan: dict[str, Any] | None = None
    execution_results: list[StepResult] = Field(default_factory=list)
    gatekeeper_audit: list[dict[str, Any]] = Field(default_factory=list)


class DecomposeResponse(BaseModel):
    """分解结果响应。"""

    task_id: str
    status: str
    steps: dict[str, Any] = Field(default_factory=dict)
    order: list[str] = Field(default_factory=list)
    parallel_groups: list[list[str]] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)


class FileNode(BaseModel):
    """文件树节点。"""

    name: str
    type: str  # "dir" | "file"
    path: str
    size: int | None = None  # 文件大小（字节），目录为 None
    children: list[FileNode] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """错误响应。"""

    error_code: int
    error_name: str
    message: str
    correlation_id: str


__all__ = [
    "DecomposeResponse",
    "ErrorResponse",
    "FileNode",
    "InterruptRequest",
    "PlanRequest",
    "StepResult",
    "TaskCreateRequest",
    "TaskResponse",
]
