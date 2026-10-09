"""决策器 —— agentic 循环「下一步做什么」的可替换来源。

- :class:`Decision` / :class:`LoopContext`：决策契约（模型每轮产出一个 Decision）。
- :class:`RouteDecider`：协议；生产实现 :class:`LLMRouteDecider`（LLM），
  测试实现 :class:`ScriptedDecider`。
- :func:`build_decision_prompt`：决策提示词模板（详细版）。
- :func:`parse_decision`：把模型返回的 JSON **归一化**成 Decision（容忍形状偏差）。
- :func:`validate_decision`：校验链（非法决策 → 返回人话原因，由循环决定回退）。
- :func:`static_fallback`：回退实现，等价于今天的 ``ACTION_ROLE_MAP``。

容错分层（详见 ``docs/agentic-loop-design.md`` §5）：
L1 提取（``LLMClient._extract_json`` 已有三级兜底）→ L2 归一化（本模块
``parse_decision``）→ L3 校验（``validate_decision``）→ L4 重试 1 次
（``LLMRouteDecider``）→ L5 回退静态映射 / 停下。

设计约束：本模块不 import llm（``llm_client`` 用 ``Any`` 注入，与 roles 层同款做法），
因此可完全离线测试。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_builder.api.orchestrator import ACTION_ROLE_MAP
from agent_builder.api.role_catalog import NON_STEP_ROLES, RoleSpec, catalog_prompt
from agent_builder.tools.outline import symbol_outline
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS
from agent_builder.tools.registry import registry

# 决策类型。
KIND_AGENT = "agent"      # 派给某个角色执行一个动作
KIND_TOOL = "tool"        # 直接执行工具（等价于 agent，保留给"无角色行为"的纯工具步）
KIND_PROPOSE = "propose"  # 提议新角色 / 新工具（仅副结构开启时可选）
KIND_FINAL = "final"      # 模型判定完成 → 收敛出结论

KINDS: frozenset[str] = frozenset({KIND_AGENT, KIND_TOOL, KIND_PROPOSE, KIND_FINAL})

# 决策器**没能**产出可用决策（JSON 解析失败 / 返回空 / 形状无法归一化）。
# 刻意不放进 KINDS：它不是一个合法决策，只用来让循环走"回退或停下"，
# 绝不能被误当成 final（那会把"没答出来"伪装成"任务已完成"）。
KIND_INVALID = "invalid"

# 不需要 role / action 的 kind。
_NO_TARGET_KINDS: frozenset[str] = frozenset({KIND_FINAL, KIND_PROPOSE})

# 决策提示词里的默认历史窗口（轮）。
DEFAULT_HISTORY_WINDOW = 6

# 期望的 JSON 结构（作为 schema_hint 传给 LLM 客户端）。
# 两种形态都给：逐步执行（agent）与收尾（final）—— 收尾必须带 ``answer``。
DECIDE_SCHEMA_HINT = (
    '{"thought": "一句话理由", "next": {"kind": "agent", '
    '"role": "角色名", "action": "动作名", "inputs": {}, "reason": "更具体的理由"}}；'
    '收尾时：{"thought": "为什么可以收尾", "next": {"kind": "final", '
    '"answer": "给用户看的结论"}}'
)

# 文本字段截断上限（防模型把长文塞进 thought 撑爆上下文）。
_MAX_TEXT_CHARS = 200

# 结论（``answer``）的截断上限 —— 比 thought 宽：它是给人看的成品，不是内部理由。
_MAX_ANSWER_CHARS = 600

# 提议（kind=propose）支持的落盘目标。
PROPOSAL_TARGETS: frozenset[str] = frozenset({"role", "tool"})

# 新角色 / 新工具的标识命名规则（snake_case，3–32 字符）。
PROPOSAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

# 提议里文本字段（用途 / 理由）的截断上限 —— 比 thought 宽，但仍设上限。
_MAX_PROPOSAL_TEXT = 400

# 提议自评风险档位（与 contracts.ChangeProposal.risk 同集合）。
PROPOSAL_RISKS: frozenset[str] = frozenset({"low", "mid", "high"})


@dataclass(slots=True)
class ProposalDraft:
    """模型提出的「扩编」草案（副结构开启时才允许）。

    只描述"需要什么能力"，**不自行落盘**：校验通过后由 ``/proposal/approve``
    生成脚手架并写入任务工作区，``permissions`` 授权等一律留人工并入。

    Attributes:
        target: ``role``（新角色）/ ``tool``（新工具；当前只接受提议、不自动落盘）。
        name: snake_case 标识（新角色名 / 新工具名）。
        mission: 一句话职责（新角色做什么）。
        accepts: 该角色需承接的动作（必须是已注册工具，或既有角色内行为，不得自造）。
        risk: 自评风险档位 ``low | mid | high``。
        rationale: 为什么现有角色做不到（校验与人工评审的依据）。
    """

    target: str = ""
    name: str = ""
    mission: str = ""
    accepts: tuple[str, ...] = ()
    risk: str = "low"
    rationale: str = ""



@dataclass(slots=True)
class Decision:
    """模型的一轮决策。

    Attributes:
        kind: ``agent`` / ``tool`` / ``propose`` / ``final``。
        thought: 一句话说明为什么这么选（审计与前端折叠展示用）—— **内部理由**。
        answer: ``kind=final`` 时**直接给用户看的结论**（做成了什么 / 关键结论是什么）。
            与 ``thought`` 分开是必须的：``thought`` 是决策理由（"可以收尾了"这类内心独白），
            拿它当结论等于把独白端给用户。缺失时循环回退用 ``thought``（不编造）。
        role: 承接该步骤的角色名（``agent`` / ``tool`` 时必填，须在目录内）。
        action: 要执行的动作（必须已注册为工具）。
        inputs: 工具参数。
        reason: 更具体的决策理由（可空）。
        proposal: ``kind=propose`` 时的扩编草案（其余 kind 为 None）。
    """

    kind: str
    thought: str = ""
    answer: str = ""
    role: str | None = None
    action: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    proposal: ProposalDraft | None = None


@dataclass(slots=True)
class LoopContext:
    """喂给决策器的上下文（只读快照 + 累积的历史）。

    Attributes:
        requirement: 用户原始需求。
        catalog: 当前可选角色目录（已按副结构开关过滤）。
        history: 历轮记录（``{round, thought, role, action, status, summary, observation}``）；
            ``observation`` 是该轮工具的**原始返回**（裁剪后），只进提示词、不进前端。
        artifacts: 至今产出的产物路径（去重由调用方保证）。
        allow_propose: 是否允许 ``kind=propose``（= 副结构开关）。
        budget_note: 预算进度的人话文案（如 ``3/12 步 · 8200/60000 tokens``），
            由 ``run_agent_loop`` 每轮刷新 —— 让模型知道"快没预算了，该收尾了"。
            放这里而不是让决策器持有 ``LoopBudget``，是为了避免 deciders ↔ agent_loop 循环 import。
        stall_note: **系统纠正**文案（如「你已连续 N 轮只读，而验证仍失败…」），由
            ``run_agent_loop`` 在检测到「连续只读 + 存在未解决失败」时刷新；空串 = 不提示。
    """

    requirement: str
    catalog: tuple[RoleSpec, ...]
    history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    allow_propose: bool = False
    budget_note: str = ""
    stall_note: str = ""


class RouteDecider(Protocol):
    """决策器协议：实现 ``decide`` 即可替换（LLM / 脚本 / 静态）。"""

    name: str

    def decide(self, context: LoopContext) -> Decision:
        """给定上下文，返回下一轮决策。"""
        ...  # pragma: no cover - 协议声明


def validate_decision(
    decision: Decision,
    *,
    catalog: tuple[RoleSpec, ...],
    allow_propose: bool = False,
) -> str | None:
    """校验链：``None`` = 通过；否则返回人话失败原因（由调用方决定是否回退）。

    校验顺序（fail fast，越靠前越省一次调用）：
    kind 合法 → ``propose`` 是否被允许 → 角色在目录内 → 角色可承接该 action →
    **仅对已注册工具**再查权限矩阵。

    最后一环区分两类 action（与今天的派发语义一致，见 ``role_catalog`` 的
    ``_accepts_by_role``）：
    - **真实工具**（``registry`` 里能查到）：必须过角色权限矩阵，避免"选对了角色
      却越权用工具"；
    - **角色内行为**（如 ``summarize`` / ``propose``）：不经工具门卫，故不查矩阵。
    """
    if decision.kind == KIND_INVALID:
        # 决策器自己承认没答出来：把它的原话（人话原因）直接作为失败原因上报。
        return decision.thought or "决策器未能产出可解析的决策"
    if decision.kind not in KINDS:
        return f"未知决策类型 {decision.kind!r}"
    if decision.kind == KIND_PROPOSE:
        if not allow_propose:
            return "副结构未开启，不允许提议新角色 / 新工具"
        return _validate_proposal(decision.proposal)
    if decision.kind in _NO_TARGET_KINDS:
        return None

    by_name = {spec.name: spec for spec in catalog}
    if not decision.role or decision.role not in by_name:
        return f"角色 {decision.role!r} 不在当前可选目录内"
    if not decision.action:
        return f"角色 {decision.role!r} 未给出要执行的 action"

    spec = by_name[decision.role]
    # accepts 为空 = 该角色当前没有可承接的动作（如看门人 / 记录员，
    # 它们的动作名未注册为工具，要等 P2 的「提议新工具」）。
    if not spec.accepts:
        return f"角色 {decision.role!r} 当前没有可承接的动作"
    if decision.action not in spec.accepts:
        return f"角色 {decision.role!r} 不接受动作 {decision.action!r}"

    if registry.get(decision.action) is None:
        return None  # 角色内行为：不经工具门卫，无需权限矩阵
    perm = DEFAULT_ROLE_PERMS.get(decision.role)
    if perm is None or not perm.is_allowed(decision.action):
        return f"角色 {decision.role!r} 未被授权使用 {decision.action!r}"
    return None


def static_fallback(
    decision: Decision, *, catalog: tuple[RoleSpec, ...]
) -> tuple[Decision | None, str]:
    """静态映射回退：拿模型想做的 action 去查今天的 ``ACTION_ROLE_MAP``。

    回退不受副结构门控影响（它就是"现状行为"），但结果角色仍须在当前目录内，
    否则执行链路里没有合法归属。

    Returns:
        ``(回退决策, 原因)``；无法回退时 ``(None, 原因)``。
    """
    action = decision.action
    if not action:
        return None, "没有 action 可供静态回退"
    role = ACTION_ROLE_MAP.get(action)
    if role is None:
        return None, f"动作 {action!r} 没有静态映射（回退失败）"
    if role not in {spec.name for spec in catalog}:
        return None, f"动作 {action!r} 的静态角色 {role!r} 不在当前可选目录内"
    reason = f"静态映射回退：{action} → {role}"
    return (
        Decision(
            kind=KIND_AGENT,
            role=role,
            action=action,
            inputs=dict(decision.inputs),
            thought=decision.thought,
            reason=reason,
        ),
        reason,
    )


def _text(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    """把任意值压成单行短文本（非字符串也接受；超长截断，防撑爆上下文）。"""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_decision(payload: Any) -> Decision:
    """把模型返回的 JSON 归一化成 :class:`Decision`（容错 L2，容忍形状偏差）。

    容忍：``next`` 缺失时取顶层字段（扁平写法）、``next`` 不是对象时退回顶层、
    ``inputs`` 非字典记空、文本字段非字符串则转换并截断、``kind`` 去空白转小写。

    **刻意不猜**的两处（宁可判 ``invalid`` 走回退，也不替模型做主）：
    - 只给了 ``role`` 没给 ``action``：缺 ``kind`` 时不推断成 ``agent``（不替它挑动作）；
    - 空返回 / 非字典：一律 ``KIND_INVALID`` —— **绝不推断成 ``final``**，
      否则"模型没答出来"会被伪装成"任务已完成"。

    Args:
        payload: 模型返回的原始对象（通常是 ``complete_json`` 的结果）。

    Returns:
        归一化后的决策；无法归一化时返回 ``kind=KIND_INVALID``（``thought`` 为人话原因）。
    """
    if not isinstance(payload, Mapping):
        return Decision(kind=KIND_INVALID, thought="模型返回的不是 JSON 对象")
    if not payload:
        return Decision(kind=KIND_INVALID, thought="模型返回了空的 JSON 对象")

    nested = payload.get("next")
    body: Mapping[str, Any] = nested if isinstance(nested, Mapping) else payload

    kind = _text(body.get("kind") or payload.get("kind")).lower()
    role = _text(body.get("role") or payload.get("role")) or None
    action = _text(body.get("action") or payload.get("action")) or None
    if not kind:
        if role and action:
            # 明确给了「谁 + 做什么」：推断成一步是安全的。
            kind = KIND_AGENT
        else:
            return Decision(
                kind=KIND_INVALID,
                thought="模型没给出 kind，也没有 role + action 可推断",
            )

    inputs = body.get("inputs", payload.get("inputs"))
    # 结论允许 ``final_answer`` 别名（模型常见写法），但**不拿 thought 兜底** ——
    # 那是决策理由，回退留给调用方（循环）决定，解析层不替它做语义猜测。
    answer = body.get("answer") or payload.get("answer")
    if not answer:
        answer = body.get("final_answer") or payload.get("final_answer")
    return Decision(
        kind=kind,
        thought=_text(payload.get("thought") or body.get("thought")),
        answer=_text(answer, _MAX_ANSWER_CHARS),
        role=role,
        action=action,
        inputs=dict(inputs) if isinstance(inputs, Mapping) else {},
        reason=_text(body.get("reason") or payload.get("reason")),
        proposal=_parse_proposal(body.get("proposal", payload.get("proposal"))),
    )


def _parse_proposal(raw: Any) -> ProposalDraft | None:
    """归一化提议草案（容忍字段缺失 / 类型偏差；缺失或非对象返回 None）。

    这里只做形状归一化，**语义校验交给 ``validate_decision``** —— 名字是否合法、
    动作是否真实存在等判断需要目录与注册表，不属于解析层。
    """
    if not isinstance(raw, Mapping):
        return None
    accepts = raw.get("accepts", ())
    if isinstance(accepts, str):
        accepts = [accepts]
    if not isinstance(accepts, (list, tuple, set, frozenset)):
        accepts = []
    accepts_items = [_text(item) for item in accepts]
    # 风险档位在解析层就收敛到合法集合：ChangeProposal 有强校验，模型给个
    # "critical" 会让落库直接抛错（500），这里统一回落 low 交给用户判断。
    risk = _text(raw.get("risk")).lower()
    return ProposalDraft(
        target=_text(raw.get("target")).lower(),
        name=_text(raw.get("name")).lower(),
        mission=_text(raw.get("mission"), _MAX_PROPOSAL_TEXT),
        accepts=tuple(item for item in accepts_items if item),
        risk=risk if risk in PROPOSAL_RISKS else "low",
        rationale=_text(raw.get("rationale"), _MAX_PROPOSAL_TEXT),
    )


def _validate_proposal(draft: ProposalDraft | None) -> str | None:
    """校验扩编草案（返回 None = 通过，否则返回人话拒绝原因）。

    这里守住的是「不得编造」：名字要合规且不与现役角色 / 工具重名、要承接的动作
    必须真实存在（已注册工具或既有角色内行为）。语义上无法自动判定的（这个角色
    是否真的有必要）留给用户在批准卡上决定。
    """
    if draft is None:
        return "提议缺少 proposal 字段（需要 target / name / mission / accepts）"
    if draft.target not in PROPOSAL_TARGETS:
        return f"未知提议类型 {draft.target!r}（可选：{' / '.join(sorted(PROPOSAL_TARGETS))}）"
    if draft.target == "tool":
        # 新工具要改 tools/registry.py 与权限矩阵，不在自动落盘范围 —— 如实拒绝，
        # 不给出"看起来批准了其实没生效"的假通道。
        return "本轮不支持自动落盘新工具（需改 tools/registry.py 与权限矩阵），请改提新角色"
    if not PROPOSAL_NAME_RE.match(draft.name):
        return f"角色名 {draft.name!r} 不合规（需 snake_case，3–32 字符，字母开头）"
    if draft.name in DEFAULT_ROLE_PERMS or draft.name in NON_STEP_ROLES:
        return f"角色 {draft.name!r} 已存在，不得重复提议"
    if registry.get(draft.name) is not None:
        return f"名字 {draft.name!r} 与既有工具冲突，请换一个"
    if not draft.mission:
        return "提议缺少用途说明（mission）"
    if not draft.accepts:
        return "新角色必须声明可承接的动作（accepts），否则无法被派发"
    for action in draft.accepts:
        if registry.get(action) is None and action not in ACTION_ROLE_MAP:
            return f"动作 {action!r} 不存在（既不是已注册工具，也不是既有角色内行为）"
    return None


# 回灌给模型的「观察结果」长度上限。工具返回（列目录 / 读文件）可能很长，
# 不裁剪会把每轮提示词撑爆。
#
# 2000 而非 800：800 的窗口**装不下一个源码文件的结构**，会让模型陷入无解的重读。
# 实测（run-6）：``research_agent/agent.py`` 共 4539 字，``class ResearchAgent``
# 在第 843 字、``__init__`` 签名在第 944 字 —— 全在 800 窗口之外，于是模型连续
# 5 次重读同一个文件（它自己的思考写着「需先看清 agent.py 中 ResearchAgent 的
# 真实签名」），每次都算成功、把空转计数反复清零，预算烧光也没能改代码。
MAX_OBSERVATION_CHARS = 2000


def _symbol_outline(raw: str, *, max_symbols: int = 40) -> str:
    """符号轮廓的薄壳 —— 实现在 :func:`agent_builder.tools.outline.symbol_outline`。

    搬到 tools 层的原因：写文件的工具（``file_write`` / ``file_edit``）也要在返回值里
    带上它，而 ``tools`` 不能反向 import ``api``。这里保留私有名只为不改动既有调用点。
    """
    return symbol_outline(raw, max_symbols=max_symbols)


def _clip_observation(raw: Any, *, limit: int = MAX_OBSERVATION_CHARS) -> str:
    """把上一步的**原始返回**裁成可入提示词的一段（空 / 非字符串 → 空串）。

    裁剪时**写明原文长度**：只说「已截断」时，模型不知道自己漏了多少、也不知道
    再读一次是否会有新内容 —— 实测它会因此反复读同一个文件。

    超长时**再附一份全文符号轮廓**（见 :func:`_symbol_outline`）：只多花几百字，
    却让模型立刻知道「哪个类在第几行、签名长什么样」，从而用 ``file_read`` 的
    ``start_line``/``end_line`` 精确取用，而不是把整份文件反复重读。
    """
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""
    if len(value) > limit:
        total = len(value)
        total_lines = value.count("\n") + 1
        shown_lines = value[:limit].count("\n") + 1
        outline = _symbol_outline(value)
        value = (
            value[:limit]
            # 说明里必须给**可操作的信息**：看的是哪几行、还剩多少行、下次一段读多大。
            # 实测（用户任务复现 run-2）：模型拿到「已截断」后直接要 start_line=65/end_line=306
            # （241 行 = 8.4k 字），再次超窗被截断，于是又陷入"读不全"的死循环。
            + f"…（原文 {total} 字 / 共 {total_lines} 行；此处只显示前 {limit} 字，"
            f"约第 1-{shown_lines} 行。要读后面的内容，请用 file_read 的 "
            f"start_line/end_line **分段**读取，一次不超过 50 行）"
            + outline
        )
    return value


def build_decision_prompt(
    context: LoopContext,
    *,
    history_window: int = DEFAULT_HISTORY_WINDOW,
    retry_note: str = "",
) -> str:
    """渲染决策提示词（详细版）。

    **稳定内容放最前**（目录 + 硬规则），**每轮变化的放后面**（需求 / 历史 / 预算）——
    这样能吃到 DeepSeek 的前缀缓存，直接省任务级 token 预算。

    Args:
        context: 决策上下文（需求 / 目录 / 历史 / 产物 / 预算）。
        history_window: 只带最近 N 轮历史（默认 6）；更早轮次通过"已产出"与
            预算进度间接体现。
        retry_note: 重试时追加的提示（如"上次输出无法解析"）。
    """
    lines: list[str] = [
        "【任务】你是执行编排决策者：每轮只决定「下一步由谁做什么」，拿到结果后再决定下一步。",
        "",
        catalog_prompt(sub_arch=context.allow_propose, specs=context.catalog),
        "",
        "【硬性规则】",
        "1. 只输出一个 JSON 对象：不要解释、不要 markdown 代码块。",
        (
            "2. 每轮只做一步；已足以回答需求时用 kind=final 收尾，**且必须给出 answer** —— "
            "它是直接写给用户看的结论（做成了什么 / 关键结论是什么），"
            "不要写「可以收尾」「需求已满足」这类内部独白。"
        ),
        "3. role 必须是【可用角色】里的名字；action 必须是该角色「可执行」列表里的项。",
        "4. 不得编造角色名、工具名，也不要塞 inputs 里没有依据的参数。",
        (
            "5. 高风险动作（写/删文件、提交、回滚）会被系统「到点暂停」等用户放行，"
            "你照常给出即可，不要因此绕开。"
        ),
        (
            "6. 上一轮失败的步骤不要原样重来：换 inputs 或换动作；"
            "实在做不下去就用 kind=final 并说明原因。"
        ),
        (
            "7. 分清「工作区原有的文件」和「你本任务的产出」：**只有本任务写过的、"
            "出现在【已产出】里的才算你的产出**。工作区里本来就有的文件（哪怕正好跟需求相关）"
            "一律不是你的成果。若【已产出】是「无」，就绝不要在 answer 里说"
            "「已构建 / 已生成 / 已产出 / 已完成某实现」，只能如实说你做了什么、还缺什么。"
            "**answer 里写到的文件名 / 运行命令必须真的在【已产出】里**："
            "不要给出你没写出来的入口（例如写「用法：python -m pkg.main」而 main.py 并不存在），"
            "也不要描述没跑过的验证结果。"
        ),
        (
            "8. 不要通读整个仓库：先想清楚产出落在**哪个具体文件**（新建就给完整路径），"
            "再按需读取；需求要的是可交付的代码/文件时，先把它写出来再验证，"
            "不要用「列出文件 / 读取文件 / 搜索代码」把仓库翻一遍就当成做完了"
            "（工作区里有大量既有代码时尤其如此，那些不是本次任务的产物）。"
        ),
        (
            "9. 写代码要**真做事**，不是搭空壳：不许用 `pass` / `return True` / "
            "`NotImplementedError` / 「TODO」/ 硬编码假结果充当实现；每个对外函数要有真实逻辑"
            "与 docstring。收尾前必须有**真实运行证据**（跑测试或跑命令并看到输出），"
            "不许只凭「看起来对」就说验证通过；确实没实现的部分如实说明，不要粉饰。"
            "**改了对外接口（类名 / 函数签名 / 返回值）就必须同步更新调用方，"
            "包括你自己写的测试**，否则测试必然全红"
            "（实机踩过：重写实现却没改测试，5 项测试全部失败、白烧好几轮）。"
        ),
        (
            "10. 工作区里**已存在但不是本任务产出**的实现 / 文件：**先跑一次 test_run 验证它**"
            "（一步就够）—— 能通过就**复用它并直接收尾**（在 answer 里说清「复用了既有 X」），"
            "不要重写；不能通过就**修它**（改同名文件要带 overwrite=true），"
            "不要另起一套并行实现。**不要因为「【已产出】为空」就反复读同一个文件**："
            "同一文件读完一次（必要时用 start_line/end_line 分段）就必须给出动作（写 / 改 / 跑）"
            "—— 实测：工作区已有实现时模型连读 7 步、一步没写，把预算烧在「复用还是重写」的犹豫上。"
        ),
    ]
    if context.allow_propose:
        # 只在门控开启时教这个能力 —— 关闭时教它等于鼓励模型去撞校验。
        lines.extend(
            [
                (
                    "11. 若现有角色确实做不到某件事，可用 kind=propose 提议**一个新角色**"
                    "（只接受 target=role）："
                ),
                (
                    '   {"thought": "...", "next": {"kind": "propose", "proposal": '
                    '{"target": "role", "name": "snake_case名", "mission": "一句话职责", '
                    '"accepts": ["已注册工具动作"], "risk": "low|mid|high", '
                    '"rationale": "为什么现有角色做不到"}}}'
                ),
                "   提议需用户批准后才会落盘；一轮只提一个，同一件事不要反复提。",
            ]
        )
    lines.extend(
        [
            "",
            "【输出结构】",
            DECIDE_SCHEMA_HINT,
            "",
            "【当前进度】",
            f"用户需求：{context.requirement}",
        ]
    )

    recent = context.history[-history_window:] if history_window > 0 else []
    if recent:
        lines.append("已完成（最早在前）：")
        for item in recent:
            lines.append(
                f"{item.get('round', '?')}. {item.get('role') or '-'} / "
                f"{item.get('action') or '-'} → {item.get('status') or '-'}："
                f"{item.get('summary') or '(无摘要)'}"
            )
            # 只给「摘要」等于让模型盲飞：实测它列完目录仍不知道列到了什么，
            # 只能继续猜参数。这里把工具的真实返回（裁剪后）一并给它。
            observation = _clip_observation(item.get("observation"))
            if observation:
                lines.append("   观察结果：")
                lines.extend(f"     {line}" for line in observation.splitlines())
        lines.append("（每轮的「观察结果」是工具的真实返回，据此决定下一步，不要重复已做过的动作。）")
    else:
        lines.append("已完成：无（这是第一步）")

    lines.append("已产出：" + ("、".join(context.artifacts) if context.artifacts else "无"))
    if context.budget_note:
        lines.append(f"已用预算：{context.budget_note}")
    if retry_note:
        lines.append("")
        lines.append(f"【注意】{retry_note}")
    if context.stall_note:
        # 系统级纠正：不靠模型自觉（实测它会连读 6 轮、思考全对却始终不动手）。
        lines.append("")
        lines.append(f"【系统纠正】{context.stall_note}")
    lines.append("请给出下一步决策。")
    return "\n".join(lines)


@dataclass(slots=True)
class LLMRouteDecider:
    """用 LLM 决策的 :class:`RouteDecider`（生产实现）。

    解析失败时按 ``retries`` 重试（默认 1 次，与 ``Router.max_retries`` 同尺度），
    仍失败则返回 ``KIND_INVALID`` —— 由循环走"回退静态映射或停下"，
    绝不自行编造决策。

    Args:
        llm_client: 需实现 ``complete_json(prompt, schema_hint) -> dict``
            （即 ``agent_builder.llm.client.LLMClient``）。用 ``Any`` 注入，
            避免 api → llm 的硬依赖，也让测试可注入假客户端。
        retries: 解析失败后的重试次数（默认 1）。
        history_window: 提示词里带的历史轮数（默认 6）。

    Attributes:
        calls: 累计提问次数（含重试），供计量与测试断言。
        retried: 最近一次 ``decide`` 是否发生过重试。
        last_error: 最近一次失败原因（人话）。
    """

    llm_client: Any
    retries: int = 1
    history_window: int = DEFAULT_HISTORY_WINDOW
    calls: int = 0
    retried: bool = False
    last_error: str = ""

    @property
    def name(self) -> str:
        """上报名：发生过重试时标注 ``llm-retry``，便于 A/B 归因（不是身份变化）。"""
        return "llm-retry" if self.retried else "llm"

    def decide(self, context: LoopContext) -> Decision:
        """问模型要下一步决策；解析失败按 ``retries`` 重试。"""
        attempts = max(int(self.retries), 0) + 1
        retry_note = ""
        decision = Decision(kind=KIND_INVALID, thought="决策器未被执行")
        for attempt in range(attempts):
            prompt = build_decision_prompt(
                context, history_window=self.history_window, retry_note=retry_note
            )
            self.last_error = ""  # 每次尝试给"最近一次失败原因"一个干净起点
            decision = parse_decision(self._ask(prompt))
            self.retried = attempt > 0
            if decision.kind != KIND_INVALID:
                return decision
            # ``_ask`` 已记下调用层原因（如客户端抛错）时不要被解析层原因覆盖。
            if not self.last_error:
                self.last_error = decision.thought
            # 重试提示要点名**最常见的两种成因**：实测（用户任务 7deadd5a）一轮已经
            # 验证通过、却因为收尾那轮输出无法解析而整轮作废（stopped=invalid_decision）。
            # ① 把结论写成了 Markdown/散文；② answer 太长被输出上限截断 → JSON 破损。
            retry_note = (
                "上次输出无法解析。常见原因有两种："
                "①把结论写成了 Markdown / 散文；②answer 太长被截断导致 JSON 不完整。"
                "请**只输出一个 JSON 对象**，不要任何解释、不要代码块围栏；"
                "收尾（kind=final）时 answer 控制在 200 字以内。"
            )
        return decision

    def _ask(self, prompt: str) -> Any:
        """问一次模型；注入的客户端抛错时降级为空结果（不让循环崩）。"""
        self.calls += 1
        try:
            return self.llm_client.complete_json(prompt, schema_hint=DECIDE_SCHEMA_HINT)
        except Exception as exc:  # noqa: BLE001  注入实现可能抛错，此处兜底降级
            self.last_error = f"决策调用失败：{type(exc).__name__}"
            return {}


@dataclass(slots=True)
class PrefixedDecider:
    """先按预置决策重放，再委托给后续决策器（``/resume`` 从挂起那步继续用）。

    用途：高风险「到点暂停」时把待放行决策存进 ``loop_state.pending_decision``，
    用户放行后**照着原样重放那一步**，再交回 LLM 继续决策 —— 这样"用户批准的是哪一步"
    与"实际执行的是哪一步"严格一致，不会因为模型重新决策而跑偏。

    Attributes:
        prelude: 待重放的决策（用尽后委托 ``decider``）。
        decider: 后续决策器（通常是 :class:`LLMRouteDecider`）。
        name: 上报名。
    """

    prelude: list[Decision]
    decider: RouteDecider
    name: str = "resumed"

    def decide(self, context: LoopContext) -> Decision:
        if self.prelude:
            return self.prelude.pop(0)
        return self.decider.decide(context)


@dataclass(slots=True)
class ScriptedDecider:
    """按脚本吐决策的假决策器 —— **仅测试 / 评估使用**（生产不实例化）。

    脚本用尽后固定返回 ``kind="final"``，避免测试脚本写不全导致死循环。
    """

    script: list[Decision]
    name: str = "scripted"
    calls: int = 0

    def decide(self, context: LoopContext) -> Decision:
        if self.calls >= len(self.script):
            return Decision(kind=KIND_FINAL, thought="脚本已用尽")
        decision = self.script[self.calls]
        self.calls += 1
        return decision


__all__ = [
    "DECIDE_SCHEMA_HINT",
    "DEFAULT_HISTORY_WINDOW",
    "KINDS",
    "KIND_AGENT",
    "KIND_FINAL",
    "KIND_INVALID",
    "KIND_PROPOSE",
    "KIND_TOOL",
    "PROPOSAL_NAME_RE",
    "PROPOSAL_RISKS",
    "PROPOSAL_TARGETS",
    "Decision",
    "LLMRouteDecider",
    "LoopContext",
    "PrefixedDecider",
    "ProposalDraft",
    "RouteDecider",
    "ScriptedDecider",
    "build_decision_prompt",
    "parse_decision",
    "static_fallback",
    "validate_decision",
]
