"""HTTP 路由 —— 薄包装 Conductor 状态机 + Decomposer。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from agent_builder.api.deps import build_llm_client_or_none, get_store
from agent_builder.api.schemas import (
    DecomposeResponse,
    InterruptRequest,
    PlanRequest,
    TaskCreateRequest,
    TaskResponse,
)
from agent_builder.api.store import InMemoryTaskStore
from agent_builder.contracts.errors import AgentError
from agent_builder.roles.decomposer import Decomposer

router = APIRouter()


def _to_task_response(entry_or_conductor: Any, task_id: str) -> TaskResponse:
    """把 Conductor 的 TaskState 转 TaskResponse。"""
    # 兼容传 entry 或 conductor。
    conductor = getattr(entry_or_conductor, "conductor", entry_or_conductor)
    state = conductor.task_state
    interrupted = None
    if state.interrupted is not None:
        interrupted = state.interrupted.model_dump()
    plan = None
    if state.plan is not None:
        plan = state.plan.model_dump()
    return TaskResponse(
        task_id=task_id,
        status=state.status.value if hasattr(state.status, "value") else str(state.status),
        current_stage=state.current_stage,
        retry_count=state.retry_count,
        interrupted=interrupted,
        created_at=state.created_at,
        updated_at=state.updated_at,
        plan=plan,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/tasks", response_model=TaskResponse, status_code=201)
def create_task(
    req: TaskCreateRequest,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """创建任务 → 接收需求 → 状态转 PLANNING。"""
    task_id = req.task_id or str(uuid.uuid4())
    try:
        conductor = store.create(task_id, req.requirement)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    return _to_task_response(conductor, task_id)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """查询任务状态。"""
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return _to_task_response(entry, task_id)


@router.get("/tasks", response_model=list[str])
def list_tasks(
    store: InMemoryTaskStore = Depends(get_store),
) -> list[str]:
    """列出所有任务 ID。"""
    return store.list_all()


@router.post("/tasks/{task_id}/plan", response_model=DecomposeResponse)
def plan_task(
    task_id: str,
    req: PlanRequest,
    store: InMemoryTaskStore = Depends(get_store),
) -> DecomposeResponse:
    """触发分解器拆分需求 + 状态转 AWAITING_CONFIRM。

    use_llm=True 时调 LLM 拆分；False 或无密钥时返回待确认。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    conductor = entry.conductor
    llm_client = build_llm_client_or_none(req.use_llm)
    decomposer = Decomposer(correlation_id=task_id)
    try:
        result = decomposer.decompose(
            task_id=task_id,
            requirement=entry.requirement,
            llm_client=llm_client,
        )
        conductor.handle_plan_ready()  # PLANNING → AWAITING_CONFIRM
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    status = conductor.task_state.status
    return DecomposeResponse(
        task_id=task_id,
        status=status.value if hasattr(status, "value") else str(status),
        steps=result.to_dict()["steps"],
        order=result.order,
        parallel_groups=result.parallel_groups,
        pending_questions=result.pending_questions,
    )


@router.post("/tasks/{task_id}/approve", response_model=TaskResponse)
def approve_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户确认计划 → 状态转 EXECUTING。"""
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_plan_accepted()  # AWAITING_CONFIRM → EXECUTING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/reject", response_model=TaskResponse)
def reject_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户拒绝计划 → 状态回 PLANNING。"""
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_plan_rejected()  # AWAITING_CONFIRM → PLANNING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/interrupt", response_model=TaskResponse)
def interrupt_task(
    task_id: str,
    req: InterruptRequest,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户中断 → 状态转 INTERRUPTED。"""
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_interrupt(req.reason)  # EXECUTING → INTERRUPTED
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/resume", response_model=TaskResponse)
def resume_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户恢复 → 状态转 EXECUTING。"""
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_resume()  # INTERRUPTED → EXECUTING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/abort", response_model=TaskResponse)
def abort_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户放弃 → 状态转 FAILED。"""
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_abort()  # → FAILED
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    return _to_task_response(entry, task_id)


__all__ = ["router"]
