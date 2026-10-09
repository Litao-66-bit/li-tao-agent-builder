"""HTTP 路由 —— 薄包装 Conductor 状态机 + Decomposer + 工作区文件树。"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_builder.api.agent_loop import (
    STEP_ID_PREFIX,
    STOP_FINAL,
    STOP_INTERRUPTED,
    STOP_PENDING_APPROVAL,
    STOP_PROPOSED,
    LoopBudget,
    LoopOutcome,
    StepOutcome,
    run_agent_loop,
    run_step,
    to_execution_results,
    to_step,
)
from agent_builder.api.chat import classify_intent, missing_key_outcome
from agent_builder.api.council import run_council
from agent_builder.api.deciders import (
    Decision,
    LLMRouteDecider,
    LoopContext,
    PrefixedDecider,
    ProposalDraft,
)
from agent_builder.api.deps import (
    build_llm_client_or_none,
    get_llm_client,
    get_project_store,
    get_store,
)
from agent_builder.api.handover import (
    ACK_SEEN,
    build_handover_brief,
    collect_open_items,
    should_suggest_handover,
)
from agent_builder.api.orchestrator import (
    build_step_executor,
    check_execution_consistency,
    run_plan,
    take_tool_observation,
)
from agent_builder.api.projects import (
    create_workspace_folder,
    pick_directory,
    validate_workspace_root,
)
from agent_builder.api.proposal_scaffold import (
    SCAFFOLD_TEMPLATE_VERSION,
    plan_scaffold,
    render_role_module,
    write_scaffold,
)
from agent_builder.api.role_briefs import current_mode
from agent_builder.api.role_catalog import build_catalog, is_high_risk
from agent_builder.api.schemas import (
    ApiKeyRequest,
    ApiKeyStatusResponse,
    ChatRequest,
    ChatResponse,
    CouncilRequest,
    DecomposeResponse,
    DirectoryPickResponse,
    FileContentResponse,
    FileNode,
    FileWriteRequest,
    HandoverAckRequest,
    HandoverResponse,
    InterruptRequest,
    LoopBudgetState,
    LoopDecision,
    LoopRoundResult,
    LoopState,
    PlanRequest,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectMkdirRequest,
    ProjectResponse,
    ProposalView,
    TaskApproveRequest,
    TaskCreateRequest,
    TaskResponse,
    TaskResumeRequest,
    TaskSummary,
)
from agent_builder.api.secrets import (
    ApiKeyStore,
    InvalidApiKeyError,
    InvalidTtlError,
    NoRotatableKeyError,
    get_api_key_store,
    validate_api_key,
    validate_ttl,
)
from agent_builder.api.security import require_local_client
from agent_builder.api.store import InMemoryTaskStore
from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import ChangeProposal, Plan, Step
from agent_builder.contracts.state_machine import TaskStatus
from agent_builder.llm.client import UsageAccumulator, merge_usage, probe_api_key
from agent_builder.llm.config import LLMConfig
from agent_builder.narrate import action_title, conclude, summarize_result
from agent_builder.roles.decomposer import Decomposer
from agent_builder.tools.permissions import get_default_role_perms

router = APIRouter()

# 文件树最大递归深度（防止深层目录遍历开销过大）。
MAX_TREE_DEPTH = 8

# 可编辑上限：不超过此大小 → 返回全文，可编辑并保存。
MAX_EDITABLE_BYTES = 2 * 1024 * 1024


def _resolve_workspace_dir() -> Path:
    """解析当前工作区目录。

    工作区由「当前项目」决定（见 agent_builder/api/projects.py）：
    - 未切换过项目 → 默认工作区项目（gatekeeper.WORKSPACE_DIR，不存在则项目根）；
    - 已切换项目且目录仍有效 → 该项目目录；
    - 项目目录已失效 → 回退默认工作区，保证接口始终可用。
    """
    return get_project_store().current_workspace_dir()


def _high_risk_actions(steps: dict[str, Any]) -> list[str]:
    """本计划中属高风险（需审批）的 action 去重列表（跨角色并集）。

    高风险真源是 ``permissions.py`` 的 ``high_risk_tools``。此处取并集，供前端提示
    「哪些步骤需要逐工具授权」；运行时判定仍按步骤所属角色精确执行。
    """
    gated: set[str] = set()
    for perm in get_default_role_perms().values():
        gated.update(perm.high_risk_tools)
    actions = {str(getattr(step, "action", "")) for step in steps.values()}
    return sorted(actions & gated)


def _record_usage(entry: Any, accumulator: UsageAccumulator, *, mode: str | None = None) -> None:
    """把本次请求的 token 用量合并进任务级计量（附生效档位，便于 A/B 归因）。

    计量落位在 ``TaskEntry.usage``（供 ``metric_collect`` / ``auditor`` 消费）；
    写入失败不影响主流程。

    Args:
        entry: 任务条目。
        accumulator: 本次请求的用量采集器。
        mode: 生效的角色简报档位（请求覆盖值）；None 用环境变量当前档位。
    """
    if accumulator.total.calls == 0:
        return
    payload = accumulator.to_dict()
    payload["mode"] = mode or current_mode()
    entry.usage = merge_usage(entry.usage, payload)


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
    # execution_results / gatekeeper_audit / self_check 仅 entry 上有（approve 后才填充）。
    execution_results = getattr(entry_or_conductor, "execution_results", []) or []
    gatekeeper_audit = getattr(entry_or_conductor, "gatekeeper_audit", []) or []
    self_check = getattr(entry_or_conductor, "self_check", None)
    council = getattr(entry_or_conductor, "council", None)
    steps = getattr(entry_or_conductor, "steps", {}) or {}
    # steps 明细（step_id → Step）：plan 只含 order / parallel_groups，缺了这段
    # 刷新 / 换标签页回看时就只剩 step_id 占位（看不到 action / 输入 / 人读标题）。
    step_payload: dict[str, Any] = {}
    for step_id, step in steps.items():
        dump = getattr(step, "model_dump", None)
        step_payload[str(step_id)] = dump() if callable(dump) else step
    # 「结论区」：agentic 取模型 kind=final 的结论；固定工作流按执行结果如实汇总。
    # 同时下发状态 / 是否非收敛停下 —— 兜底措辞必须与顶栏一致：任务没收敛时不能说
    # 「执行完成」（否则顶栏「已暂停 / 已停下」和结论自相矛盾）。
    loop_state = getattr(entry_or_conductor, "loop_state", None)
    status_value = state.status.value if hasattr(state.status, "value") else str(state.status)
    stopped_reason = getattr(loop_state, "stopped_reason", "") if loop_state is not None else ""
    conclusion = conclude(
        final_answer=getattr(loop_state, "final_answer", "") if loop_state is not None else "",
        execution_results=execution_results,
        status=status_value,
        stopped=bool(stopped_reason) and stopped_reason != STOP_FINAL,
    )
    return TaskResponse(
        task_id=task_id,
        status=status_value,
        current_stage=state.current_stage,
        retry_count=state.retry_count,
        interrupted=interrupted,
        created_at=state.created_at,
        updated_at=state.updated_at,
        plan=plan,
        steps=step_payload,
        execution_results=execution_results,
        gatekeeper_audit=gatekeeper_audit,
        high_risk_actions=_high_risk_actions(steps),
        self_check=self_check,
        council=council,
        title=getattr(entry_or_conductor, "title", "") or "",
        pending_approval=getattr(entry_or_conductor, "pending_approval", None),
        loop_state=getattr(entry_or_conductor, "loop_state", None),
        pending_proposal=getattr(entry_or_conductor, "pending_proposal", None),
        proposals=getattr(entry_or_conductor, "proposals", None) or [],
        conclusion=conclusion,
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ── API 密钥（安全优先：只进内存、不回显、不落盘）──────────────
# 端点均挂 require_local_client：必须携带本机客户端标识头，拒绝跨站来源。


def _iso_utc(ts: float | None) -> str | None:
    """把 Unix 时间戳转成 UTC ISO8601 字符串；None 透传。"""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _key_status_payload(
    store: ApiKeyStore, *, rotated_from: str | None = None
) -> ApiKeyStatusResponse:
    """统一的密钥状态响应构造（绝不包含密钥明文）。"""
    return ApiKeyStatusResponse(
        configured=store.is_configured,
        masked=store.masked_hint(),
        fingerprint=store.fingerprint(),
        expired=store.is_expired,
        expires_at=_iso_utc(store.expires_at),
        remaining_s=store.remaining_s,
        rotation_count=store.rotation_count,
        rotated_at=_iso_utc(store.rotated_at),
        rotated_from_fingerprint=rotated_from,
    )


@router.get(
    "/settings/api-key",
    response_model=ApiKeyStatusResponse,
    dependencies=[Depends(require_local_client)],
)
def get_api_key_status() -> ApiKeyStatusResponse:
    """查询密钥配置状态。

    只返回「是否已配置 + 掩码 + 非可逆指纹 + TTL/轮换元数据」，
    任何情况下都不返回密钥明文。
    """
    return _key_status_payload(get_api_key_store())


@router.post(
    "/settings/api-key",
    response_model=ApiKeyStatusResponse,
    dependencies=[Depends(require_local_client)],
)
def set_api_key(
    req: ApiKeyRequest,
    verify: bool = Query(False, description="是否对 {base_url}/models 做一次轻量探针"),
) -> ApiKeyStatusResponse:
    """保存 API 密钥到进程内存（可选设置 TTL）。

    先做格式与 TTL 校验（非空 / 长度 / 无空白与控制字符 / ttl > 0 且 ≤ 30 天）；
    ``verify=true`` 时再做一次轻量连通性探针。任一步失败都返回 400，
    且**不改变现有已存密钥**（不落库）。失败信息只含原因，不含密钥原文。
    """
    try:
        key = validate_api_key(req.api_key)
        ttl = validate_ttl(req.ttl_s)
    except (InvalidApiKeyError, InvalidTtlError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if verify:
        ok, reason = probe_api_key(key, base_url=LLMConfig.from_env().base_url)
        if not ok:
            raise HTTPException(status_code=400, detail=f"密钥连通性校验失败：{reason}")
    store = get_api_key_store()
    store.set_key(key, ttl_s=ttl)  # 复用同一校验；全部通过后才落库
    return _key_status_payload(store)


@router.post(
    "/settings/api-key/rotate",
    response_model=ApiKeyStatusResponse,
    dependencies=[Depends(require_local_client)],
)
def rotate_api_key(
    req: ApiKeyRequest,
    verify: bool = Query(False, description="是否对 {base_url}/models 做一次轻量探针"),
) -> ApiKeyStatusResponse:
    """轮换运行时 API 密钥：用新密钥替换当前密钥。

    与 ``POST /settings/api-key`` 的差别：
    - 要求当前存在**有效**密钥（未配置/已过期 → 400），语义是「换掉」而非「首次设置」；
    - 响应回带被替换掉的旧密钥指纹 ``rotated_from_fingerprint``，便于核对换的是哪把；
    - 响应同时回带累计轮换次数与轮换时间。

    格式 / TTL / 探针任一失败都返回 400，且**不改变现有密钥**（先全部校验，最后才替换）。
    """
    try:
        key = validate_api_key(req.api_key)
        ttl = validate_ttl(req.ttl_s)
    except (InvalidApiKeyError, InvalidTtlError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store = get_api_key_store()
    if not store.is_configured:
        raise HTTPException(
            status_code=400,
            detail="当前没有可轮换的密钥；请先用 POST /settings/api-key 设置",
        )
    if verify:
        ok, reason = probe_api_key(key, base_url=LLMConfig.from_env().base_url)
        if not ok:
            raise HTTPException(status_code=400, detail=f"密钥连通性校验失败：{reason}")
    try:
        previous = store.rotate(key, ttl_s=ttl)
    except NoRotatableKeyError as exc:  # pragma: no cover - 上面已判定，防御性兜底
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _key_status_payload(store, rotated_from=previous)


@router.delete(
    "/settings/api-key",
    response_model=ApiKeyStatusResponse,
    dependencies=[Depends(require_local_client)],
)
def delete_api_key() -> ApiKeyStatusResponse:
    """删除已保存的 API 密钥（幂等）。"""
    store = get_api_key_store()
    store.clear()
    return _key_status_payload(store)


# ── 项目（工作区）──
# 工作区由「项目」决定：选定目录 → 登记为项目 → 设为当前 → 后续 /workspace/* 均指向它。
# 目录选择由后端弹系统对话框（浏览器安全模型下前端拿不到绝对路径）；边界校验见 projects.py。


@router.get(
    "/projects",
    response_model=ProjectListResponse,
    dependencies=[Depends(require_local_client)],
)
def list_projects() -> ProjectListResponse:
    """返回项目列表（按最近打开倒序）+ 当前项目与工作区路径。"""
    store = get_project_store()
    current = store.current()
    return ProjectListResponse(
        current_project_id=current.project_id if current is not None else None,
        workspace_dir=str(store.current_workspace_dir()),
        projects=[ProjectResponse(**asdict(project)) for project in store.list_projects()],
    )


@router.post(
    "/projects/pick-directory",
    response_model=DirectoryPickResponse,
    dependencies=[Depends(require_local_client)],
)
def pick_workspace_directory() -> DirectoryPickResponse:
    """弹系统文件夹选择对话框（后端进程内），返回选中目录的绝对路径。

    用户取消 → cancelled=True；目录不合法（驱动器根/系统保护目录等）→ 400；
    当前环境无法弹出对话框 → 501。
    """
    try:
        selected = pick_directory()
    except RuntimeError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    if not selected:
        return DirectoryPickResponse(cancelled=True)
    try:
        path = validate_workspace_root(selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DirectoryPickResponse(cancelled=False, path=str(path), name=path.name)


@router.post(
    "/projects/create-folder",
    response_model=DirectoryPickResponse,
    dependencies=[Depends(require_local_client)],
)
def create_project_folder(req: ProjectMkdirRequest) -> DirectoryPickResponse:
    """在已选父目录下新建文件夹，返回新目录绝对路径（父目录同样受边界校验）。"""
    try:
        created = create_workspace_folder(req.parent_path, req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DirectoryPickResponse(cancelled=False, path=str(created), name=created.name)


@router.post(
    "/projects",
    response_model=ProjectResponse,
    dependencies=[Depends(require_local_client)],
)
def create_project(req: ProjectCreateRequest) -> ProjectResponse:
    """登记目录为项目并设为当前工作区（同路径已登记则复用，仅刷新最近打开时间）。"""
    try:
        path = validate_workspace_root(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProjectResponse(**asdict(get_project_store().register(path)))


@router.post(
    "/projects/{project_id}/open",
    response_model=ProjectResponse,
    dependencies=[Depends(require_local_client)],
)
def open_project(project_id: str) -> ProjectResponse:
    """切换到已登记的项目（project_id="default" 即内置默认工作区）。"""
    try:
        project = get_project_store().open(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}") from exc
    return ProjectResponse(**asdict(project))


@router.get(
    "/workspace/files",
    response_model=list[FileNode],
    dependencies=[Depends(require_local_client)],
)
def list_workspace_files() -> list[FileNode]:
    """列出工作区文件树（跳过隐藏目录和 __pycache__，限深 MAX_TREE_DEPTH）。"""
    ws_dir = _resolve_workspace_dir()
    return _build_file_tree(ws_dir, ws_dir)


@router.get(
    "/workspace/file",
    response_model=FileContentResponse,
    dependencies=[Depends(require_local_client)],
)
def read_workspace_file(path: str = Query(..., description="工作区相对路径")) -> FileContentResponse:
    """读取工作区内单个文件内容。

    分级策略：
    - ≤ MAX_EDITABLE_BYTES：返回全文，前端可编辑并保存；
    - > MAX_EDITABLE_BYTES：只读截断预览（truncated=True，前端禁止编辑）。

    始终只读前 MAX_EDITABLE_BYTES 字节，因此超大文件（含 GB 级）也能秒开，
    不会把整份内容塞进响应体 / 浏览器。

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


