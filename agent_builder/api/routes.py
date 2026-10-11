"""HTTP 路由 —— 薄包装 Conductor 状态机 + Decomposer + 工作区文件树。"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_builder.api.agent_loop import (
    BUDGET_STEP_INCREMENT,
    MAX_BUDGET_STEPS_LIMIT,
    MAX_LOOP_STEPS,
    STEP_ID_PREFIX,
    STOP_BUDGET_STEPS,
    STOP_BUDGET_TOKENS,
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
from agent_builder.api.approval_store import (
    grant_workspace_approvals,
    read_workspace_approvals,
    revoke_workspace_approvals,
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
from agent_builder.api.project_log import (
    ProjectLogEntry,
    build_project_context,
    read_project_log,
    record_project_log,
    workspace_file_snapshot,
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
    CapabilitiesResponse,
    CapabilityGroupView,
    ChatRequest,
    ChatResponse,
    CouncilRequest,
    DecomposeResponse,
    DirectChatRequest,
    DirectChatResponse,
    DirectoryPickResponse,
    EffectiveOption,
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
    TaskContinueRequest,
    TaskCreateRequest,
    TaskInstructRequest,
    TaskResponse,
    TaskResumeRequest,
    TaskSummary,
)
from agent_builder.api.secret_store import (
    PERSIST_ENV,
    delete_secret,
    load_secret,
    persist_enabled,
    save_secret,
    secret_path,
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
from agent_builder.contracts.errors import AgentError, model_error
from agent_builder.contracts.schemas import ChangeProposal, Plan, Step
from agent_builder.contracts.state_machine import TaskStatus
from agent_builder.llm.client import UsageAccumulator, merge_usage, probe_api_key
from agent_builder.llm.config import LLMConfig
from agent_builder.narrate import action_title, conclude, summarize_result
from agent_builder.roles.decomposer import Decomposer
from agent_builder.tools.gatekeeper import inside_workspace, workspace_path
from agent_builder.tools.permissions import (
    CAPABILITY_GROUPS,
    CAPABILITY_LABELS,
    CAPABILITY_NOTES,
    DEFAULT_CAPABILITIES,
    WORKSPACE_SCOPED_WRITE_TOOLS,
    filter_role_perms,
    get_default_role_perms,
)

router = APIRouter()

# 文件树最大递归深度（防止深层目录遍历开销过大）。
MAX_TREE_DEPTH = 8

# 可编辑上限：不超过此大小 → 返回全文，可编辑并保存。
MAX_EDITABLE_BYTES = 2 * 1024 * 1024

# 可读执行计划的长度上限（只是给人看的摘要，不该长篇大论）。
MAX_PLAN_TEXT_CHARS = 1200
# 「上文」的长度上限：够放下上一任务的需求 + 结论 + 产出清单，又不至于把提示词撑爆。
MAX_CONTEXT_CHARS = 4000

logger = logging.getLogger(__name__)

_PLAN_TEXT_PROMPT = """你是执行编排的规划助手。请用**中文**给出一份简短、可核对的执行计划，
让用户在开始执行之前一眼看懂「你打算做什么」。

要求：
1. 3~6 条，每条一行，用「1. 」「2. 」这样的序号开头；
2. 每条写清「做什么 + 产出什么文件」，能确定就写文件路径；不要写空话套话；
3. 最后单独一行以「验证：」开头，说明打算怎么证明做成了（跑什么、看什么）；
4. **必须写明交付形态与入口**（CLI / Web 页面 / 库 / HTTP API）以及怎么启动。
   需求只说「给我做一个 X」而没说形态时，**按"人能打开的东西"补齐**：
   Web → 页面文件 + 提供页面的路由；CLI → 可执行入口 + 用法；
   确实拿不准，就在最后加一行「待确认：要 CLI 还是 Web 页面」——
   **不要默认只交一个后端库**（实测 2026-10-11：用户要"给我做一个 agent"，
   计划只产出 `src/agent.py` + `main.py`，没有任何前端）。
5. 只输出计划本身：不要前后客套、不要 markdown 代码块、不要重复需求原文。

