"""agentic 控制循环 —— 由模型逐轮决策「下一个由谁做、做什么」。

与现状的区别：不再是「一次分解出完整 Plan → 确定性顺序执行」，而是每一轮
``决策 → 校验 → 执行 → 观察 → 再决策``，直到模型判定完成或触发终止条件。

设计约束：
- **纯逻辑 + 依赖注入**：决策器、执行回调、预算全部由调用方注入 → 单测用
  ``ScriptedDecider`` 即可完整覆盖，不碰网络、不碰真实工具。
- 不 import llm、不直接执行工具：本模块只把决策落成 ``Step``，执行交给 ``execute``
  回调（P1 传 ``orchestrator`` 的派发链路，权限 / 沙箱 / 审计原样生效）。
- 审批门复用现有语义：``execute`` 返回 ``pending_approval`` 时循环**立即停下**并
  保留 ``pending_decision``，等 ``/resume`` 放行后从该步继续（循环上下文可回灌）。
- P0 只提供骨架，**不接入路由**：``/plan`` / ``/run`` 行为零变化。
- 决策器 / 执行回调抛出的异常直接上抛，由调用方决定回退（P1 接静态回退）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.api.artifacts import artifacts_of
from agent_builder.api.deciders import (
    KIND_FINAL,
    KIND_PROPOSE,
    Decision,
    LoopContext,
    RouteDecider,
    _clip_observation,
    static_fallback,
    validate_decision,
)
from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Step
from agent_builder.narrate import action_title, describe_step, summarize_result

# ── 预算（与仓库既有阈值同尺度；用户已确认的档位）────────────────
# 20 步：agentic 不再预分解 DAG（规划阶段去工作流化），所以「写代码 → 运行 →
# 排错」这条主链路要自己走完。实测 12 步会在「已建包 + 已写实现 + 已写测试 +
# 跑挂两次 + 正在按报错对齐接口」时被硬切（差一步收尾），故放宽到 20。
MAX_LOOP_STEPS = 20
# 60k tokens：交接提示门控阈值 12000 tokens 的 5 倍，作为"该收手"的硬顶。
MAX_LOOP_TOKENS = 60_000
# 连续 2 轮"没有进展" → 判空转。「有进展」= 成功**且**不是紧接着重复同一个动作
# （见 run_agent_loop 的 repeated：重复取回同一份东西不算新信息）。
MAX_STAGNANT_ROUNDS = 2
# 同一个「动作 + 参数」反复失败到第 N 次 → 判空转（复用 STOP_STAGNANT）。
#
# 只看"上一轮是否成功"不够：实测模型在一个失败的 file_read 之间夹了一次成功的
# file_list，就把空转计数清零 —— 同一路径连撞 5 次，直到预算耗尽也没换做法。
MAX_REPEAT_FAILURES = 2

# 终止原因（LoopOutcome.stopped_reason）。
STOP_FINAL = "final"
STOP_BUDGET_STEPS = "budget_steps"
STOP_BUDGET_TOKENS = "budget_tokens"
STOP_STAGNANT = "stagnant"
STOP_PENDING_APPROVAL = "pending_approval"
STOP_INVALID_DECISION = "invalid_decision"
# 用户中断：状态机把任务翻成 interrupted 后，循环在轮次边界观察到并退出。
STOP_INTERRUPTED = "interrupted"
# 模型提议扩编（kind=propose）：P0 只把提议**浮出水面**并停下，
# 批准 → 落盘 → 注册的闭环在 P2 接；绝不把提议当成一个可执行步骤跑掉。
STOP_PROPOSED = "proposed"

# 动态步骤 id 前缀（写进 TaskEntry.steps → 复用审批门 / 前端回看 / 用量计量）。
STEP_ID_PREFIX = "loop"


@dataclass(slots=True)
class LoopBudget:
    """循环预算与已用量（调用方可跨 ``/resume`` 保留同一个实例）。"""

    max_steps: int = MAX_LOOP_STEPS
    max_tokens: int = MAX_LOOP_TOKENS
    max_stagnant_rounds: int = MAX_STAGNANT_ROUNDS
    steps_used: int = 0
    tokens_used: int = 0
    stagnant_rounds: int = 0

    def stop_reason(self) -> str | None:
        """按当前用量判断是否该终止；None = 继续。"""
        if self.steps_used >= self.max_steps:
            return STOP_BUDGET_STEPS
        if self.tokens_used >= self.max_tokens:
            return STOP_BUDGET_TOKENS
        if self.stagnant_rounds >= self.max_stagnant_rounds:
            return STOP_STAGNANT
        return None

    def record(self, *, tokens: int, progressed: bool) -> None:
        """记一轮用量：步数 +1、token 累加；无进展则空转计数 +1，否则清零。"""
        self.steps_used += 1
        self.tokens_used += max(tokens, 0)
        self.stagnant_rounds = 0 if progressed else self.stagnant_rounds + 1


@dataclass(slots=True)
class StepOutcome:
    """一次步骤执行的产出 —— 由 ``execute`` 回调返回（便于注入与测试）。

    Attributes:
        status: ``done`` / ``failed`` / ``skipped`` / ``pending_approval``。
        summary: 一句话人话摘要（沿用 ``narrate.summarize_result`` 的口径）。
        error: 失败原因（人话）；成功为空串。
        artifacts: 本轮产出的产物路径（判定"是否有进展"的依据之一）。
        tokens: 本轮消耗的 token 数。
        observation: 本轮工具的**原始返回**（供循环回灌给模型；只进提示词、不进前端）。
            角色派发路径只把结果吃进自己的结果对象，没有它模型就无从判断下一步。
    """

    status: str
    summary: str = ""
    error: str = ""
    artifacts: list[str] = field(default_factory=list)
    tokens: int = 0
    observation: str = ""

    @property
    def progressed(self) -> bool:
        """是否算"有进展"：成功，或产出了新产物。"""
        return self.status == "done" or bool(self.artifacts)


@dataclass(slots=True)
class LoopRound:
    """一轮记录（审计 / 前端折叠展示用）。"""

    round_index: int
    decision: Decision
    status: str
    summary: str
    artifacts: list[str]
    tokens: int
    error: str = ""
    observation: str = ""


@dataclass(slots=True)
class LoopOutcome:
    """循环结果。

    Attributes:
        rounds: 历轮记录（按时间序）。
        stopped_reason: 终止原因（见 ``STOP_*``）。
        decided_by: 实际拍板的决策器名（发生回退时为回退决策器名）。
        pending_decision: 因高风险挂起的那一步（``/resume`` 续跑用）；否则 None。
        proposal: 模型提出的扩编提议（``kind=propose``）；否则 None。
        fallback_reasons: 发生过的回退原因（人话，供审计与前端提示）。
        final_answer: 模型以 ``kind=final`` 收尾时给出的**结论**（人话）。
            这是「结论区」的唯一来源。取 ``decision.answer``（专门写给用户看的结论），
            模型没给时才回退 ``decision.thought`` —— 那是决策理由（"可以收尾了"），
            直接端给用户就是内心独白。
    """

    rounds: list[LoopRound]
    stopped_reason: str
    decided_by: str
    pending_decision: Decision | None = None
    proposal: Decision | None = None
    fallback_reasons: list[str] = field(default_factory=list)
    final_answer: str = ""


def _attempt_key(decision: Decision) -> str:
    """「动作 + 参数」的规范化指纹 —— 用来识别反复无效的重试。"""
    try:
        inputs = json.dumps(decision.inputs or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        inputs = str(decision.inputs)
    return f"{decision.action or ''}|{inputs}"


def _failure_signature(outcome: StepOutcome) -> str:
    """本次失败的**症状指纹** —— 区分「同一件事又失败一次」与「换了新症状的失败」。

    实测（质量探针 run-1）：R3 的 ``test_run`` 因 ``ImportError``（通过 0 项）失败，
    模型据此重写了测试文件；R7 对同一 target 再跑 —— 这次是「通过 2 项、失败 4 项」的
    **新症状**，明明在收敛，却被旧口径当成"同动作同参数重复失败"直接判 ``stagnant``
    掐死。所以指纹里必须带上失败原文：症状变了就不算重复。

    ⚠️ 指纹必须覆盖**整条**失败原文，不能只取开头 —— 实测（用户任务复现 run-2）：
    ``test_run`` 的失败原文开头永远是「测试未通过：失败 6 项…；<测试名列表>」，
    而真正区分症状的异常信息（``Paper(year=...)`` vs ``no attribute 'survey'``）
    在**尾部**。只取前 200 字时，两种完全不同的病因被判成同一症状 → 又在误杀。
    （顺带把 pytest 的耗时抹掉，避免同一症状因计时不同被当成新症状。）
    """
    raw = outcome.error or outcome.summary or outcome.observation or ""
    text = " ".join(str(raw).split())
    # 抹掉易变部分：pytest 的 "in 0.02s" / 毫秒数，避免同一症状因计时不同被当成新症状。
    text = re.sub(r"\bin \d+(?:\.\d+)?s\b", "in <t>", text)
    return text


# 「只读动作」= 不改变工作区的动作。连着做这些而不动手，说明模型在"再确认一下"里空转。
READ_ONLY_ACTIONS: frozenset[str] = frozenset(
    {"file_read", "file_list", "code_search", "data_query"}
)
# 连续只读多少轮、且存在未解决的失败时，插入系统纠正。
STALL_READ_ROUNDS = 3


def _stall_note(rounds: list[LoopRound]) -> str:
    """连续只读 + 存在未解决失败 → 返回**硬性**纠正文案；否则空串。

    实测（用户任务 ``a20291c1``）：模型第 2 步拿到测试失败，接着**连读 6 轮**，思考里已经把
    根因说得完全正确（"需改实现而非改测试：测试传 topic/synonyms，实现是 keywords"），
    却始终不动手；第 9 步又跑同一个 ``test_run`` 拿到一模一样的失败 → 重复失败闸门掐停，
    9 步 0 产物。它不缺工具（``file_edit`` 就在能力目录里），缺的是「必须动手」这条硬约束 ——
    所以由系统直接纠正，而不是指望模型自觉。
    """
    trailing = 0
    for item in reversed(rounds):
        if item.status == "done" and item.decision.action in READ_ONLY_ACTIONS:
            trailing += 1
        else:
            break
    if trailing < STALL_READ_ROUNDS:
        return ""
    failed = next((item for item in reversed(rounds) if item.status == "failed"), None)
    if failed is None:
        return ""
    reason = " ".join(str(failed.error or failed.summary or "").split())[:160]
    return (
        f"你已连续 {trailing} 轮只做只读动作（读文件 / 列目录 / 搜索），"
        f"而验证仍然是**失败**的：{reason}。"
        f"下一步**必须**给出改动 —— 优先 `file_edit`（局部改，不必整份重写），"
        f"或 `file_write`（整份重写）；确实推不动就用 kind=final 说明原因。"
        f"**不要**再用 test_run 重复同一次验证，也不要再读同一个文件。"
    )


def to_step(decision: Decision, *, round_index: int) -> Step:
    """把决策落成可执行 ``Step``（写 ``assignee`` —— 既有钩子的正式启用点）。

    id 用 ``loop-NNN`` 前缀并写进 ``TaskEntry.steps``，从而**直接复用**现有的
    ``_pending_high_risk``（审批门）、``execution_results``（前端回看）与 ``/usage``。

    ``title`` / ``description`` 与固定工作流走**同一个** ``describe_step``：否则 agentic
    的步骤没有中文动作名，前端只能回退英文 ``action``（正是待消除的第 ② 类机器输出）。
    """
    action = decision.action or ""
    title, description = describe_step(action, decision.inputs)
    return Step(
        id=f"{STEP_ID_PREFIX}-{round_index:03d}",
        action=action,
        inputs=dict(decision.inputs),
        assignee=decision.role,
        title=title,
        description=description,
    )


def run_agent_loop(
    *,
    context: LoopContext,
    decider: RouteDecider,
    execute: Callable[[Step], StepOutcome],
    budget: LoopBudget | None = None,
    start_index: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> LoopOutcome:
    """跑一轮 agentic 循环。

    Args:
        context: 决策上下文（含需求、目录、历史；**会被就地更新**，跨 ``/resume``
            复用同一个实例即可续跑）。
        decider: 决策器（生产为 LLM，测试为脚本）。
        execute: 执行回调：接收 ``Step``，返回 ``StepOutcome``。异常直接上抛。
        budget: 预算与用量（跨 ``/resume`` 复用同一实例可累计用量）。
        start_index: 轮次起始偏移 —— ``/resume`` 续跑时传上次已完成的轮数，
            保证步骤 id（``loop-NNN``）与轮次编号不重号。
        should_stop: 可选回调，**每轮开始前**调用一次；返回 True 则立即返回
            ``stopped_reason=STOP_INTERRUPTED``。这是「用户中断」的落地方式：
            状态机已把任务翻成 ``interrupted``，循环在下一个轮次边界观察到并退出，
            不再决策 / 不再执行工具。默认 None（不检查）。

    Returns:
        :class:`LoopOutcome`：历轮记录 + 终止原因 + 待放行决策（若有）。
    """
    budget = budget if budget is not None else LoopBudget()
    rounds: list[LoopRound] = []
    fallback_reasons: list[str] = []
    pending_decision: Decision | None = None
    decided_by = decider.name
    # 「动作 + 参数」→ 已失败次数（识别反复无效的重试，见 MAX_REPEAT_FAILURES）。
    failed_attempts: dict[tuple[str, str], int] = {}

    while True:
        # 用户中断优先于预算：状态机已翻成 interrupted → 立刻退出，不再产出新步骤。
        if should_stop is not None and should_stop():
            return LoopOutcome(
                rounds=rounds,
                stopped_reason=STOP_INTERRUPTED,
                decided_by=decided_by,
                pending_decision=pending_decision,
                fallback_reasons=fallback_reasons,
            )

        early = budget.stop_reason()
        if early is not None:
            return LoopOutcome(
                rounds=rounds,
                stopped_reason=early,
                decided_by=decided_by,
                pending_decision=pending_decision,
                fallback_reasons=fallback_reasons,
            )

        # 刷新给人看的预算进度：让决策器知道"快没预算了，该收尾了"。
        context.budget_note = (
            f"{budget.steps_used}/{budget.max_steps} 步 · "
            f"{budget.tokens_used}/{budget.max_tokens} tokens"
        )
        # 刷新系统纠正：连续只读 + 还没解决的失败 → 硬性要求动手（不靠模型自觉）。
        context.stall_note = _stall_note(rounds)

        decision = decider.decide(context)
        problem = validate_decision(
            decision, catalog=context.catalog, allow_propose=context.allow_propose
        )
        if problem is not None:
            fallback, fallback_reason = static_fallback(decision, catalog=context.catalog)
            fallback_reasons.append(f"{problem}；{fallback_reason}")
            if fallback is None:
                return LoopOutcome(
                    rounds=rounds,
                    stopped_reason=STOP_INVALID_DECISION,
                    decided_by=decided_by,
                    pending_decision=pending_decision,
                    fallback_reasons=fallback_reasons,
                )
            decision = fallback
            decided_by = "static"

        if decision.kind == KIND_FINAL:
            # 「结论区」优先用 answer（专门写给用户看的结论）；模型没给才回退 thought。
            # 两者不分家的话，用户看到的是"可以收尾"这类内心独白。
            return LoopOutcome(
                rounds=rounds,
                stopped_reason=STOP_FINAL,
                decided_by=decided_by,
                pending_decision=pending_decision,
                fallback_reasons=fallback_reasons,
                final_answer=decision.answer or decision.thought,
            )

        if decision.kind == KIND_PROPOSE:
            return LoopOutcome(
                rounds=rounds,
                stopped_reason=STOP_PROPOSED,
                decided_by=decided_by,
                proposal=decision,
                fallback_reasons=fallback_reasons,
            )

        # 上一轮（跳过被审批门拦下、实际没执行的那轮）的「动作 + 参数」指纹，
        # 用于识别「紧接着重复同一个动作」。
        previous_key = next(
            (
                _attempt_key(item.decision)
                for item in reversed(rounds)
                if item.status != "pending_approval"
            ),
            "",
        )

        step = to_step(decision, round_index=start_index + len(rounds) + 1)
        outcome = execute(step)
        # 工具原始返回只留一份给提示词（裁剪后）；前端仍只看 summary。
        observation = _clip_observation(outcome.observation)
        rounds.append(
            LoopRound(
                round_index=start_index + len(rounds) + 1,
                decision=decision,
                status=outcome.status,
                summary=outcome.summary,
                artifacts=list(outcome.artifacts),
                tokens=outcome.tokens,
                error=outcome.error,
                observation=observation,
            )
        )
        context.history.append(
            {
                "round": start_index + len(rounds),
                "thought": decision.thought,
                "role": decision.role,
                "action": decision.action,
                "status": outcome.status,
                "summary": outcome.summary,
                "observation": observation,
            }
        )
        context.artifacts.extend(outcome.artifacts)

        # 挂起那轮不记预算：审批门拦下的是"还没执行"，既不算一步也不算一次空转。
        # 否则放行后续跑时，这一轮会被当成空转，凑满阈值而过早停下。
        if outcome.status == "pending_approval":
            return LoopOutcome(
                rounds=rounds,
                stopped_reason=STOP_PENDING_APPROVAL,
                decided_by=decided_by,
                pending_decision=decision,
                fallback_reasons=fallback_reasons,
            )

        # 紧接着重复「同一个动作 + 同一份参数」、又没产出新东西 = 这一轮只是把同一份
        # 东西又拿了一遍，**没有带来新信息**，不算进展。
        # 实测（run-6）：模型连续 5 次 file_read 同一个 4.5k 的 agent.py，每次都算
        # 「成功」→ 空转计数被反复清零、空转检测失效，14 步里 6 步白烧在重读上，
        # 明明已诊断出「实现与测试签名不匹配」却没轮次去改。
        # 失败了但**症状是新的**（例如 TypeError 变成 NameError）= 世界变了（模型刚改过代码），
        # 算进展 —— 否则"改一处 → 跑一次 → 再改下一处"这种正常修复循环会被空转计数掐死。
        # 实测（用户任务 02bd9686）：R6 成功改掉 render_json 的 TypeError，R9 跑出**新的**
        # NameError（它自己引入的），R10 想补 import 但 old_string 不匹配 —— 于是 R9+R10
        # 两轮"无产出"把整轮在第 10 步掐死，而预算还剩 10 步。
        symptom = _failure_signature(outcome) if outcome.status == "failed" else ""
        new_symptom = bool(symptom) and not any(
            signature == symptom for _, signature in failed_attempts
        )
        repeated = not outcome.artifacts and _attempt_key(decision) == previous_key
        budget.record(
            tokens=outcome.tokens,
            progressed=(outcome.progressed and not repeated) or new_symptom,
        )

        # 同一个「动作 + 参数」反复失败 = 明显没有进展，直接停下让用户接手。
        # 不能只看"连续无进展"：中间夹一个成功的动作就会把空转计数清零。
        if outcome.status == "failed":
            # 指纹 = 动作/参数 **+ 本次失败症状**：症状变了说明世界变了（模型改了代码），
            # 那不是"重复失败"，得给它下一轮（实测见 _failure_signature 的注释）。
            key = (_attempt_key(decision), symptom)
            failed_attempts[key] = failed_attempts.get(key, 0) + 1
            if failed_attempts[key] >= MAX_REPEAT_FAILURES:
                return LoopOutcome(
                    rounds=rounds,
                    stopped_reason=STOP_STAGNANT,
                    decided_by=decided_by,
                    pending_decision=pending_decision,
                    fallback_reasons=fallback_reasons,
                )


def run_step(step: Step, execute: Callable[[Step], Any]) -> StepOutcome:
    """把一次「角色派发 + 工具门卫」调用转成 :class:`StepOutcome`（异常转状态，不抛出）。

    与 ``Router._execute_step`` 同口径：权限不足 → ``pending_approval``（转发审批门）；
    ``AgentError`` / 其他异常 → ``failed``。

    **不重试**：重派语义留给决策方 —— agentic 模式下模型看到失败会自己换做法，
    而"换个同参重试"对确定性失败（未授权覆盖、参数非法）毫无意义。
    """
    try:
        result = execute(step)
    except PermissionError as exc:
        return StepOutcome(
            status="pending_approval",
            summary=f"等待你放行：{action_title(step.action)}",
            error=str(exc),
        )
    except AgentError as exc:
        reason = exc.info.message
        return StepOutcome(
            status="failed",
            summary=summarize_result(
                action=step.action, status="failed", error=reason, inputs=step.inputs
            ),
            error=reason,
            # 失败也要让模型看见真实输出：工具把 stdout/stderr 带在错误信息里
            # （如 sandbox_run 的「命令退出码 N」），截掉它模型就只能盲猜。
            observation=reason,
        )
    except Exception as exc:  # noqa: BLE001  执行层异常一律转失败，不让循环崩
        reason = f"{type(exc).__name__}: {exc}"
        return StepOutcome(
            status="failed",
            summary=summarize_result(
                action=step.action, status="failed", error=reason, inputs=step.inputs
            ),
            error=reason,
            observation=reason,
        )
    return StepOutcome(
        status="done",
        summary=summarize_result(
            action=step.action, status="done", result=result, inputs=step.inputs
        ),
        artifacts=artifacts_of(step, result),
    )


def to_execution_results(outcome: LoopOutcome) -> list[dict[str, Any]]:
    """把循环轮次转成 ``StepResult`` 形状的 dict 列表。

    写进 ``TaskEntry.execution_results`` 后，**现有前端卡片渲染、一致性自检、
    ``GET /tasks/{id}`` 回看全部零改动复用**（这是"复用现有工具卡"的落地方式）。
    """
    rows: list[dict[str, Any]] = []
    for item in outcome.rounds:
        rows.append(
            {
                "step_id": f"{STEP_ID_PREFIX}-{item.round_index:03d}",
                "action": item.decision.action or "",
                "status": item.status,
                "executor": item.decision.role or "",
                "summary": item.summary,
                "thought": item.decision.thought,
                "artifacts": list(item.artifacts),
                "result": None,
                "error": item.error or (item.summary if item.status == "failed" else None),
                "retries": 0,
            }
        )
    return rows


__all__ = [
    "MAX_LOOP_STEPS",
    "MAX_LOOP_TOKENS",
    "MAX_REPEAT_FAILURES",
    "MAX_STAGNANT_ROUNDS",
    "STEP_ID_PREFIX",
    "STOP_BUDGET_STEPS",
    "STOP_BUDGET_TOKENS",
    "STOP_FINAL",
    "STOP_INTERRUPTED",
    "STOP_INVALID_DECISION",
    "STOP_PENDING_APPROVAL",
    "STOP_PROPOSED",
    "STOP_STAGNANT",
    "LoopBudget",
    "LoopOutcome",
    "LoopRound",
    "StepOutcome",
    "run_agent_loop",
    "run_step",
    "to_execution_results",
    "to_step",
]