@router.put(
    "/workspace/file",
    response_model=FileContentResponse,
    dependencies=[Depends(require_local_client)],
)
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


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """先聊天，再干活 —— 判断这句话是闲聊还是执行诉求。

    - ``kind=chat``：直接回话，前端只显示这条回复，**不建任务、不出计划**；
    - ``kind=task``：前端转入任务链路（建任务 → 规划 → 执行）。

    无可用密钥时**不降级成固定工作流**（那会让用户又看到一份计划），而是如实告知。
    本端点无副作用：不读也不写任务存储。
    """
    client = get_llm_client(model=req.model, temperature=req.temperature)
    outcome = missing_key_outcome() if client is None else classify_intent(
        client, req.message, req.history
    )
    return ChatResponse(kind=outcome.kind, reply=outcome.reply, reason=outcome.reason)


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


@router.get("/task-summaries", response_model=list[TaskSummary])
def list_task_summaries(
    store: InMemoryTaskStore = Depends(get_store),
) -> list[TaskSummary]:
    """按更新时间倒序列出任务摘要（供前端「多任务并存、可回看」列表）。

    与 ``GET /tasks``（只返回 ID 列表，保持既有契约）互补；路径特意不含
    ``/tasks/{id}`` 前缀段，避免与任务详情路由产生路径歧义。
    """
    summaries: list[TaskSummary] = []
    for entry in store.list_summaries():
        state = entry.conductor.task_state
        summaries.append(
            TaskSummary(
                task_id=state.task_id,
                title=entry.title,
                requirement=entry.requirement,
                status=state.status.value if hasattr(state.status, "value") else str(state.status),
                current_stage=state.current_stage,
                created_at=state.created_at,
                updated_at=state.updated_at,
                has_pending_approval=entry.pending_approval is not None,
                # 列表与详情口径一致：非收敛停下时列表也要能显示「已停下」。
                stopped_reason=(
                    getattr(getattr(entry, "loop_state", None), "stopped_reason", "") or ""
                ),
            )
        )
    return summaries


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> dict[str, Any]:
    """删除任务：从内存任务存储中移除该任务条目。

    - 任务不存在 → 404（与 ``GET /tasks/{id}`` 一致）；
    - 存在 → 从 store 移除并返回 ``{"task_id": ..., "deleted": True}``。

    删除**不触发任何执行副作用**：后端执行是请求内同步完成的，不存在后台在跑的
    任务，因此删除不会中断任何运行中的工作。删除**不会**释放 ``data/projects.json``
    等项目状态（项目注册表与任务存储相互独立）。

    路径 ``/tasks/{task_id}``（DELETE）与 ``GET /tasks``（只返回 ID 列表）、
    ``GET /task-summaries`` 的既有契约互不冲突，后者保持不变。
    """
    if not store.remove(task_id):
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return {"task_id": task_id, "deleted": True}


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
    # 记录本次规划的执行选项（模型/温度/详细度/副结构自检/LLM 开关），供 /approve 阶段读取。
    entry.options = {
        "model": req.model,
        "temperature": req.temperature,
        "advanced": req.advanced,
        "self_check": req.self_check,
        "use_llm": req.use_llm,
        "council": req.council,
        "role_brief": req.role_brief,
        # 执行形态与副结构门控（P1）：执行阶段（/run、/approve、/resume）读取。
        "mode": req.mode,
        "sub_arch": req.sub_arch,
    }
    # 任务级 token 计量：规划阶段（decomposer）的用量也计入该任务。
    plan_usage = UsageAccumulator()
    llm_client = build_llm_client_or_none(
        req.use_llm,
        model=req.model,
        temperature=req.temperature,
        role="decomposer",
        usage_sink=plan_usage,
        role_brief_mode=req.role_brief,
    )
    # 评审会（可选，规划阶段自动）：先多角色独立表态并收敛，再让分解器据此拆分。
    # 会议必须使用 LLM——无可用密钥时 run_council 直接抛 E_VALIDATION（拒绝发起）。
    if req.council:
        try:
            entry.council = run_council(
                task_id,
                entry.requirement,
                llm_client=get_llm_client(model=req.model, temperature=req.temperature),
            )
        except AgentError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    # ③ agentic 去工作流化：**不再**预先跑 Decomposer 出一份 DAG。
    # 执行阶段的步骤由模型逐轮长出来（见 ``_execute_plan_agentic``）；预分解出的 DAG
    # 并不是真正会跑的东西，把它当主界面渲染，用户看到的永远是「一份固定工作流」。
    #
    # 只在「确实有可用密钥」时才跳过：无密钥时 agentic 会回退固定工作流（见
    # ``_execute_plan``），那时预分解仍是必须的，否则会没步骤可跑。
    agentic_no_dag = req.mode == "agentic" and (
        get_llm_client(
            model=req.model,
            temperature=req.temperature,
            role_brief_mode=req.role_brief,
        )
        is not None
    )
    if agentic_no_dag:
        try:
            entry.steps = {}
            entry.pending_questions = []
            plan = Plan(task_id=task_id, order=[], parallel_groups=[], confirmed_by_user=False)
            plan.validate_steps({})
            conductor.task_state.plan = plan
            conductor.handle_plan_ready()  # PLANNING → AWAITING_CONFIRM
        except AgentError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _record_usage(entry, plan_usage, mode=req.role_brief)
        status = conductor.task_state.status
        return DecomposeResponse(
            task_id=task_id,
            status=status.value if hasattr(status, "value") else str(status),
            mode=req.mode,
            council=entry.council,
        )

    decomposer = Decomposer(correlation_id=task_id)
    try:
        result = decomposer.decompose(
            task_id=task_id,
            requirement=entry.requirement,
            llm_client=llm_client,
            detail_level=req.advanced,
        )
        # 保存 steps 到 entry，供 approve 阶段 run_plan 使用。
        entry.steps = result.steps
        # 保存待确认问题，供交接提示的「人的未决」使用。
        entry.pending_questions = list(result.pending_questions)
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
    _record_usage(entry, plan_usage, mode=req.role_brief)
    status = conductor.task_state.status
    return DecomposeResponse(
        task_id=task_id,
        status=status.value if hasattr(status, "value") else str(status),
        mode=req.mode,
        steps=result.to_dict()["steps"],
        order=result.order,
        parallel_groups=result.parallel_groups,
        pending_questions=result.pending_questions,
        high_risk_actions=_high_risk_actions(result.steps),
        council=entry.council,
    )