【需求】
{requirement}"""


def _trim_plan_text(text: Any) -> str:
    """清洗计划文本：剥掉代码块围栏、去掉首尾空白、限长。"""
    value = str(text or "").strip()
    if value.startswith("```"):
        value = "\n".join(
            line for line in value.splitlines() if not line.strip().startswith("```")
        ).strip()
    return value[:MAX_PLAN_TEXT_CHARS]


def _clean_context(raw: Any) -> str:
    """清洗「上文」：去首尾空白、限长（**超长截断并注明**，绝不无声丢弃）。"""
    value = str(raw or "").strip()
    if len(value) > MAX_CONTEXT_CHARS:
        value = value[:MAX_CONTEXT_CHARS] + f"…（上文过长，已截断；原文 {len(value)} 字）"
    return value


def _task_artifacts(entry: Any) -> list[str]:
    """汇总任务产出（loop_state.artifacts + execution_results[].artifacts），去重保序。"""
    paths: list[str] = []
    loop_state = getattr(entry, "loop_state", None)
    for path in getattr(loop_state, "artifacts", None) or []:
        paths.append(str(path))
    for item in getattr(entry, "execution_results", None) or []:
        for path in (item.get("artifacts") if isinstance(item, dict) else None) or []:
            paths.append(str(path))
    seen: set[str] = set()
    return [p for p in paths if p and not (p in seen or seen.add(p))]


def _project_context_for_new_task(task_id: str) -> str:
    """新任务的**自动上文**：来自工作区级项目日志。

    这才是"后端自己记得"（实测 2026-10-11）：前端拼的上文在刷新 / 切项目 / 重启后即失效，
    而项目日志落在**工作区里**、由后端在任务收尾时自动写入。
    """
    try:
        workspace = _resolve_workspace_dir()
    except Exception:  # noqa: BLE001 - 项目状态异常不该拦住建任务
        return ""
    return build_project_context(read_project_log(workspace), exclude_task_id=task_id)


def _record_task_project_log(entry: Any) -> None:
    """把任务收尾状态写进工作区级项目日志（跨任务、跨重启）。失败不影响主流程。"""
    try:
        workspace = _resolve_workspace_dir()
        conductor = getattr(entry, "conductor", None)
        state = conductor.task_state
        task_id = str(state.task_id)
        status = state.status.value if hasattr(state.status, "value") else str(state.status)
        response = _to_task_response(entry, task_id)
        record_project_log(
            workspace,
            ProjectLogEntry(
                task_id=task_id,
                requirement=str(getattr(entry, "requirement", "") or ""),
                title=str(getattr(entry, "title", "") or ""),
                status=status,
                conclusion=str(getattr(response, "conclusion", "") or ""),
                artifacts=_task_artifacts(entry),
            ),
        )
    except Exception:  # 记账是旁路，任何异常都不能影响任务本身
        logger.warning("项目日志记账失败（忽略）", exc_info=True)


def _draft_plan_text(
    requirement: str, llm_client: Any, context_note: str = "", workspace_note: str = ""
) -> str:
    """让模型给一份**可读的执行计划**（只用于展示，不冻结执行）。

    为什么需要（实测 2026-10-11）：agentic 下 ``/plan`` 不跑 Decomposer、前端又只在
    workflow 模式渲染计划块 → 用户在整个执行期间只看到「收到，开始执行。」，
    完全不知道要做什么（黑箱）。

    无可用客户端 / 调用失败 / 返回空 → **一律返回空串，绝不影响任务创建**
    （计划文本是锦上添花，不能拖垮主流程）。

    ``context_note``（上文）会附在需求之后：规划时就要知道"后端已经存在"，
    否则第二阶段的计划会把已完成的事再排一遍。
    """
    if llm_client is None:
        return ""
    prompt = _PLAN_TEXT_PROMPT.format(requirement=requirement)
    if context_note:
        prompt += (
            "\n\n【上文】用户在同一个工作区里刚做完的事（据此判断哪些**已经做过**，"
            "不要再排一遍）：\n" + context_note
        )
    if workspace_note:
        prompt += (
            "\n\n【工作区现状】工作区里**已经有**这些文件（规划时要据此判断哪些已做完、"
            "哪些要接着改；不要把已完成的再排一遍，也不要从零重写）：\n" + workspace_note
        )
    try:
        text = llm_client.chat([{"role": "user", "content": prompt}])
    except Exception:  # 展示用信息，失败只记日志
        logger.warning("生成可读执行计划失败（忽略）", exc_info=True)
        return ""
    return _trim_plan_text(text)


def _resolve_workspace_dir() -> Path:
    """解析当前工作区目录。

    工作区由「当前项目」决定（见 agent_builder/api/projects.py）：
    - 未切换过项目 → 默认工作区项目（gatekeeper.WORKSPACE_DIR，不存在则项目根）；
    - 已切换项目 → 该项目目录（目录失效也**如实返回它自己**，不再偷偷换成别的目录，
      见 :meth:`ProjectStore.current_workspace_dir`）。
    """
    return get_project_store().current_workspace_dir()


def _high_risk_actions(
    steps: dict[str, Any],
    *,
    workspace: Path | None = None,
    count_in_workspace_writes: bool = True,
) -> list[str]:
    """本计划中属高风险（需审批）的 action 去重列表（跨角色并集）。

    高风险真源是 ``permissions.py`` 的 ``high_risk_tools``。此处取并集，供前端提示
    「哪些步骤需要逐工具授权」；运行时判定仍按步骤所属角色精确执行。

    **按实际风险判定**（卡 44）：``file_write`` / ``file_edit`` 是否高危取决于**落到哪里**——
    写在工作区内是本职工作（不算高危、不再打断用户），写到工作区外则由沙箱硬拒绝
    （``E_PERMISSION``，不是"等放行"）。判定复用门卫的
    :func:`agent_builder.tools.gatekeeper.inside_workspace`，不另写一套口径。

    Args:
        steps: ``step_id → Step``。
        workspace: 判定用的工作区；None 用当前工作区。
        count_in_workspace_writes: True 时把**工作区内的**写/改也计入高风险。这是**保守**
            默认（用于「挂起判定」之外的场景）：不确定就该多提示一次，绝不漏标。
            False 则按实际落点判定 —— 工作区内放行、工作区外仍算高风险。
            （工作区外/给不出路径的写，无论这个开关怎么设都算高风险。）
    """
    gated: set[str] = set()
    for perm in get_default_role_perms().values():
        gated.update(perm.high_risk_tools)
    risky: set[str] = set()
    for step in steps.values():
        action = str(getattr(step, "action", "") or "")
        if not action:
            continue
        if action in gated:
            risky.add(action)
            continue
        # 工作区边界类写/改：高危与否取决于**落到哪里**。
        if action not in WORKSPACE_SCOPED_WRITE_TOOLS:
            continue
        if count_in_workspace_writes:
            risky.add(action)  # 保守默认：不区分落点
            continue
        path_str = workspace_path(getattr(step, "inputs", None))
        # 给不出路径 → 不能假设它落在工作区内（宁可多提示一次，也不能漏标）；
        # 越界路径同样算高风险（真正执行时会被沙箱硬拒绝）。
        if not path_str or not inside_workspace(path_str, workspace):
            risky.add(action)
    return sorted(risky)


def _task_capabilities(entry: Any) -> set[str] | None:
    """本任务启用的能力组；None = 全部启用（未在 ``/plan`` 里设过开关）。

    **空列表与 None 必须区分**：``[]`` = 用户把能力全关了（只剩审计/记忆类基础设施），
    None = 没设置过（沿用既有行为 = 全开）。写成 ``or None`` 会把两者混成同一个语义。
    """
    options = getattr(entry, "options", None) or {}
    raw = options.get("capabilities")
    if raw is None:
        return None
    return {str(item) for item in raw}


def _task_role_perms(entry: Any) -> dict[str, Any]:
    """本任务生效的角色权限矩阵（已按能力开关收窄）——审计/展示用。"""
    return filter_role_perms(get_default_role_perms(), _task_capabilities(entry))


def _run_self_check(entry: Any) -> None:
    """执行收尾时跑一次「计划 ↔ 执行结果」一致性自检（卡 46b：**两种模式都生效**）。

    为什么以前 agentic 下是空转：``self_check`` 只在固定工作流路径算过
    （``_execute_plan`` 里），而 agentic 走的是 ``_execute_plan_agentic`` ——
    界面上那个开关在默认模式下**永远没有任何效果**（用户"不知道有没有用"的直觉是对的）。

    ⚠️ 它**只做客观核对**，不参与任何决策：题目里的硬约束是"不许降灵活性"，
    所以这里既不读模型输出、也不改变终止原因 / 状态转换。
    """
    if not entry.options.get("self_check"):
        return
    plan = entry.conductor.task_state.plan
    if plan is None:
        return
    entry.self_check = check_execution_consistency(plan, entry.steps, entry.execution_results)


def _effective_options(
    req: PlanRequest, *, decomposed: bool, model_available: bool
) -> list[EffectiveOption]:
    """把「本次各选项到底生效了没有」如实算出来（卡 46b）。

    这是**纯汇报**，一行决策逻辑都不碰：agentic 的执行仍由模型逐轮主导
    （``deciders.py`` / ``agent_loop.py`` 在本卡**零改动**）。

    口径（与 ``PlanRequest`` 的字段描述一一对应）：

    - ``decomposed``：本次是否真的跑了分解器（``mode=workflow`` 或有密钥回退固定工作流）；
    - ``model_available``：本次是否有可用模型客户端。

    为什么要这个：四个底部开关里，``use_llm`` 与 ``advanced`` 在 agentic 下**完全不生效**，
    ``self_check`` 以前也只在固定工作流路径算 —— 用户"不知道有没有用"的直觉是对的。
    现在要么让它生效（``self_check`` 已补到 agentic），要么**如实说"本模式不支持"**。
    """
    agentic = req.mode == "agentic"
    options: list[EffectiveOption] = []

    options.append(
        EffectiveOption(
            option="council",
            label="评审会",
            value=req.council,
            applied=req.council,
            detail=(
                "规划前已召开评审会（多角色独立表态后收敛）"
                if req.council
                else "未开启"
            ),
        )
    )
    options.append(
        EffectiveOption(
            option="self_check",
            label="执行自检",
            value=req.self_check,
            applied=req.self_check,
            detail=(
                "执行收尾会追加一次「计划 ↔ 执行结果」一致性核对（两种模式都生效）"
                if req.self_check
                else "未开启"
            ),
        )
    )
    options.append(
        EffectiveOption(
            option="sub_arch",
            label="副结构",
            value=req.sub_arch,
            applied=bool(req.sub_arch and agentic and model_available),
            detail=(
                "模型可提议新角色 / 新工具（需你批准后落盘）"
                if req.sub_arch and agentic and model_available
                else (
                    "已开启，但固定工作流不产生提议 → 本次无效果"
                    if req.sub_arch and not agentic
                    else (
                        "已开启，但没有可用密钥 → agentic 会回退固定工作流，不会产生提议"
                        if req.sub_arch
                        else "未开启"
                    )
                )
            ),
        )
    )
    options.append(
        EffectiveOption(
            option="use_llm",
            label="LLM 拆分",
            value=req.use_llm,
            applied=bool(req.use_llm and decomposed),
            detail=(
                "已用模型拆分需求（产出固定 DAG）"
                if req.use_llm and decomposed
                else (
                    "未开启"
                    if not req.use_llm
                    else "agentic 下规划阶段不分解（模型在执行阶段逐轮决策），本项对本次规划无影响"
                )
            ),
        )
    )
    options.append(
        EffectiveOption(
            option="advanced",
            label="详细度",
            value=req.advanced,
            applied=decomposed,
            detail=(
                f"已作用于分解提示词（{req.advanced}）"
                if decomposed
                else "**本模式不适用**：agentic 规划阶段不跑分解器，详细度无处可施"
            ),
        )
    )
    options.append(
        EffectiveOption(
            option="capabilities",
            label="技能（能力开关）",
            value=req.capabilities,
            applied=True,
            detail=(
                "已生效：该组之外的工具不会进入本任务的角色白名单，也不出现在能力目录里"
                if req.capabilities is not None
                else "未设置 → 全部能力启用"
            ),
        )
    )
    return options


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
        high_risk_actions=_high_risk_actions(
            steps, workspace=_resolve_workspace_dir(), count_in_workspace_writes=False
        ),
        self_check=self_check,
        council=council,
        title=getattr(entry_or_conductor, "title", "") or "",
        pending_approval=getattr(entry_or_conductor, "pending_approval", None),
        running=bool(getattr(entry_or_conductor, "running", False)),
        plan_text=getattr(entry_or_conductor, "plan_text", "") or "",
        parent_task_id=getattr(entry_or_conductor, "parent_task_id", None) or "",
        context_chars=len(getattr(entry_or_conductor, "context_note", "") or ""),
        # 卡 50：追加的修改要求也要能被回看（前端据此显示"你改过哪几句"）。
        follow_ups=list(getattr(entry_or_conductor, "follow_ups", None) or []),
        # 用户点过几次「继续再跑一轮」（成本归因用）。
        budget_extensions=int(getattr(entry_or_conductor, "budget_extensions", 0) or 0),
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

# 当前密钥**从哪里来**（卡 47）：runtime（本进程 POST 保存）/ env（启动读环境变量）/
# file（启动读落盘文件）/ none。只用于解释状态，不参与密钥派生。
api_key_source: str = "none"


def _iso_utc(ts: float | None) -> str | None:
    """把 Unix 时间戳转成 UTC ISO8601 字符串；None 透传。"""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def load_api_key_on_startup() -> str:
    """启动时按顺序装载密钥：**环境变量 → 落盘文件 → 不加载**（卡 47）。

    Returns:
        实际来源：``env`` / ``file`` / ``none``（写进模块级 ``api_key_source``）。

    修复的痛点（实测）：内存密钥在后端重启后**静默失效** ——
    `GET /settings/api-key` 变 ``configured:false``，下一次 ``/run`` 才报 ``E_MODEL``，
    用户完全看不出发生了什么（本次会话就踩了 8 次）。

    ⚠️ **读落盘文件不做开关判断**：用户上次明确点了"记住密钥"，重启后就该恢复；
    只有**写**才需要开关（见 ``api/secret_store.py`` 的说明）。
    日志只记来源，**永不记密钥内容**。
    """
    global api_key_source
    store = get_api_key_store()
    if store.is_configured:
        # 已经有人设过了（例如测试里预置），别覆盖。
        return api_key_source
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        try:
            store.set_key(validate_api_key(env_key))
        except InvalidApiKeyError as exc:
            logger.warning("DEEPSEEK_API_KEY 不合法（忽略）：%s", exc)
        else:
            api_key_source = "env"
            logger.info("启动装载密钥：来源=环境变量（内容不记录）")
            return api_key_source
    stored = load_secret()
    if stored is not None:
        try:
            store.set_key(validate_api_key(stored.api_key))
        except InvalidApiKeyError as exc:
            logger.warning("落盘密钥不合法（忽略）：%s", exc)
        else:
            api_key_source = "file"
            logger.info("启动装载密钥：来源=落盘文件 %s（内容不记录）", secret_path())
            return api_key_source
    api_key_source = "none"
    return api_key_source


def _key_status_payload(
    store: ApiKeyStore, *, rotated_from: str | None = None, source: str | None = None
) -> ApiKeyStatusResponse:
    """统一的密钥状态响应构造（绝不包含密钥明文）。

    ``source`` 不传时按**当前来源记录**推导（``api_key_source``）：POST 保存过 = ``runtime``；
    启动时从 env / 文件读入 = ``env`` / ``file``；都没有 = ``none``。
    """
    resolved = source if source is not None else (api_key_source or "none")
    if not store.is_configured:
        resolved = "none"
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
        source=resolved,
        # 「是否已落盘」= **文件在不在**，不看环境开关。
        # 为什么（实测 2026-10-11）：开关本身是进程级 env，重启就没了 —— 若跟着开关报，
        # 就会出现"明明是从落盘文件读回来的（source=file），却显示 persisted=false"这种自相矛盾。
        # 反向也成立：关闭落盘时文件会被删掉，所以"文件在"就等于"用户确实开过"。
        persisted=secret_path().exists(),
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
    global api_key_source
    api_key_source = "runtime"
    # 卡 47：只有在**显式开启**落盘时才写文件（默认关闭，保住"密钥不落盘"的既有约束）。
    if persist_enabled():
        save_secret(key, fingerprint=store.fingerprint() or "")
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
    """删除已保存的 API 密钥（幂等）。

    卡 47：**连落盘的那一份一起删**（否则"删了密钥"重启后又回来了，比不删更让人困惑）。
    """
    store = get_api_key_store()
    store.clear()
    delete_secret()
    global api_key_source
    api_key_source = "none"
    return _key_status_payload(store, source="none")


@router.post(
    "/settings/api-key/persist",
    response_model=ApiKeyStatusResponse,
    dependencies=[Depends(require_local_client)],
)
def set_api_key_persistence(
    persist: bool = Query(..., description="true = 把当前密钥落盘（重启后仍可用）；false = 删除落盘副本"),
) -> ApiKeyStatusResponse:
    """开启 / 关闭「密钥落盘」并立即对**当前密钥**生效（卡 47 的显式开关）。

    为什么是显式开关而不是默认落盘：``secrets.py`` 的既有安全约束是"只驻内存、不落盘"，
    并且有对抗性用例在守（全盘搜索无命中）。把它改成默认行为属于安全策略变更，
    所以这里让**用户自己选**：

    - ``persist=true`` + 已有密钥 → 落盘（0600、原子写、日志不记内容）；
    - ``persist=true`` 但**没有**密钥 → 400（不写空文件，也不假装成功）；
    - ``persist=false`` → 删除落盘副本，内存里的密钥保留（本进程内继续可用）。

    注意：这里**不接受密钥明文**（不重复 POST 的校验路径），只对"当前已存的密钥"起作用。
    """
    store = get_api_key_store()
    if persist:
        current = store.get()
        if not current:
            raise HTTPException(status_code=400, detail="当前没有已保存的密钥，无法落盘")
        if not save_secret(current, fingerprint=store.fingerprint() or ""):
            raise HTTPException(status_code=500, detail="密钥落盘失败，请检查磁盘权限")
        # 显式落盘 = 让"读文件"这条路下次开机生效。
        os.environ[PERSIST_ENV] = "1"
    else:
        delete_secret()
        os.environ.pop(PERSIST_ENV, None)
    return _key_status_payload(store)


# ── 项目（工作区）──
# 工作区由「项目」决定：选定目录 → 登记为项目 → 设为当前 → 后续 /workspace/* 均指向它。
# 目录选择由后端弹系统对话框（浏览器安全模型下前端拿不到绝对路径）；边界校验见 projects.py。


@router.delete(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    dependencies=[Depends(require_local_client)],
)
def delete_project(project_id: str) -> ProjectResponse:
    """从注册表里移除一个项目（**只删登记，不动磁盘**）。

    为什么需要（实测 2026-10-11）：用户把工作区目录删掉之后，注册表里会留下一堆
    「点进去就 400」的死条目，项目列表变得不可用 —— 而此前**没有任何删除入口**。

    约束：**当前工作区与内置默认工作区不可移除**（``current()`` 会回退到内置 default 项目，
    那等于把工作区静默换成产品源码根）。
    """
    store = get_project_store()
    if store.get(project_id) is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    try:
        project = store.remove(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProjectResponse(**asdict(project))


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
    """切换到已登记的项目（project_id="default" 即内置默认工作区）。

    **切换之前**必须校验目录仍存在：实测（2026-10-10）不校验时 ``/open`` 会返回 200
    把一个**已被删除**的项目置为当前，随后 ``current_workspace_dir()`` 静默回退到
    ``default_workspace_dir()``（= **产品自己的源码根**）—— 「打开一个死项目」于是
    变成「读写产品源码」，而同一个 ``/projects`` 响应里 ``current_project_id``
    （死项目）与 ``workspace_dir``（产品仓库）还互相矛盾。
    """
    store = get_project_store()
    existing = store.get(project_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")
    if not Path(existing.path).is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"项目目录已不存在：{existing.path}（请重新选择工作区）",
        )
    return ProjectResponse(**asdict(store.open(project_id)))


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


def _direct_messages(req: DirectChatRequest) -> list[dict[str, str]]:
    """组装转发给模型的消息：system（可选）+ 历史 + 本轮输入。

    **原样转发**：不做意图判定、不加提示词装饰 —— 直连的意义就是"前端只有大模型"。
    """
    messages: list[dict[str, str]] = []
    if req.system.strip():
        messages.append({"role": "system", "content": req.system})
    for turn in req.messages:
        role = str(turn.role or "").strip()
        content = str(turn.content or "")
        if role in ("user", "assistant", "system") and content:
            messages.append({"role": role, "content": content})
    tail = req.message.strip()
    if tail:
        messages.append({"role": "user", "content": tail})
    return messages


def _client_usage(client: Any) -> dict[str, Any]:
    """取客户端累计用量（取不到返回空表）—— 直连也要让用户看见花了多少。"""
    usage = getattr(client, "usage", None)
    to_dict = getattr(usage, "to_dict", None)
    if not callable(to_dict):
        return {}
    try:
        payload = to_dict()
    except Exception:  # noqa: BLE001 - 用量是旁路信息，取不到就算了
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


@router.post(
    "/llm/chat",
    response_model=DirectChatResponse,
    dependencies=[Depends(require_local_client)],
)
def direct_llm_chat(req: DirectChatRequest) -> DirectChatResponse:
    """**直连大模型**：把大模型与工作流拆开，前端只跟模型对话（「LLM 拆分」开关）。

    为什么单独开一个端点、而不是给 ``/chat`` 加开关：两条语义混在一个函数里迟早纠缠
    （``/chat`` 要判断"要不要干活"）。而本端点的验收标准恰恰是"**不产生任何任务、
    不写工作区**"（见 ``tests/test_api_direct_llm.py``）。

    安全性：直连会原样转发用户消息，属于"把模型暴露给前端"，因此**必须**挂本机标识闸门
    （``require_local_client``）—— 否则它就是一个对外裸奔的 LLM 代理。
    """
    client = get_llm_client(model=req.model, temperature=req.temperature)
    if client is None:
        raise HTTPException(
            status_code=409,
            detail=model_error(
                "未配置可用的模型密钥，直连大模型无法工作；请先在顶部保存 API 密钥",
                "api.routes",
                "direct-llm",
            ).to_dict(),
        )
    messages = _direct_messages(req)
    if not messages:
        raise HTTPException(status_code=400, detail="messages 与 message 不能同时为空")
    started = time.perf_counter()
    reply = client.chat(messages)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if not reply:
        # LLMClient 调用失败时降级为空串 —— 直连必须如实报错，不能"成功返回空回复"。
        raise HTTPException(
            status_code=502,
            detail=model_error(
                "模型没有返回内容（请检查密钥、网络或模型名）", "api.routes", "direct-llm"
            ).to_dict(),
        )
    return DirectChatResponse(
        reply=reply,
        model=req.model or "",
        duration_ms=elapsed_ms,
        usage=_client_usage(client),
    )


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
    # 续接：把「上文」落到任务上（只进提示词，不改变任务语义）。
    entry = store.get(task_id)
    if entry is not None:
        # **后端自己记得**：工作区级项目日志优先于前端传的上文（前者跨重启、覆盖多轮）；
        # 前端传的上文只作为"该工作区还没有日志"时的兜底。
        auto_context = _project_context_for_new_task(task_id)
        entry.context_note = _clean_context(auto_context or req.context)
        entry.parent_task_id = (req.parent_task_id or "").strip() or None
        # 工作区级放行记录：新任务**一开始就**带着它，所以「新任务从空集合开始」
        # 这个让用户每写一次文件都要重批的行为（卡 44）不再存在。
        seed_workspace_approvals(entry)
        return _to_task_response(entry, task_id)
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


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    dependencies=[Depends(require_local_client)],
)
def get_capabilities() -> CapabilitiesResponse:
    """**真实**能力清单（卡 45）：左栏「技能 / 插件」面板的唯一数据来源。

    为什么要有这个端点：此前那三个开关**没有任何后端出处**，点了不生效、也不持久化
    （前端自己写着"尚未接入后端，仅作展示"）。现在每一项都能追到这里，而这里的每一项
    又都能追到 ``tools/permissions.py`` 的权限矩阵与能力组映射 —— 不存在"没有出处的开关"。

    插件区**如实为空**（``plugins=[]`` + ``plugins_note``）：后端没有插件系统，
    界面必须显示说明而不是画三个假条目。

    挂 ``require_local_client``：能力清单会**枚举本机可用的工具与角色**
    （属于内部信息），与 ``/projects`` / ``/workspace/*`` 同档。
    """
    workspace = _resolve_workspace_dir()
    perms = get_default_role_perms()
    groups: list[CapabilityGroupView] = []
    for group in sorted(CAPABILITY_GROUPS):
        tools = sorted(CAPABILITY_GROUPS[group])
        gated = sorted(
            {
                tool
                for perm in perms.values()
                for tool in perm.high_risk_tools
                if tool in CAPABILITY_GROUPS[group]
            }
        )
        roles = sorted(
            role for role, perm in perms.items() if set(perm.allowed_tools) & set(tools)
        )
        groups.append(
            CapabilityGroupView(
                group=group,
                label=CAPABILITY_LABELS.get(group, group),
                note=CAPABILITY_NOTES.get(group, ""),
                tools=tools,
                high_risk_tools=gated,
                roles=roles,
            )
        )
    gated_all = sorted({tool for perm in perms.values() for tool in perm.high_risk_tools})
    return CapabilitiesResponse(
        workspace=str(workspace),
        default_capabilities=sorted(DEFAULT_CAPABILITIES),
        capability_groups=groups,
        roles={role: sorted(perm.allowed_tools) for role, perm in sorted(perms.items())},
        high_risk_tools=gated_all,
        plugins=[],
        plugins_note=(
            "后端尚未实现插件系统：这里的插件清单恒为空。"
            "「海报生成 / 网页抓取 / Git 集成」这类条目一律不再显示 —— 它们从来没有后端出处。"
        ),
    )


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
        # 任务级能力开关（卡 45）：None = 全部启用；执行阶段按它收窄角色白名单与能力目录。
        "capabilities": list(req.capabilities) if req.capabilities is not None else None,
        # 步数预算（用户可给大些一次跑完；跑满仍会停下来问要不要续）。
        "max_steps": req.max_steps,
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
        # 「先制定方案」：**agentic 也要有一份看得见的计划**，否则用户面对的是黑箱
        # （实测：整个执行期间对话区只有「收到，开始执行。」+ 一行折叠摘要）。
        # 它只用于展示，不冻结执行 —— 执行仍由模型逐轮决策（保持「规划去工作流化」）。
        entry.plan_text = _draft_plan_text(
            entry.requirement,
            llm_client
            or get_llm_client(
                model=req.model,
                temperature=req.temperature,
                role_brief_mode=req.role_brief,
            ),
            entry.context_note,
            workspace_file_snapshot(_resolve_workspace_dir()),
        )
        _record_usage(entry, plan_usage, mode=req.role_brief)
        status = conductor.task_state.status
        return DecomposeResponse(
            task_id=task_id,
            status=status.value if hasattr(status, "value") else str(status),
            mode=req.mode,
            council=entry.council,
            plan_text=entry.plan_text,
            effective_options=_effective_options(
                req, decomposed=False, model_available=llm_client is not None
            ),
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
        # 这一条路径**真的跑了分解器** → advanced / use_llm 本次确实生效。
        effective_options=_effective_options(
            req, decomposed=True, model_available=llm_client is not None
        ),
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


def _needs_approval(step: Any, workspace: Path | None = None) -> bool:
    """这一步在**执行前**是否需要用户放行（「到点暂停」的判定入口）。

    与 :func:`_pending_high_risk` / :func:`_high_risk_actions` 同源同口径，保证
    "计划里被标成待放行的"与"执行时真的停下来等的"是同一件事。
    """
    action = str(getattr(step, "action", "") or "")
    if not action:
        return False
    if is_high_risk(str(getattr(step, "assignee", "") or ""), action):
        return True
    if action not in WORKSPACE_SCOPED_WRITE_TOOLS:
        return False
    # 写在**工作区外**：沙箱会硬拒绝，绝不能悄悄执行；先按"待放行"挂起（不会执行）。
    path_str = workspace_path(getattr(step, "inputs", None))
    return not path_str or not inside_workspace(path_str, workspace)


def _pending_high_risk(entry: Any, workspace: Path | None = None) -> dict[str, Any] | None:
    """列出计划中**尚未被放行**的高风险步骤；全部已放行或无高风险时返回 None。

    「到点暂停」的判定依据：只要存在未放行的高风险动作，任务就挂起等用户决定，
    而不是让门卫把它判成不可重试的失败（E_PERMISSION）。

    **按实际风险判定**（卡 44）：工作区内的 ``file_write`` / ``file_edit`` 不再挂起
    （写产出文件是本职工作，边界由沙箱兜底）；确需放行的只剩
    ``file_delete`` / ``git_commit`` / ``rollback``。判定与门卫同口径
    （``inside_workspace``），因此这里与应用层的"要不要暂停"不会各说一套。

    Args:
        workspace: 判定用的工作区；None 用当前工作区（与实际执行时门卫用的是同一个）。
    """
    steps = getattr(entry, "steps", None) or {}
    risky = set(
        _high_risk_actions(
            steps,
            workspace=workspace if workspace is not None else _resolve_workspace_dir(),
            count_in_workspace_writes=False,
        )
    )
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


def seed_workspace_approvals(entry: Any) -> None:
    """把**工作区级**已放行的能力并进任务级授权集合（读侧容错，绝不抛）。

    卡 44 验收标准 4：放行记录按工作区持久化，重启后端 / 新任务都不必重复问同一类工具。
    这里只做"读回来"，写回在 :func:`grant_approvals`。
    """
    workspace = _resolve_workspace_dir()
    entry.approved_tools = set(getattr(entry, "approved_tools", None) or set()) | (
        read_workspace_approvals(workspace)
    )


def grant_approvals(entry: Any, tools: set[str]) -> None:
    """放行 ``tools``：写进任务级集合，**并落盘到工作区级记录**。

    落盘失败不影响本次放行（任务照常继续），只是"下次不再问"没记住。
    """
    if not tools:
        return
    entry.approved_tools = set(getattr(entry, "approved_tools", None) or set()) | set(tools)
    grant_workspace_approvals(_resolve_workspace_dir(), set(tools))


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
            capabilities=_task_capabilities(entry),
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
    _run_self_check(entry)

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


def _rounds_payload(rounds: Any, base_rounds: list[LoopRoundResult] | None = None) -> list:
    """把循环轮次转成 ``LoopRoundResult`` 列表（可叠加此前段落已完成的轮次）。"""
    payload: list[LoopRoundResult] = list(base_rounds or [])
    payload.extend(
        LoopRoundResult(
            round=item.round_index,
            decision=_loop_decision(item.decision),
            status=item.status,
            summary=item.summary,
            artifacts=list(item.artifacts),
            tokens=item.tokens,
        )
        for item in rounds
    )
    return payload


def _publish_live_progress(
    entry: Any,
    rounds: Any,
    context: LoopContext,
    budget: LoopBudget,
    *,
    base_rounds: list[LoopRoundResult] | None = None,
) -> None:
    """把**当前已完成**的轮次写进 entry，供前端轮询时边跑边渲染。

    为什么必须做（实测 2026-10-10，真实浏览器）：``steps`` / ``execution_results`` /
    ``loop_state`` 原先都是 ``run_agent_loop`` **返回之后**才写的，于是循环段内
    ``GET /tasks/{id}`` 什么都看不到 —— 前端即便轮询也无内容可画（实测某段 14 秒里
    工具卡恒为 2 张，直到该段结束才一口气冒出 5 张）。这里每完成一轮刷新一次。
    """
    entry.loop_state = LoopState(
        rounds=_rounds_payload(rounds, base_rounds),
        artifacts=list(context.artifacts),
        budget=LoopBudgetState(
            steps_used=budget.steps_used,
            max_steps=budget.max_steps,
            tokens_used=budget.tokens_used,
            max_tokens=budget.max_tokens,
            stagnant_rounds=budget.stagnant_rounds,
            max_stagnant_rounds=budget.max_stagnant_rounds,
        ),
    )


def _loop_state_from(
    outcome: LoopOutcome,
    budget: LoopBudget,
    context: LoopContext,
    *,
    base_rounds: list[LoopRoundResult] | None = None,
) -> LoopState:
    """构造循环上下文快照（累积历轮，供回看 / 审计 / ``/resume`` 续跑）。"""
    rounds = _rounds_payload(outcome.rounds, base_rounds)
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
    # 工作区**只取一次**：下面既用它喂提示词（工作区现状），也用它做"这一步要不要放行"的
    # 判定 —— 两处必须是同一个值，否则"按工作区判定"会与实际执行时的门卫各说一套。
    workspace_dir = _resolve_workspace_dir()
    capabilities = _task_capabilities(entry)
    executor, gatekeeper = build_step_executor(
        task_id,
        llm_client=llm_client,
        role_brief_mode=brief_mode,
        approved_tools=approved_tools,
        capabilities=capabilities,
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
        catalog=build_catalog(sub_arch=sub_arch, capabilities=capabilities),
        history=history,
        artifacts=list(previous.artifacts) if previous is not None else [],
        allow_propose=sub_arch,
        context_note=entry.context_note,
        workspace_note=workspace_file_snapshot(workspace_dir),
        # 卡 50：用户追加的修改要求 —— 与原始需求分开标注给模型。
        follow_ups=list(getattr(entry, "follow_ups", None) or []),
    )
    # 步数 / token 累计（兜住无限往复）；空转计数**不跨放行继承** —— 用户放行是一次
    # 新的干预，理应给一轮新机会，否则「挂起 + 放行后一次失败」会被凑成两轮空转而过早停下。
    #
    # 卡：步数硬顶**由用户决定**（``entry.options["max_steps"]``，默认 MAX_LOOP_STEPS）——
    # 跑满后循环停下问用户"要不要再来一轮"（``POST /tasks/{id}/continue``），
    # 而不是把"20 步没收敛"直接判死。
    budget_steps = int(entry.options.get("max_steps") or MAX_LOOP_STEPS)
    #
    # 卡 50：**追加修改要求时步数预算另起一轮**。理由与放行同源 —— 用户说"把 X 改成 Y"
    # 是一次新干预，如果沿用上一轮已经烧光的预算，追加的指令会**一次都不执行**就被
    # 预算闸门判停（用户看到的就是"改了但它没动"）。token 计量仍累计进任务用量（记账不丢）。
    if entry.follow_ups and previous_budget is not None:
        budget = LoopBudget(max_steps=budget_steps)
    else:
        budget = LoopBudget(
            max_steps=budget_steps,
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

        **按实际风险判定**（卡 44）：工作区内的写/改不再挂起（写产出文件是本职工作），
        真正需要放行的是删除 / 提交 / 回滚，以及**落到工作区外**的路径 —— 后者不放行
        也不执行，门卫会硬拒绝。
        """
        if _needs_approval(step, workspace_dir) and step.action not in approved_tools:
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
        # 每完成一轮就刷新一次 entry.loop_state：前端轮询才有东西可画（长任务不再
        # 「一整段没动静」）。终态仍由下面的 _loop_state_from 覆盖成完整快照。
        on_round=lambda rounds: _publish_live_progress(
            entry, rounds, context, budget, base_rounds=base_rounds
        ),
    )

    # 落步骤（``GET /tasks/{id}`` 的 steps，回看用）+ 执行结果（复用现有卡片渲染）。
    for item in outcome.rounds:
        step = to_step(item.decision, round_index=item.round_index)
        entry.steps[step.id] = step
    entry.execution_results = list(entry.execution_results) + to_execution_results(outcome)
    entry.gatekeeper_audit = gatekeeper.snapshot()
    entry.loop_state = _loop_state_from(outcome, budget, context, base_rounds=base_rounds)
    _sync_step_status(entry)  # steps 状态与执行结果对齐（否则永远是 pending）
    # 卡 46b：「执行自检」在 agentic 下也真的生效（以前只在固定工作流路径算过）。
    _run_self_check(entry)

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


@router.post("/tasks/{task_id}/instruct", response_model=TaskResponse)
def instruct_task(
    task_id: str,
    req: TaskInstructRequest,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """**在同一个任务上追加修改要求**（卡 50）——"不满意就改"的闭环入口。

    用户的痛点原话："做的不满意也没法改"。此前只能再发一句，而那是**新建任务**：
    模型从零开始，把工作区里已经做好的东西重做一遍（甚至覆盖掉）。

    这里的语义是：

    - **不新建任务**：指令挂到当前任务上，``task_id`` 不变；
    - 追加的指令与**原始需求分开**进提示词（``LoopContext.follow_ups``），
      并明确告诉模型"在已有产物上改、不要从零重做"；
    - 复用**已有的** agentic 循环与状态机，不加旁路：把任务摆回 EXECUTING 后
      调用 ``_execute_plan``（历史 / 产物 / 预算都从 ``loop_state`` 续，见
      ``_execute_plan_agentic`` 的续跑分支）；
    - 追加要求会**如实记账**在 ``TaskEntry.follow_ups``（供回看与提示词）。

    允许的来源状态：``verifying``（跑完了、还没交付）与 ``interrupted``
    （到点暂停 / 用户中断）—— 这两条都**已有**状态机转换可以回到 EXECUTING。
    ``failed``（已放弃）与 ``delivered``（已交付）是终态、契约里没有回去的转换，
    所以这里**如实拒绝并给下一步**，而不是偷偷改契约或硬塞状态。
    正在执行的任务也拒绝（409）：否则两个线程会同时改同一份状态。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    _assert_not_running(entry, task_id)

    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="追加的修改要求不能为空")

    conductor = entry.conductor
    status = conductor.task_state.status
    if status in (TaskStatus.DELIVERED, TaskStatus.FAILED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"任务已{'交付' if status is TaskStatus.DELIVERED else '放弃'}，"
                "状态机里没有从终态回到执行的转换；请另起一个新任务再改"
            ),
        )
    if status in (TaskStatus.RECEIVED, TaskStatus.PLANNING, TaskStatus.AWAITING_CONFIRM):
        raise HTTPException(
            status_code=409,
            detail="任务还没执行过，追加修改没有意义；请直接改需求或重新规划",
        )
    if status is TaskStatus.EXECUTING:
        raise HTTPException(
            status_code=409,
            detail="任务正在执行中，请等它结束或先中断后再追加修改",
        )

    # 记账：追加要求本身也是一条事实（回看 / 提示词都用它）。
    entry.follow_ups = list(entry.follow_ups) + [instruction]

    _reopen_for_execution(entry)
    if req.wait:
        _execute_plan(entry, task_id, entry.approved_tools)
        _record_task_project_log(entry)
    else:
        _start_background_execution(entry, task_id, entry.approved_tools)
    return _to_task_response(entry, task_id)


def _reopen_for_execution(entry: Any) -> None:
    """把一个**已停下**的任务摆回可执行态（``/instruct`` 与 ``/continue`` 共用）。

    **复用状态机既有转换，不加新事件、不改契约**：

    - ``verifying`` →（``verify_failed``）``reworking`` →（``rework_done``）``executing``
    - ``interrupted`` →（``resume``）``executing``
    - 已经是 ``executing``（非收敛停下就停在这个状态）→ 无需转换，原样返回

    ``verifying`` 那条路会碰 ``retry_count``：**先把计数清零** —— 用户主动要求"再来一轮"
    是**一次新干预**，不是"同一件工作又失败了"，否则几下之后就会撞上契约的 ``MAX_RETRY``
    被判成"修不好"→ abort（实测在 ``/instruct`` 上第 3 次就踩到）。
    """
    conductor = entry.conductor
    status = conductor.task_state.status
    try:
        if status is TaskStatus.VERIFYING:
            conductor.task_state.retry_count = 0
            conductor.handle_verify_failed()  # → reworking
            conductor.handle_rework_done()  # → executing
        elif status is TaskStatus.INTERRUPTED:
            conductor.handle_resume()  # → executing
        # executing：非收敛停下时状态就停在这里，不需要任何转换
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc


def _budget_stopped(entry: Any) -> bool:
    """任务是否是**预算用满**导致的非收敛停下（``/continue`` 的准入条件）。"""
    loop = getattr(entry, "loop_state", None)
    reason = str(getattr(loop, "stopped_reason", "") or "")
    return reason in (STOP_BUDGET_STEPS, STOP_BUDGET_TOKENS)


@router.post("/tasks/{task_id}/continue", response_model=TaskResponse)
def continue_task(
    task_id: str,
    req: TaskContinueRequest | None = None,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """**用户决定"再来一轮预算"** —— 预算用满后由用户拍板，而不是直接判死。

    背景（用户反馈）：20 轮预算跑完，界面只给「放弃任务」，等于**直接判死**。
    但"跑满 20 步还没收敛"不等于"这事做不成"——实测常见是差几步收尾。

    准入条件（**必须真的是预算停下**，否则 409）：``loop_state.stopped_reason`` 是
    ``budget_steps`` 或 ``budget_tokens``。空转 / 决策不可解析停下**不在**这里续 ——
    那两种是"模型没招了"，再多给步数也是烧钱，该改需求或放弃。

    行为：预算上浮（默认 +20，累计不超过 ``MAX_BUDGET_STEPS_LIMIT``）→ 摆回执行态 →
    复用已有的 agentic 循环继续跑（历史 / 产物 / 轮次都从 ``loop_state`` 续，
    所以模型看得见自己刚才做到哪）。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    _assert_not_running(entry, task_id)

    if not _budget_stopped(entry):
        loop = getattr(entry, "loop_state", None)
        reason = str(getattr(loop, "stopped_reason", "") or "（没有停下记录）")
        raise HTTPException(
            status_code=409,
            detail=(
                f"当前任务不是「预算用满」停下（stopped_reason={reason}），无需续跑；"
                "如果是空转 / 决策不可解析停下，请把需求说得更具体后重新发送，或放弃任务"
            ),
        )

    increment = req.additional_steps if req and req.additional_steps > 0 else BUDGET_STEP_INCREMENT
    current = int(entry.options.get("max_steps") or MAX_LOOP_STEPS)
    target = current + increment
    if target > MAX_BUDGET_STEPS_LIMIT:
        raise HTTPException(
            status_code=409,
            detail=(
                f"续跑已达上限（当前 {current} 步，上限 {MAX_BUDGET_STEPS_LIMIT} 步）；"
                "再跑下去成本不可控，请改需求重开任务，或直接放弃"
            ),
        )

    entry.options["max_steps"] = target
    entry.budget_extensions = int(getattr(entry, "budget_extensions", 0) or 0) + 1
    _reopen_for_execution(entry)

    if req is None or req.wait:
        _execute_plan(entry, task_id, entry.approved_tools)
        _record_task_project_log(entry)
    else:
        _start_background_execution(entry, task_id, entry.approved_tools)
    return _to_task_response(entry, task_id)


def _assert_not_running(entry: Any, task_id: str) -> None:
    """同一任务不允许并发起两次后台执行（否则两个线程会同时改同一份任务状态）。"""
    if getattr(entry, "running", False):
        raise HTTPException(
            status_code=409,
            detail=f"任务正在执行中，请等它结束或先中断：{task_id}",
        )


def _start_background_execution(entry: Any, task_id: str, approved_tools: set[str]) -> None:
    """把「跑循环」那段放到后台线程，**立刻**把控制权还给请求。

    为什么需要（实测 2026-10-10，真实浏览器）：``/run`` / ``/resume`` 原先同步阻塞到整段
    agentic 循环跑完才返回，期间前端**完全收不到任何东西** —— 实测首次执行冻结 12 秒、
    放行续跑冻结 22 秒，工具卡一张都不出现，直到结束才一次性冒出来（13 张卡 + 结论）。
    任务越长冻得越久，而循环本身是逐轮更新的（``GET /tasks/{id}`` 在请求进行中就能看到
    ``loop_state.rounds`` 增长）—— 所以只要不阻塞请求，前端就能轮询着边跑边渲染。
    """
    entry.running = True

    def _worker() -> None:
        try:
            _execute_plan(entry, task_id, approved_tools)
        except Exception:  # 后台线程：异常必须落日志，不能静默消失
            logger.exception("后台执行任务失败：%s", task_id)
        finally:
            entry.running = False
            # 后台路径同样要记进工作区项目日志（否则异步跑的任务"没留下记忆"）。
            _record_task_project_log(entry)

    threading.Thread(target=_worker, name=f"task-run-{task_id[:8]}", daemon=True).start()


@router.post("/tasks/{task_id}/run", response_model=TaskResponse)
def run_task(
    task_id: str,
    req: TaskApproveRequest | None = None,
    wait: bool = Query(
        True,
        description=(
            "true（默认）= 同步跑到本次循环结束再返回；"
            "false = 立刻返回（``running=true``），执行放到后台线程，前端据此边跑边渲染"
        ),
    ),
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
    _assert_not_running(entry, task_id)
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

    # 先把**工作区级**已放行的能力读回来：新任务不必重批同一类工具（卡 44 验收 4）。
    seed_workspace_approvals(entry)
    if req and req.approved_tools:
        grant_approvals(entry, set(req.approved_tools))

    if _pending_high_risk(entry) is not None:
        return _pause_for_high_risk(entry, task_id)

    entry.pending_approval = None
    if wait:
        _execute_plan(entry, task_id, entry.approved_tools)
        _record_task_project_log(entry)
    else:
        _start_background_execution(entry, task_id, entry.approved_tools)
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
    # 用户的显式放行**按工作区记住**（落盘），新任务 / 重启后不再重复问同一类工具。
    approved_tools: set[str] = (
        set(req.approved_tools) if req and req.approved_tools else set()
    )
    grant_approvals(entry, approved_tools)
    entry.pending_approval = None
    # 注意：这里只传本次显式授权的集合（严格契约），累积集合只用于 /run 与 /resume。
    _execute_plan(entry, task_id, approved_tools)
    _record_task_project_log(entry)
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

    同时**清零放行记录**（任务级 + 工作区级）：改计划 = 作废旧计划的授权，
    新计划里的高风险步骤必须重新逐工具放行（否则旧授权会「继承」到一份用户还没
    看过的计划上，静默执行写 / 删 / 提交）。

    ⚠️ 工作区级记录必须**一并撤销**（卡 44）：否则新计划一执行就会从工作区文件里
    把旧授权读回来，"清零"只在内存里做样子、实际上一轮就复活了。
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
    revoke_workspace_approvals(_resolve_workspace_dir())
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/interrupt", response_model=TaskResponse)
def interrupt_task(
    task_id: str,
    req: InterruptRequest,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户中断 → 状态转 INTERRUPTED。

    同时**清掉** ``pending_approval``：那份清单描述的是"被暂停前正等着你放行的那一步"，
    用户已经选择中断了，再挂着「待放行」只会让列表出现一个点不了的标记
    （实测 2026-10-11：``abort``/``interrupt`` 后任务已是终态、却仍带 ``has_pending_approval``）。
    清掉不影响「恢复」：``/resume`` 走的是 ``loop_state.pending_decision``，不依赖这份清单。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_interrupt(req.reason)  # EXECUTING → INTERRUPTED
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    entry.pending_approval = None
    _record_task_project_log(entry)
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/resume", response_model=TaskResponse)
def resume_task(
    task_id: str,
    req: TaskResumeRequest | None = None,
    wait: bool = Query(
        True,
        description=(
            "true（默认）= 同步跑到本次循环结束再返回；"
            "false = 立刻返回（``running=true``），执行放到后台线程，前端据此边跑边渲染"
        ),
    ),
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
    _assert_not_running(entry, task_id)
    paused_for_approval = entry.pending_approval is not None
    try:
        entry.conductor.handle_resume()  # INTERRUPTED → EXECUTING
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc

    # 放行按工作区记住（落盘）：同一工作区的新任务不再重复问同一类工具。
    seed_workspace_approvals(entry)
    if req and req.approved_tools:
        grant_approvals(entry, set(req.approved_tools))

    if not paused_for_approval:
        return _to_task_response(entry, task_id)

    # agentic 模式：挂起点由循环自己管理（``loop_state.pending_decision`` + ``PrefixedDecider``
    # 原样重放），不走「按 entry.steps 重新扫描高风险步骤」那套 —— agentic 的步骤是
    # 模型动态生成的，计划里本就没有一份可预先扫描的清单。
    if entry.options.get("mode", "agentic") == "agentic" and entry.loop_state is not None:
        entry.pending_approval = None
        if wait:
            _execute_plan(entry, task_id, entry.approved_tools)
            _record_task_project_log(entry)
        else:
            _start_background_execution(entry, task_id, entry.approved_tools)
        return _to_task_response(entry, task_id)

    if _pending_high_risk(entry) is not None:
        return _pause_for_high_risk(entry, task_id)

    entry.pending_approval = None
    if wait:
        _execute_plan(entry, task_id, entry.approved_tools)
        _record_task_project_log(entry)
    else:
        _start_background_execution(entry, task_id, entry.approved_tools)
    return _to_task_response(entry, task_id)


@router.post("/tasks/{task_id}/abort", response_model=TaskResponse)
def abort_task(
    task_id: str,
    store: InMemoryTaskStore = Depends(get_store),
) -> TaskResponse:
    """用户放弃 → 状态转 FAILED。

    **必须一并清掉 ``pending_approval``**：任务是终态了，那份「待放行」清单已经没有意义，
    留着会让任务列表挂一个永远点不了的标记（实测 2026-10-11，用户真实任务
    「你现在仔帮我这个agent写一个前端」就是 failed + 待放行）。
    """
    entry = store.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    try:
        entry.conductor.handle_abort()  # → FAILED
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    entry.pending_approval = None
    # 「放弃」也是一次发生过的项目活动，要记账（噪声过滤在读取侧做，见 build_project_context）。
    _record_task_project_log(entry)
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
