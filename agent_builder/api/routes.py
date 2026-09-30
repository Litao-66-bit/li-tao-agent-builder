"""HTTP 路由 —— 薄包装 Conductor 状态机 + Decomposer + 工作区文件树。"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_builder.api.deps import build_llm_client_or_none, get_store
from agent_builder.api.orchestrator import run_plan
from agent_builder.api.schemas import (
    ApiKeyRequest,
    ApiKeyStatusResponse,
    DecomposeResponse,
    FileContentResponse,
    FileNode,
    FileWriteRequest,
    InterruptRequest,
    PlanRequest,
    TaskCreateRequest,
    TaskResponse,
)
from agent_builder.api.secrets import InvalidApiKeyError, get_api_key_store
from agent_builder.api.store import InMemoryTaskStore
from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Plan
from agent_builder.roles.decomposer import Decomposer
from agent_builder.tools.gatekeeper import WORKSPACE_DIR

router = APIRouter()

# 文件树最大递归深度（防止深层目录遍历开销过大）。
MAX_TREE_DEPTH = 8

# 可编辑上限：不超过此大小 → 返回全文，可编辑并保存。
MAX_EDITABLE_BYTES = 2 * 1024 * 1024

# 预览硬上限：超过此大小 → 直接拒绝（避免把超大文件塞进响应体 / 浏览器）。
MAX_PREVIEW_BYTES = 20 * 1024 * 1024


def _resolve_workspace_dir() -> Path:
    """解析工作区目录。

    优先使用 gatekeeper.WORKSPACE_DIR；若该路径在当前环境不存在
    （如 Windows 开发环境），则回退到项目根目录。
    """
    if WORKSPACE_DIR.exists():
        return WORKSPACE_DIR.resolve()
    # 回退：项目根目录（agent_builder/api/routes.py → 上溯 2 层 = li-tao-agent-builder/）。
    fallback = Path(__file__).resolve().parents[2]
    return fallback


def _build_file_tree(root: Path, current: Path, depth: int = 0) -> list[FileNode]:
    """递归构建文件树（限深 MAX_TREE_DEPTH）。"""
    nodes: list[FileNode] = []
    if depth >= MAX_TREE_DEPTH:
        return nodes
    try:
        entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except (PermissionError, OSError):
        return nodes
    for entry in entries:
        # 跳过隐藏目录（.git、.venv 等）和 __pycache__。
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        rel_path = str(entry.relative_to(root)).replace("\\", "/")
        if entry.is_dir():
            children = _build_file_tree(root, entry, depth + 1)
            nodes.append(FileNode(name=entry.name, type="dir", path=rel_path, children=children))
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = None
            nodes.append(FileNode(name=entry.name, type="file", path=rel_path, size=size))
    return nodes


def _safe_workspace_path(rel_path: str) -> Path:
    """把工作区相对路径解析为限定在工作区根目录内的绝对路径。

    安全边界（防路径穿越）：
    - 只接受相对路径，拒绝绝对路径与 ``..``；
    - 拒绝隐藏目录（``.git``/``.venv`` 等）与 ``__pycache__``；
    - 解析后的真实路径必须仍位于工作区根目录内。
    """
    raw = (rel_path or "").strip().replace("\\", "/")
    if not raw:
        raise HTTPException(status_code=400, detail="path 不能为空")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise HTTPException(status_code=400, detail="只接受工作区相对路径")
    for part in pure.parts:
        # 拒绝 .. / 空段 / 隐藏目录 / 缓存目录；
        # 额外拒绝含 ":" 的段（Windows 盘符与 NTFS 备用数据流 ADS）。
        if part in ("..", "") or part.startswith(".") or part == "__pycache__" or ":" in part:
            raise HTTPException(
                status_code=400,
                detail="非法路径：不允许访问上级目录、隐藏目录、缓存目录或含冒号的路径段",
            )
    root = _resolve_workspace_dir()
    target = (root / Path(*pure.parts)).resolve()
    if target != root and not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="路径超出工作区范围")
    return target


def _to_task_response(entry_or_conductor: Any, task_id: str) -> TaskResponse:
    """把 Conductor 的 TaskState 转 TaskResponse。

    支持传 TaskEntry（含 steps/execution_results/gatekeeper_audit）
    或裸 Conductor（仅状态字段）。
    """
    # 兼容传 entry 或 conductor。
    conductor = getattr(entry_or_conductor, "conductor", entry_or_conductor)
    state = conductor.task_state
    interrupted = None
    if state.interrupted is not None:
        interrupted = state.interrupted.model_dump()
    plan = None
    if state.plan is not None:
        plan = state.plan.model_dump()
    # execution_results / gatekeeper_audit 仅 entry 上有（approve 后才填充）。
    execution_results = getattr(entry_or_conductor, "execution_results", []) or []
    gatekeeper_audit = getattr(entry_or_conductor, "gatekeeper_audit", []) or []
    return TaskResponse(
        task_id=task_id,
        status=state.status.value if hasattr(state.status, "value") else str(state.status),
        current_stage=state.current_stage,
        retry_count=state.retry_count,
        interrupted=interrupted,
        created_at=state.created_at,
        updated_at=state.updated_at,
        plan=plan,
        execution_results=execution_results,
        gatekeeper_audit=gatekeeper_audit,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ── API 密钥（安全优先：只进内存、不回显、不落盘）──────────────


@router.get("/settings/api-key", response_model=ApiKeyStatusResponse)
def get_api_key_status() -> ApiKeyStatusResponse:
    """查询密钥配置状态。

    只返回「是否已配置 + 掩码」，任何情况下都不返回密钥明文。
    """
    store = get_api_key_store()
    return ApiKeyStatusResponse(configured=store.is_configured, masked=store.masked_hint())


@router.post("/settings/api-key", response_model=ApiKeyStatusResponse)
def set_api_key(req: ApiKeyRequest) -> ApiKeyStatusResponse:
    """保存 API 密钥到进程内存。

    仅做格式校验（非空 / 长度 / 无空白与控制字符），不落盘、不回显；
    校验失败时返回 400，错误信息只含原因，不含密钥原文。
    """
    store = get_api_key_store()
    try:
        store.set_key(req.api_key)
    except InvalidApiKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiKeyStatusResponse(configured=store.is_configured, masked=store.masked_hint())


@router.delete("/settings/api-key", response_model=ApiKeyStatusResponse)
def delete_api_key() -> ApiKeyStatusResponse:
    """删除已保存的 API 密钥（幂等）。"""
    store = get_api_key_store()
    store.clear()
    return ApiKeyStatusResponse(configured=False, masked=None)


@router.get("/workspace/files", response_model=list[FileNode])
def list_workspace_files() -> list[FileNode]:
    """列出工作区文件树（跳过隐藏目录和 __pycache__，限深 MAX_TREE_DEPTH）。"""
    ws_dir = _resolve_workspace_dir()
    return _build_file_tree(ws_dir, ws_dir)


@router.get("/workspace/file", response_model=FileContentResponse)
def read_workspace_file(path: str = Query(..., description="工作区相对路径")) -> FileContentResponse:
    """读取工作区内单个文件内容。

    分级策略：
    - ≤ MAX_EDITABLE_BYTES：返回全文，前端可编辑并保存；
    - ≤ MAX_PREVIEW_BYTES：只读截断预览（truncated=True，前端禁止编辑）；
    - > MAX_PREVIEW_BYTES：413 拒绝。

    安全约束：路径必须落在工作区内（见 _safe_workspace_path，防路径穿越）；
    错误信息只回显用户提交的相对路径，不泄露服务端绝对路径。
    """
    root = _resolve_workspace_dir()
    target = _safe_workspace_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"路径不是文件: {path}")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=400, detail="无法读取文件信息") from exc
    if size > MAX_PREVIEW_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（{size} 字节），超过预览上限 {MAX_PREVIEW_BYTES} 字节",
        )

    truncated = size > MAX_EDITABLE_BYTES
    try:
        with target.open("rb") as fh:
            data = fh.read(MAX_EDITABLE_BYTES)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"读取失败: {path}") from exc

    # 二进制启发式：前 8KB 出现 NUL 字节即判定为非文本。
    if b"\x00" in data[:8192]:
        raise HTTPException(
            status_code=415,
            detail="文件不是文本（疑似二进制），暂不支持预览",
        )
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if not truncated:
            raise HTTPException(
                status_code=415,
                detail="文件不是 UTF-8 文本，暂不支持预览",
            ) from exc
        # 截断处可能切断多字节字符，替换为占位符保证可展示。
        content = data.decode("utf-8", errors="replace")

    rel = target.relative_to(root).as_posix()
    return FileContentResponse(path=rel, size=size, content=content, truncated=truncated)


@router.put("/workspace/file", response_model=FileContentResponse)
def write_workspace_file(
    req: FileWriteRequest,
    path: str = Query(..., description="工作区相对路径"),
) -> FileContentResponse:
    """把编辑后的内容写回工作区内**已存在**的文件。

    安全约束：
    - 路径必须落在工作区内（见 _safe_workspace_path，防路径穿越）；
    - 只允许覆盖已存在的普通文件，不允许借此新建文件；
    - 内容限 UTF-8 文本且不超过 MAX_FILE_PREVIEW_BYTES；
    - 采用「同目录临时文件 + os.replace」原子写，写入失败不破坏原文件。
    """
    root = _resolve_workspace_dir()
    target = _safe_workspace_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"路径不是文件: {path}")

    data = req.content.encode("utf-8")
    if len(data) > MAX_EDITABLE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"内容过大（{len(data)} 字节），超过写入上限 {MAX_EDITABLE_BYTES} 字节",
        )

    # 原子写：先写同目录临时文件，再整体替换，避免中途失败写坏原文件。
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, target)
    except OSError as exc:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass  # 清理失败不影响主流程
        raise HTTPException(status_code=400, detail=f"写入失败: {path}") from exc

    rel = target.relative_to(root).as_posix()
    return FileContentResponse(path=rel, size=len(data), content=req.content)


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
        # 保存 steps 到 entry，供 approve 阶段 run_plan 使用。
        entry.steps = result.steps
        # 构造 Plan 并校验引用完整性 + DAG 无环；置位 plan 后转 AWAITING_CONFIRM。
        plan = Plan(
            task_id=task_id,
            order=result.order,
            parallel_groups=result.parallel_groups,
            confirmed_by_user=False,
        )
        plan.validate_steps(result.steps)
        conductor.task_state.plan = plan
        conductor.handle_plan_ready()  # PLANNING → AWAITING_CONFIRM
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    except ValueError as exc:
        # Plan.validate_steps 抛的引用完整性/环错误。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    """用户确认计划 → 状态转 EXECUTING → 执行编排器派发各步骤。

    步骤全部 done 时自动转 VERIFYING；出现 failed/pending_approval 则保持
    EXECUTING，由前端展示执行结果卡片供用户决策（重试/中断）。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    conductor = entry.conductor
    try:
        conductor.handle_plan_accepted()  # AWAITING_CONFIRM → EXECUTING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc

    # 已有 plan + steps 时触发执行编排器；空计划保持 EXECUTING 让用户决策。
    plan = conductor.task_state.plan
    if plan is not None and entry.steps:
        try:
            execution_results, gatekeeper_audit = run_plan(
                task_id=task_id,
                plan=plan,
                steps=entry.steps,
            )
        except AgentError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        entry.execution_results = execution_results
        entry.gatekeeper_audit = gatekeeper_audit

        # 全部 done → 转 VERIFYING；否则保持 EXECUTING 让用户决策。
        all_done = bool(execution_results) and all(
            r.get("status") == "done" for r in execution_results
        )
        if all_done:
            try:
                conductor.handle_all_steps_done()  # EXECUTING → VERIFYING
            except AgentError as exc:
                raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    # 无 steps（空计划 / LLM 未拆分）→ 保持 EXECUTING，等待用户中断或重新规划。

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