@router.post("/tasks/{task_id}/council", response_model=dict[str, Any])
def run_council_endpoint(
    task_id: str,
    req: CouncilRequest,
    store: InMemoryTaskStore = Depends(get_store),
) -> dict[str, Any]:
    """手动召开评审会：多角色独立表态后收敛为结构化纪要。

    - 议题缺省用任务原始需求；
    - 无可用 LLM 密钥 → 409（拒绝发起，不产出空壳纪要）；
    - 纪要存回该任务，可供前端展示与刷新恢复。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    topic = (req.topic or entry.requirement).strip()
    try:
        minutes = run_council(
            task_id,
            topic,
            llm_client=get_llm_client(),
            participants=req.participants,
            rounds=req.rounds,
        )
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    entry.council = minutes
    return minutes


def _pending_high_risk(entry: Any) -> dict[str, Any] | None:
    """列出计划中**尚未被放行**的高风险步骤；全部已放行或无高风险时返回 None。

    「到点暂停」的判定依据：只要存在未放行的高风险动作，任务就挂起等用户决定，
    而不是让门卫把它判成不可重试的失败（E_PERMISSION）。
    """
    steps = getattr(entry, "steps", None) or {}
    risky = set(_high_risk_actions(steps))
    granted = getattr(entry, "approved_tools", None) or set()
    ungranted = [
        {"step_id": sid, "action": step.action}
        for sid, step in steps.items()
        if step.action in risky and step.action not in granted
    ]
    if not ungranted:
        return None
    return {"tools": sorted({item["action"] for item in ungranted}), "steps": ungranted}


def _mark_plan_ready(entry: Any, task_id: str) -> None:
    """把任务从 PLANNING 推到 AWAITING_CONFIRM（agentic 直跑 ``/run`` 时用）。

    agentic 下没有预先分解的 DAG，所以这里补一个**空计划**占位：状态机契约
    （``planning + plan_ready → awaiting_confirm``）不被改动，只是不再需要"先分解"。
    """
    conductor = entry.conductor
    if conductor.task_state.plan is None:
        plan = Plan(task_id=task_id, order=[], parallel_groups=[], confirmed_by_user=False)
        plan.validate_steps({})
        conductor.task_state.plan = plan
    try:
        conductor.handle_plan_ready()  # PLANNING → AWAITING_CONFIRM
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc


def _pause_for_high_risk(entry: Any, task_id: str) -> TaskResponse:
    """把任务挂起在「等待放行」：EXECUTING → INTERRUPTED + 记录待放行清单。"""
    entry.pending_approval = _pending_high_risk(entry)
    try:
        entry.conductor.handle_interrupt("high_risk_pending")  # EXECUTING → INTERRUPTED
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    return _to_task_response(entry, task_id)


# 执行结果状态 → ``Step.status``。Step 只认 pending/running/done/failed/skipped；
# 「待放行」是"还没执行"，保持 pending。
_STEP_STATUS_FROM_RESULT: dict[str, str] = {
    "done": "done",
    "failed": "failed",
    "skipped": "skipped",
}


def _sync_step_status(entry: Any) -> None:
    """把执行结果回填到 ``entry.steps[*].status``。

    此前 steps 只在执行前落一次（``to_step`` 默认 ``pending``），跑完再看还是 ``pending`` ——
    而 ``execution_results`` 已经明确写着 done / failed，两边对不上（实测两次运行都是如此）。
    """
    by_id = {
        str(item.get("step_id")): item
        for item in (getattr(entry, "execution_results", None) or [])
        if isinstance(item, dict)
    }
    for step_id, step in (getattr(entry, "steps", None) or {}).items():
        item = by_id.get(str(step_id))
        if item is None:
            continue
        mapped = _STEP_STATUS_FROM_RESULT.get(str(item.get("status") or ""))
        if mapped is not None:
            step.status = mapped


def _agentic_no_model_error(task_id: str) -> AgentError:
    """agentic 缺模型密钥时的统一错误：可读、不可重试。

    后端重启会清空内存里的密钥，此时 agentic 根本无法决策；以前这种情况会静默走到
    ``_execute_plan`` 的空计划分支直接 return —— 任务停在 EXECUTING 且什么都没执行，
    之后每次 ``/run`` 都 409「非法状态转换」，任务被彻底卡死。
    """
    return AgentError(
        "E_MODEL",
        "未配置可用的模型密钥，agentic 无法执行；请先保存 API 密钥后重新执行",
        source="api.routes",
        correlation_id=task_id,
        retryable=False,
    )


def _agentic_model_available(entry: Any) -> bool:
    """agentic 执行是否具备可用模型客户端（无密钥 = 不可用）。"""
    return (
        get_llm_client(
            model=entry.options.get("model"),
            temperature=entry.options.get("temperature"),
        )
        is not None
    )


def _execute_plan(entry: Any, task_id: str, approved_tools: set[str]) -> None:
    """执行已确认计划：注入用量采集、落执行结果与审计、按结果推进状态。

    按 ``entry.options["mode"]`` 分流：
    - ``agentic``（默认）且**存在可用密钥** → 模型逐轮决策（``_execute_plan_agentic``）；
    - 其余（``workflow``，或 ``agentic`` 但无可用密钥）→ 固定工作流（``run_plan``），
      这就是"无密钥自动回退静态映射"的落地。

    Args:
        approved_tools: 本次实际放行的工具集合（由调用方决定累积策略）。
    """
    conductor = entry.conductor
    plan = conductor.task_state.plan
    brief_mode = entry.options.get("role_brief")

    # agentic 决策必须用真实 LLM；无密钥时**不报错**，直接回退固定工作流（行为同今天）。
    if entry.options.get("mode", "agentic") == "agentic":
        agentic_usage = UsageAccumulator()
        agentic_client = get_llm_client(
            model=entry.options.get("model"),
            temperature=entry.options.get("temperature"),
            usage_sink=agentic_usage,
            role_brief_mode=brief_mode,
        )
        if agentic_client is not None:
            _execute_plan_agentic(entry, task_id, approved_tools, agentic_client, agentic_usage)
            _record_usage(entry, agentic_usage, mode=brief_mode)
            # 非收敛停下时状态仍留在 executing（没有转换）→ 这里补刷一次时间戳，
            # 否则前端「已深度思考（用时 N 秒）」里的 N 恒为 0。
            conductor.touch()
            return

    if plan is None or not entry.steps:
        return  # 空计划 → 保持 EXECUTING，等用户中断或重新规划

    # 「LLM 拆分」开关打开且存在可用密钥时，把 LLM 客户端注入执行阶段，
    # 让命中的 agent 角色能用前端配置的密钥；否则保持纯工具执行。
    # 任务级 token 计量：执行阶段各角色（按角色派生实例）共用同一个采集器。
    exec_usage = UsageAccumulator()
    exec_llm_client = None
    if entry.options.get("use_llm"):
        exec_llm_client = get_llm_client(
            model=entry.options.get("model"),
            temperature=entry.options.get("temperature"),
            usage_sink=exec_usage,
            role_brief_mode=brief_mode,
        )
    try:
        execution_results, gatekeeper_audit = run_plan(
            task_id=task_id,
            plan=plan,
            steps=entry.steps,
            llm_client=exec_llm_client,
            role_brief_mode=brief_mode,
            approved_tools=approved_tools,
        )
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    entry.execution_results = execution_results
    entry.gatekeeper_audit = gatekeeper_audit
    _sync_step_status(entry)  # steps 状态与执行结果对齐（否则永远是 pending）
    _record_usage(entry, exec_usage, mode=brief_mode)
    # 返工计量：Router 层重试次数之和（供审计员对照基线）。
    entry.rework_count += sum(int(r.get("retries") or 0) for r in execution_results)
    # 同 agentic：执行结束但状态未转换（非全 done）时也要推进 updated_at，否则「用时」恒为 0。
    conductor.touch()

    # 副结构自检（开关在 /plan 阶段设置）：纯计算核对，不改变执行结论。
    if entry.options.get("self_check"):
        entry.self_check = check_execution_consistency(plan, entry.steps, execution_results)

    # 全部 done → 转 VERIFYING；否则保持 EXECUTING 让用户决策。
    all_done = bool(execution_results) and all(
        r.get("status") == "done" for r in execution_results
    )
    if all_done:
        try:
            conductor.handle_all_steps_done()  # EXECUTING → VERIFYING
        except AgentError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc


def _loop_decision(decision: Decision) -> LoopDecision:
    """循环内决策 → 可回传形态（``LoopState`` 里存这个）。"""
    return LoopDecision(
        kind=decision.kind,
        thought=decision.thought,
        role=decision.role,
        action=decision.action,
        inputs=dict(decision.inputs),
        reason=decision.reason,
    )


def _decision_from(payload: LoopDecision) -> Decision:
    """``LoopState.pending_decision`` → 循环内决策（``/resume`` 重放那一步用）。"""
    return Decision(
        kind=payload.kind,
        thought=payload.thought,
        role=payload.role,
        action=payload.action,
        inputs=dict(payload.inputs),
        reason=payload.reason,
    )


def _loop_state_from(
    outcome: LoopOutcome,
    budget: LoopBudget,
    context: LoopContext,
    *,
    base_rounds: list[LoopRoundResult] | None = None,
) -> LoopState:
    """构造循环上下文快照（累积历轮，供回看 / 审计 / ``/resume`` 续跑）。"""
    rounds = list(base_rounds or [])
    rounds.extend(
        LoopRoundResult(
            round=item.round_index,
            decision=_loop_decision(item.decision),
            status=item.status,
            summary=item.summary,
            artifacts=list(item.artifacts),
            tokens=item.tokens,
        )
        for item in outcome.rounds
    )
    return LoopState(
        rounds=rounds,
        artifacts=list(context.artifacts),
        budget=LoopBudgetState(
            steps_used=budget.steps_used,
            max_steps=budget.max_steps,
            tokens_used=budget.tokens_used,
            max_tokens=budget.max_tokens,
            stagnant_rounds=budget.stagnant_rounds,
            max_stagnant_rounds=budget.max_stagnant_rounds,
        ),
        pending_decision=(
            _loop_decision(outcome.pending_decision)
            if outcome.pending_decision is not None
            else None
        ),
        stopped_reason=outcome.stopped_reason,
        decided_by=outcome.decided_by,
        final_answer=outcome.final_answer,
    )


def _execute_plan_agentic(
    entry: Any,
    task_id: str,
    approved_tools: set[str],
    llm_client: Any,
    usage: UsageAccumulator,
) -> None:
    """agentic 执行：模型逐轮决策 → 单步执行，直到收敛 / 预算耗尽 / 高风险挂起。

    与固定工作流**共用** ``build_step_executor``，所以权限 / 沙箱 / 审计语义完全一致。
    每次高风险动作仍走「到点暂停」：``run_step`` 把 PermissionError 转成
    ``pending_approval``，循环据此停下并保存待放行决策；``/resume`` 放行后
    **原样重放那一步**（``PrefixedDecider``），保证"用户批准的就是实际执行的那一步"。
    """
    conductor = entry.conductor
    brief_mode = entry.options.get("role_brief")
    sub_arch = bool(entry.options.get("sub_arch"))
    executor, gatekeeper = build_step_executor(
        task_id,
        llm_client=llm_client,
        role_brief_mode=brief_mode,
        approved_tools=approved_tools,
    )

    # 续跑：从 loop_state 恢复历史 / 产物 / 预算与轮次偏移（没有则从零开始）。
    previous = entry.loop_state
    base_rounds = list(previous.rounds) if previous is not None else []
    previous_budget = previous.budget if previous is not None else None
    if previous is not None and previous.pending_decision is not None:
        # 重放挂起那一步：把它的旧记录（轮次 + 执行结果行）撤掉，用**同一编号**重记，
        # 否则同一动作会留下"待放行"与"已完成"两条，且全 done 判定永远不成立。
        base_rounds = base_rounds[:-1]
        last_row = entry.execution_results[-1] if entry.execution_results else None
        if last_row is not None and last_row.get("status") == "pending_approval":
            entry.execution_results = entry.execution_results[:-1]
    history = [
        {
            "round": item.round,
            "thought": item.decision.thought,
            "role": item.decision.role,
            "action": item.decision.action,
            "status": item.status,
            "summary": item.summary,
        }
        for item in base_rounds
    ]
    context = LoopContext(
        requirement=entry.requirement,
        catalog=build_catalog(sub_arch=sub_arch),
        history=history,
        artifacts=list(previous.artifacts) if previous is not None else [],
        allow_propose=sub_arch,
    )
    # 步数 / token 累计（兜住无限往复）；空转计数**不跨放行继承** —— 用户放行是一次
    # 新的干预，理应给一轮新机会，否则「挂起 + 放行后一次失败」会被凑成两轮空转而过早停下。
    budget = LoopBudget(
        steps_used=previous_budget.steps_used if previous_budget else 0,
        tokens_used=previous_budget.tokens_used if previous_budget else 0,
    )
    decider: Any = LLMRouteDecider(llm_client=llm_client)
    if previous is not None and previous.pending_decision is not None:
        decider = PrefixedDecider(
            prelude=[_decision_from(previous.pending_decision)], decider=decider
        )

    def _execute(step: Step) -> StepOutcome:
        """执行一步：高风险未放行 → 不执行、转「到点暂停」；否则走派发链路。

        「到点暂停」必须在**进入工具链之前**判定：门卫对未授权高风险工具的裁决是
        ``E_PERMISSION``（失败），而产品语义是"等用户拍板"（挂起），两者不能混。
        门卫仍会在真正执行时兜底（放行集合与角色权限不一致时依旧拦下）。
        """
        if is_high_risk(step.assignee or "", step.action) and step.action not in approved_tools:
            return StepOutcome(
                status="pending_approval",
                summary=f"等待你放行：{action_title(step.action)}",
            )
        before = usage.total.total_tokens
        outcome = run_step(step, executor)
        outcome.tokens = max(usage.total.total_tokens - before, 0)
        # 取走这一轮工具的原始返回：① 回灌提示词（模型据此决定下一步）；
        # ② 角色派发路径会把原始输出吃进自己的结果对象，摘要只剩「列出文件完成」
        #    这类空话 —— 有原始返回时就补成带内容的人话（只补这一种情况，不动别的摘要）。
        outcome.observation = take_tool_observation()
        if (
            outcome.status == "done"
            and outcome.observation
            and outcome.summary == f"{action_title(step.action)}完成"
        ):
            outcome.summary = summarize_result(
                action=step.action,
                status="done",
                result=outcome.observation,
                inputs=step.inputs,
            )
        return outcome

    outcome = run_agent_loop(
        context=context,
        decider=decider,
        execute=_execute,
        budget=budget,
        start_index=len(base_rounds),
        # 「用户中断」落地：/interrupt 把状态翻成 interrupted，同步阻塞的循环在
        # 每个轮次边界据此退出 —— 否则点了「⏸ 中断」界面变「已暂停」、后台却还在跑。
        should_stop=lambda: conductor.task_state.status is TaskStatus.INTERRUPTED,
    )

    # 落步骤（``GET /tasks/{id}`` 的 steps，回看用）+ 执行结果（复用现有卡片渲染）。
    for item in outcome.rounds:
        step = to_step(item.decision, round_index=item.round_index)
        entry.steps[step.id] = step
    entry.execution_results = list(entry.execution_results) + to_execution_results(outcome)
    entry.gatekeeper_audit = gatekeeper.snapshot()
    entry.loop_state = _loop_state_from(outcome, budget, context, base_rounds=base_rounds)
    _sync_step_status(entry)  # steps 状态与执行结果对齐（否则永远是 pending）

    # 用户中断：状态已由 /interrupt 翻成 interrupted，这里**不做任何收尾转换**
    # （否则「结果都 done」会把中断中的任务误推到 verifying，覆盖用户的暂停意图）。
    if outcome.stopped_reason == STOP_INTERRUPTED:
        return

    # 模型提议「扩编」→ 不再丢弃：落成契约记录 + 算好待落盘内容，挂起等用户决定。
    if outcome.stopped_reason == STOP_PROPOSED and outcome.proposal is not None:
        draft = outcome.proposal.proposal
        if draft is not None:
            record, files = _proposal_record(draft, task_id, seq=len(entry.proposals) + 1)
            entry.proposals.append(record)
            entry.pending_proposal = ProposalView(
                proposal=record, accepts=list(draft.accepts), rationale=draft.rationale
            )
            # 待落盘内容此刻就算好：批准时写入的就是用户在卡片上预览过的那份。
            entry.pending_scaffold = files
            try:
                conductor.handle_interrupt("proposal_pending")  # EXECUTING → INTERRUPTED
            except AgentError as exc:
                raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
            return

    if outcome.stopped_reason == STOP_PENDING_APPROVAL and outcome.pending_decision is not None:
        action = outcome.pending_decision.action or ""
        last = outcome.rounds[-1] if outcome.rounds else None
        entry.pending_approval = {
            "tools": [action] if action else [],
            "steps": [
                {
                    "step_id": f"{STEP_ID_PREFIX}-{last.round_index:03d}" if last else "",
                    "action": action,
                }
            ],
        }
        try:
            conductor.handle_interrupt("high_risk_pending")  # EXECUTING → INTERRUPTED
        except AgentError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        return

    # 收尾判定：**模型自己说完成了**（``stopped_reason == final``）也算收尾 —— 与固定
    # 工作流的「全部 done」同口径。此前只认「所有步骤都 done」，于是"模型收敛了、但中间
    # 有失败步骤"的任务会永远卡在 EXECUTING（顶栏一直「执行中」，没有任何终态）。
    # 非收敛终止（预算耗尽 / 空转 / 决策非法）仍留在 EXECUTING，由用户决定。
    all_done = bool(entry.execution_results) and all(
        r.get("status") == "done" for r in entry.execution_results
    )
    if all_done or outcome.stopped_reason == STOP_FINAL:
        try:
            conductor.handle_all_steps_done()  # EXECUTING → VERIFYING
        except AgentError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc


def _proposal_record(
    draft: ProposalDraft, task_id: str, *, seq: int
) -> tuple[ChangeProposal, dict[str, str]]:
    """把模型草案落成契约记录 + **待落盘内容**（此处不写盘）。

    Returns:
        ``(ChangeProposal, files)``：前者进任务响应供批准卡渲染与审计；后者是
        ``相对工作区路径 → 内容``，由 ``/proposal/approve`` 写入 —— 用户批准的就是
        他在卡片上预览过的那份内容。
    """
    proposal_id = f"{task_id}-p{seq}"
    files = plan_scaffold(draft, proposal_id)
    record = ChangeProposal(
        proposal_id=proposal_id,
        target_module=f"agent_builder/roles/{draft.name}.py",
        change_desc=draft.mission,
        diff_preview=render_role_module(draft, proposal_id),
        impacted_files=list(files),
        risk=draft.risk,
        verification_plan=f"并入后运行 tests/test_roles_{draft.name}.py（≥10 例）",
        status="proposed",
        version=SCAFFOLD_TEMPLATE_VERSION,
    )
    return record, files


def _decide_proposal(entry: Any, task_id: str, *, approve: bool) -> TaskResponse:
    """批准 / 拒绝当前待定提议（两个端点共用，避免决定逻辑写两遍）。

    批准 = 把预览时算好的脚手架写入工作区 ``proposals/<id>/``；**不改仓库既有文件**，
    因此新角色在人工并入授权前不会被派发（生成物里的 README 就是并入清单）。
    """
    view = entry.pending_proposal
    if view is None or view.proposal.status != "proposed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "E_VALIDATION",
                "message": "当前没有待决定的副结构提议",
            },
        )
    if not approve:
        view.proposal.status = "rejected"
        view.proposal.approved_by = "user"
        entry.pending_proposal = None
        entry.pending_scaffold = None
        _resume_after_proposal(entry)
        return _to_task_response(entry, task_id)

    files = entry.pending_scaffold or {}
    result = write_scaffold(_resolve_workspace_dir(), files)
    if result.error is not None:
        # 写盘失败就不改状态：提议仍可重试，避免"看起来批准了其实没落盘"。
        raise HTTPException(
            status_code=409,
            detail={"error": "E_VALIDATION", "message": f"脚手架写入失败：{result.error}"},
        )
    view.proposal.status = "approved"
    view.proposal.approved_by = "user"
    view.proposal.impacted_files = list(result.files)
    entry.pending_proposal = None
    entry.pending_scaffold = None
    _resume_after_proposal(entry)
    return _to_task_response(entry, task_id)


def _resume_after_proposal(entry: Any) -> None:
    """提议决定完成 → 把任务从挂起拉回 EXECUTING（状态机要求先 RESUME 再继续）。"""
    try:
        entry.conductor.handle_resume()  # INTERRUPTED → EXECUTING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc


@router.post(
    "/tasks/{task_id}/proposal/approve",
    response_model=TaskResponse,
    dependencies=[Depends(require_local_client)],
)
def approve_proposal(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """批准副结构提议：把脚手架写入工作区（**不改仓库既有文件**）。

    「批准」≠「立刻获得权限」：新角色要人工并入 ``tools/permissions.py`` 授权后
    才会进权限矩阵与角色目录，在此之前不会被派发。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return _decide_proposal(entry, task_id, approve=True)


@router.post(
    "/tasks/{task_id}/proposal/reject",
    response_model=TaskResponse,
    dependencies=[Depends(require_local_client)],
)
def reject_proposal(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """拒绝副结构提议：只记录决定，不写任何文件。"""
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return _decide_proposal(entry, task_id, approve=False)


@router.post("/tasks/{task_id}/run", response_model=TaskResponse)
def run_task(
    task_id: str,
    req: TaskApproveRequest | None = None,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """直接执行（**不强制前置确认**；用户可随时中断）。

    默认「能跑就跑」。只有当计划里存在**尚未放行的高风险步骤**（写 / 删除 /
    提交 / 回滚）时才「到点暂停」：状态转 INTERRUPTED，并在 ``pending_approval``
    列出待放行的工具与步骤；用户 ``POST /resume`` 放行后继续。安全边界不放松
    ——放行仍走逐工具授权通道，只是把「前置确认」改成「到点提醒」。

    ``approved_tools`` 会被**累积**到任务级授权集合（放行一次后不再重复询问）。

    agentic 下「先分解出 DAG 再确认」这一步已去工作流化，所以本端点也接受
    ``planning`` 直跑：直接带原始需求进模型循环（内部补一次 plan_ready）。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    if entry.conductor.task_state.status is TaskStatus.PLANNING:
        _mark_plan_ready(entry, task_id)

    # agentic + 无可用模型 + **没有可回退的固定计划**（``entry.steps`` 为空）→ 一定跑不起来。
    # 这时**先**判断再推进状态：否则会先转 EXECUTING 再发现没模型 → 任务卡在 executing
    # 什么都没执行，之后每次 /run 都 409「非法状态转换」（实测踩到）。拦在这里，任务仍留在
    # AWAITING_CONFIRM，存好密钥再 /run 就能恢复。
    # 注意：有 steps 时**不拦** —— 那是「无密钥回退固定工作流」的有效路径，照常执行。
    if (
        entry.options.get("mode", "agentic") == "agentic"
        and not entry.steps
        and not _agentic_model_available(entry)
    ):
        raise HTTPException(status_code=409, detail=_agentic_no_model_error(task_id).to_dict())

    try:
        entry.conductor.handle_plan_accepted()  # AWAITING_CONFIRM → EXECUTING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc

    if req and req.approved_tools:
        entry.approved_tools |= set(req.approved_tools)

    if _pending_high_risk(entry) is not None:
        return _pause_for_high_risk(entry, task_id)

    entry.pending_approval = None
    _execute_plan(entry, task_id, entry.approved_tools)
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/approve", response_model=TaskResponse)
def approve_task(
    task_id: str,
    req: TaskApproveRequest | None = None,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户确认计划 → 状态转 EXECUTING → 执行编排器派发各步骤。

    步骤全部 done 时自动转 VERIFYING；出现 failed/pending_approval 则保持
    EXECUTING，由前端展示执行结果卡片供用户决策（重试/中断）。

    ``approved_tools`` 是**逐工具授权**通道：高风险（写/删除/提交/回滚）步骤只有在
    该列表中才会带 ``granted_by`` 通过审批门；未列出的高风险步骤会被门卫拒绝。
    **省略该字段 = 不授权任何高风险工具。**

    > 这是「前置确认」的严格通道（保持既有契约）。想要「默认直接跑 + 到点暂停」
    > 的柔性流程，请用 ``POST /tasks/{id}/run``。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    conductor = entry.conductor
    try:
        conductor.handle_plan_accepted()  # AWAITING_CONFIRM → EXECUTING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc

    # 逐工具授权：仅显式列出的高风险工具才带 granted_by 通过审批门（省略 = 不授权）。
    approved_tools: set[str] = (
        set(req.approved_tools) if req and req.approved_tools else set()
    )
    entry.approved_tools |= approved_tools
    entry.pending_approval = None
    # 注意：这里只传本次显式授权的集合（严格契约），累积集合只用于 /run 与 /resume。
    _execute_plan(entry, task_id, approved_tools)
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/reject", response_model=TaskResponse)
def reject_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户拒绝 / 修改计划 → 状态回 PLANNING（两个来源：等待确认、已暂停）。

    - ``awaiting_confirm``：常规「改计划」；
    - ``interrupted``：**「到点暂停」待放行**或手动中断后改计划
      （状态机已定义 ``interrupted + plan_rejected → planning``）。

    从暂停态退回时一并清掉 ``pending_approval``：该清单描述的是**旧计划**里待放行的
    高风险步骤，计划即将被重新生成，留着会让任务列表继续显示「待放行」、
    并可能让用户对已作废的计划点「放行」。

    同时**清零任务级放行记录** ``approved_tools``：改计划 = 作废旧计划的授权，
    新计划里的高风险步骤必须重新逐工具放行（否则旧授权会「继承」到一份用户还没
    看过的计划上，静默执行写 / 删 / 提交）。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_plan_rejected()  # → PLANNING（含 interrupted 来源）
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    entry.pending_approval = None
    entry.approved_tools = set()
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
    req: TaskResumeRequest | None = None,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户恢复 → 状态转 EXECUTING。

    两种来源分开处理：

    - **因高风险动作挂起**（``pending_approval`` 非空）：本请求可携带
      ``approved_tools`` 放行；放行后若仍有未放行的高风险步骤，任务会**再次挂起**
      （而不是被门卫判成不可重试失败），全部放行后才继续执行剩余计划。
    - **用户手动中断**：只翻转状态，不自动重跑计划（避免重复副作用）；是否继续
      由用户在前端决定。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    paused_for_approval = entry.pending_approval is not None
    try:
        entry.conductor.handle_resume()  # INTERRUPTED → EXECUTING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc

    if req and req.approved_tools:
        entry.approved_tools |= set(req.approved_tools)

    if not paused_for_approval:
        return _to_task_response(entry, task_id)

    # agentic 模式：挂起点由循环自己管理（``loop_state.pending_decision`` + ``PrefixedDecider``
    # 原样重放），不走「按 entry.steps 重新扫描高风险步骤」那套 —— agentic 的步骤是
    # 模型动态生成的，计划里本就没有一份可预先扫描的清单。
    if entry.options.get("mode", "agentic") == "agentic" and entry.loop_state is not None:
        entry.pending_approval = None
        _execute_plan(entry, task_id, entry.approved_tools)
        return _to_task_response(entry, task_id)

    if _pending_high_risk(entry) is not None:
        return _pause_for_high_risk(entry, task_id)

    entry.pending_approval = None
    _execute_plan(entry, task_id, entry.approved_tools)
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


@router.post("/tasks/{task_id}/deliver", response_model=TaskResponse)
def deliver_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户确认交付 → 从 ``verifying`` 一路推到 ``delivered``（交付出口）。

    agentic 收敛后任务停在 ``verifying``（顶栏「验证中」）等用户拍板。此前**没有
    任何出口**：既不会自动交付，UI 也没有按钮 → 任务永远停在「验证中」（实机踩过）。
    本端点走契约里**已有**的两步转换 ``verify_passed → delivering → delivered``，
    不改状态机契约；只在 ``verifying`` 合法，其余状态 409。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_verify_passed()  # VERIFYING → DELIVERING
        entry.conductor.deliver()  # DELIVERING → DELIVERED
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    return _to_task_response(entry, task_id)


@router.get("/tasks/{task_id}/usage", response_model=dict[str, Any])
def get_task_usage(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> dict[str, Any]:
    """读取任务级 token 计量（供 A/B 对照取 P1 成本指标）。

    只读、幂等、无副作用；返回 ``TaskEntry.usage``（数值字段 + ``by_role`` 明细 +
    记录时的角色简报档位 ``mode``）。尚未产生用量时返回空字典。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return entry.usage or {}


# ── 交接提示（handover hint）──
# 规则式判定（不调 LLM）：门控（轮次 / token / 字符）+ 未决项 + 任务状态综合。
# 端点均挂 require_local_client：只允许本机前端调用。


@router.get(
    "/tasks/{task_id}/handover",
    response_model=HandoverResponse,
    dependencies=[Depends(require_local_client)],
)
def get_handover(
    task_id: str,
    turns: int = Query(0, ge=0, description="对话轮次（用户消息数）"),
    context_tokens: int = Query(0, ge=0, description="累计 token 量"),
    context_chars: int = Query(0, ge=0, description="累计字符量"),
    store: InMemoryTaskStore = Depends(get_store),
) -> HandoverResponse:
    """读取交接判定与结构化交接对象（幂等，无副作用）。

    门控由前端上报的轮次 / token / 字符触发；未决项与 ack 来自后端 TaskEntry。
    本版不调 LLM（``generated_by='rule'``）。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")

    state = entry.conductor.task_state
    status_value = state.status.value if hasattr(state.status, "value") else str(state.status)
    ack = entry.handover_ack or {}
    ack_status = ack.get("status") or "none"

    # 收集未决项数量用于判定。
    human, machine = collect_open_items(entry)
    open_items = len(human) + len(machine)

    decision = should_suggest_handover(
        turns=turns,
        context_tokens=context_tokens,
        context_chars=context_chars,
        task_status=status_value,
        prompts_shown=entry.handover_prompts_shown,
        ack_status=ack_status,
        open_items=open_items,
    )
    return HandoverResponse(
        **build_handover_brief(
            entry,
            decision,
            turns=turns,
            context_tokens=context_tokens,
            context_chars=context_chars,
        )
    )


@router.post(
    "/tasks/{task_id}/handover/ack",
    response_model=HandoverResponse,
    dependencies=[Depends(require_local_client)],
)
def ack_handover(
    task_id: str,
    req: HandoverAckRequest,
    store: InMemoryTaskStore = Depends(get_store),
) -> HandoverResponse:
    """记录交接提示的 ack（已查看 / 已忽略），用于限次与去重。

    - ``seen``：计入已提示次数（限次），卡片可收起；
    - ``ignored``：本会话不再重复提示（去重）。
    返回更新后的交接对象（前端据此刷新卡片状态）。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry.handover_ack = {"status": req.status, "at": now_iso}
    # 「已查看」计入提示次数（限次）；「已忽略」不计入但触发去重。
    if req.status == ACK_SEEN:
        entry.handover_prompts_shown += 1

    # 重新判定（ack 变化会影响 strength）。
    state = entry.conductor.task_state
    status_value = state.status.value if hasattr(state.status, "value") else str(state.status)
    human, machine = collect_open_items(entry)
    open_items = len(human) + len(machine)
    decision = should_suggest_handover(
        turns=0,
        context_tokens=0,
        context_chars=0,
        task_status=status_value,
        prompts_shown=entry.handover_prompts_shown,
        ack_status=req.status,
        open_items=open_items,
    )
    return HandoverResponse(
        **build_handover_brief(
            entry,
            decision,
            turns=0,
            context_tokens=0,
            context_chars=0,
        )
    )


__all__ = ["router"]
