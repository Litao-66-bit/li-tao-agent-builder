"""API 请求/响应模型 —— Pydantic schema。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from agent_builder.api.agent_loop import MAX_BUDGET_STEPS_LIMIT
from agent_builder.api.secrets import MAX_TTL_S
from agent_builder.contracts.schemas import ChangeProposal
from agent_builder.tools.permissions import CAPABILITY_GROUPS

# 模型名长度上限（防超长输入被带进请求头 / 提示词）。
MAX_MODEL_CHARS = 64

# 高级执行选项：控制分解产出的详细度。
AdvancedLevel = Literal["default", "detailed", "concise"]


class TaskCreateRequest(BaseModel):
    """创建任务请求。"""

    requirement: str = Field(description="用户需求文本")
    task_id: str | None = Field(default=None, description="可选任务 ID，不传则自动生成")
    context: str = Field(
        default="",
        description=(
            "**上文**（可选）：本次需求之前、用户在同一个工作区里刚做完的事"
            "（上一任务的需求 / 结论 / 已产出文件）。用来让「先做后端 → 再做前端」这类"
            "多步需求接得上；**只进提示词，不改变任务语义**。超长会被截断并注明"
        ),
    )
    parent_task_id: str | None = Field(
        default=None,
        description="上一个任务的 ID（可选）：便于审计「这句话是接着谁说的」",
    )


class ChatTurn(BaseModel):
    """一轮对话（``/chat`` 的上下文）。"""

    role: Literal["user", "assistant"] = Field(default="user", description="发言方")
    content: str = Field(description="发言内容")


class ChatRequest(BaseModel):
    """对话请求 —— **先聊天，再决定要不要干活**。

    这是「普通消息不再无条件变成一份执行计划」的入口：模型判断这句话是闲聊 / 提问
    （``kind=chat``，直接回答）还是明确的执行诉求（``kind=task``，转任务链路）。
    """

    message: str = Field(description="用户这一句话")
    history: list[ChatTurn] = Field(
        default_factory=list, description="最近几轮对话（可选，用于上下文）"
    )
    model: str | None = Field(default=None, description="模型名覆盖")
    temperature: float | None = Field(
        default=None, ge=0.0, le=1.0, description="采样温度 0–1（前端「推理强度」滑块）"
    )

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        """空 / 纯空白消息直接拒绝（避免拿空串去问模型）。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message 不能为空")
        return cleaned


class ChatResponse(BaseModel):
    """对话响应。"""

    kind: Literal["chat", "task"] = Field(
        description="chat=直接回答即可；task=需要转入任务链路（建任务 → 执行）"
    )
    reply: str = Field(description="要对用户说的话（两种 kind 都有，可直接展示）")
    reason: str = Field(default="", description="一句话判定理由（审计与排查用）")


class DirectChatRequest(BaseModel):
    """**直连大模型**请求（「LLM 拆分」开启时使用）。

    语义：把大模型与它后面的工作流**拆开** —— 前端只跟模型对话。因此本请求
    **不涉及任何任务**：不建任务、不规划、不执行、不过门卫、不落盘、不写项目日志。
    与 ``/chat`` 的边界：``/chat`` 是"闲聊 vs 执行诉求"的**分流**（可能转任务链路）；
    直连**永远是对话**。
    """

    messages: list[ChatTurn] = Field(
        default_factory=list,
        description="多轮对话（role/content），按顺序**原样**转发给模型，不做任何提示词装饰",
    )
    message: str = Field(default="", description="本轮用户输入；会接在 messages 之后转发")
    system: str = Field(default="", description="可选 system 段（前端一般不传）")
    model: str | None = Field(default=None, description="模型名覆盖")
    temperature: float | None = Field(
        default=None, ge=0.0, le=1.0, description="采样温度 0–1"
    )


class DirectChatResponse(BaseModel):
    """直连大模型响应。"""

    reply: str = Field(description="模型回复原文（不做二次加工，直接展示）")
    model: str = Field(default="", description="请求指定的模型名（空 = 用服务端默认）")
    duration_ms: int = Field(default=0, description="本次调用的墙钟耗时（毫秒）")
    usage: dict[str, Any] = Field(
        default_factory=dict, description="本次调用的 token 用量（取不到则为空表）"
    )


class PlanRequest(BaseModel):
    """触发分解/规划请求。

    **每个字段都写明"什么时候才真正生效"**（卡 46b）—— 底部栏以前只画开关、
    不说条件，于是用户"不知道这些开关到底有没有用"（实测四个人里三个在默认模式下空转）。

    底部栏选项的生效条件：

    - ``model`` / ``temperature`` → LLM 客户端；**任何需要调用模型的环节都生效**，
      但 agentic 模式下**规划阶段不调模型**，所以它们实际作用于执行阶段；
    - ``advanced`` → 分解器提示词详细度（detailed 更细 / concise 更少）；
      ⚠️ **只在真正跑分解器时生效**，即 ``mode=workflow`` 或无密钥回退固定工作流时；
    - ``use_llm`` → 是否调用 LLM **拆分需求**；⚠️ 同样只在分解路径生效，
      agentic 有密钥时规划阶段**刻意不分解**（这就是"规划去工作流化"），
      所以它在默认模式下**不代表"任务会不会用模型"**（执行阶段永远由模型驱动）；
    - ``self_check`` → 执行收尾时追加一次「计划 ↔ 执行结果」一致性自检；**两种模式都生效**；
    - ``council`` → 规划前先召开评审会（多角色独立表态后收敛）；**两种模式都生效**（需密钥）；
    - ``sub_arch`` → 副结构门控：关闭时模型只能调用主架构角色，开启后才允许 ``kind=propose``；
      只在 agentic 且模型可用时有意义；
    - ``capabilities`` → 任务级能力开关；两种模式都生效。

    执行形态（P1 新增）：
    - ``mode`` → ``agentic``（默认，由模型逐轮决策选 agent）/ ``workflow``（固定工作流）；
      两者都随 ``/plan`` 存入任务选项，执行阶段（``/run`` / ``/approve`` / ``/resume``）读取。
      无可用密钥时 ``agentic`` 自动回退固定工作流（行为与今天一致）。

    每次 ``/plan`` 的响应里都会带 ``effective_options``：**本次实际生效的选项**清单
    （含"不适用"的原因），界面据此如实标注，而不是让用户猜。
    """

    use_llm: bool = Field(
        default=False,
        description=(
            "是否调用 LLM 拆分需求。**只在分解路径生效**（mode=workflow，或 agentic 无密钥回退时）；"
            "agentic 且有密钥时规划阶段不分解，本字段对规划无影响"
        ),
    )
    mode: Literal["agentic", "workflow"] = Field(
        default="agentic",
        description="执行形态：agentic（模型逐轮决策选 agent）| workflow（固定工作流）",
    )
    sub_arch: bool = Field(
        default=False,
        description=(
            "副结构开关：允许模型提议新角色 / 新工具（关闭时只可调用主架构角色）；"
            "**只在 agentic 且模型可用时有效**（固定工作流不产生提议）"
        ),
    )
    model: str | None = Field(
        default=None, description="模型名覆盖；不传则用 DEEPSEEK_MODEL（默认 deepseek-chat）"
    )
    temperature: float | None = Field(
        default=None, ge=0.0, le=1.0, description="采样温度 0–1（前端「推理强度」滑块）"
    )
    advanced: AdvancedLevel = Field(
        default="default",
        description=(
            "产出详细度：default | detailed | concise。"
            "⚠️ **只在跑分解器时生效**（mode=workflow 或无密钥回退）；agentic 规划阶段不适用"
        ),
    )
    self_check: bool = Field(
        default=False,
        description=(
            "是否在执行收尾时追加「计划 ↔ 执行结果」一致性自检（纯计算，不参与决策）；"
            "**agentic 与固定工作流都生效**"
        ),
    )
    council: bool = Field(
        default=False,
        description="是否在分解前先召开评审会（多角色独立表态后收敛）；两种模式都生效（需密钥）",
    )
    role_brief: Literal["off", "core", "full"] | None = Field(
        default=None,
        description=(
            "角色简报档位覆盖（A/B 对照用）：off / core / full；"
            "不传则用环境变量 AGENT_BUILDER_ROLE_BRIEF（默认 off）"
        ),
    )
    capabilities: list[str] | None = Field(
        default=None,
        description=(
            "**任务级能力开关**（卡 45）：列出本任务启用的能力组（见 GET /capabilities）。"
            "不传 / None = 全部启用（保持既有行为）；传了 = 只有列出的组可用。"
            "真实生效方式有两条：① 执行时把该组的工具从角色白名单里剔除（`build_step_executor`），"
            "② 规划/执行的能力目录里**不再出现**该组的动作（否则模型看得见、调了才失败）。"
            "组名必须在 CAPABILITY_GROUPS 里，否则 422。"
        ),
    )
    max_steps: int | None = Field(
        default=None,
        ge=1,
        le=MAX_BUDGET_STEPS_LIMIT,
        description=(
            "**步数预算**（agentic 循环的硬顶）；不传 = 默认 20 步。"
            "跑满后循环停下并**问用户**要不要再来一轮（`POST /tasks/{id}/continue`，默认 +20），"
            "而不是直接判死。传大些可以一次给足，但上限受 MAX_BUDGET_STEPS_LIMIT（200）约束"
        ),
    )

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        """模型名不得为空或含空白（防注入请求头 / 提示词）；超长直接拒绝。"""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("model 不能为空字符串")
        if len(cleaned) > MAX_MODEL_CHARS:
            raise ValueError(f"model 过长（最多 {MAX_MODEL_CHARS} 字符）")
        if any(char.isspace() for char in cleaned):
            raise ValueError("model 不能包含空白字符")
        return cleaned

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str] | None) -> list[str] | None:
        """能力组名必须真实存在 —— **不静默忽略**未知组名。

        为什么必须报错：界面上的开关如果打错一个字母，静默忽略会变成"开关看着关了、其实没关"，
        正是卡 45 要消灭的那类欺骗性 UI。
        """
        if value is None:
            return None
        unknown = sorted({str(item) for item in value} - set(CAPABILITY_GROUPS))
        if unknown:
            raise ValueError(
                f"未知能力组: {unknown}；可选: {sorted(CAPABILITY_GROUPS)}"
            )
        return sorted({str(item) for item in value})



class TaskInstructRequest(BaseModel):
    """在**同一个任务**上追加修改要求（卡 50）。

    语义边界：这不是"新建任务"，而是"对已有产物继续说一句" ——
    后端把指令挂到该任务的 ``follow_ups`` 上（与原始需求分开进提示词），
    再把任务摆回执行态复用**已有的** agentic 循环。

    ``wait=false`` 时立刻返回（``running=true``），执行放后台线程 —— 与 ``/run`` / ``/resume`` 同款。
    """

    instruction: str = Field(
        description=(
            "追加的修改要求（人话，如「把 result.txt 里的标题改成二级标题」）。"
            "会与**原始需求分开**标注给模型，避免它把这句当成新需求从头重做"
        )
    )
    wait: bool = Field(
        default=True,
        description="true = 同步跑到本次循环结束；false = 立刻返回，前端轮询着边跑边渲染",
    )


class TaskContinueRequest(BaseModel):
    """**用户决定"再来一轮预算"**（卡：预算用满后由用户拍板，而不是直接判死）。

    背景：步数预算是硬顶（默认 20 步）。跑满后循环停下（``stopped_reason=budget_steps``），
    此前界面**只给「放弃任务」**一个出口 —— 等于"跑满预算 = 直接判死"。
    但"20 步没收敛"不等于"这事做不成"（实测常见是差几步收尾），所以改成停下来问用户。

    ``additional_steps``：本次追加多少步（默认 ``BUDGET_STEP_INCREMENT`` = 20）。
    **只增不减**，且总量受 ``MAX_BUDGET_STEPS_LIMIT`` 约束。
    """

    additional_steps: int = Field(
        default=0,
        ge=0,
        le=1000,
        description=(
            "本次追加的步数；0 = 用默认增量（BUDGET_STEP_INCREMENT，20 步）。"
            "上限受 MAX_BUDGET_STEPS_LIMIT（200 步）约束"
        ),
    )
    wait: bool = Field(
        default=True,
        description=(
            "true = 同步跑到本次循环结束；false = 立刻返回（``running=true``），"
            "执行放后台线程，前端轮询着边跑边渲染（与 ``/run`` / ``/resume`` 同款）"
        ),
    )


class InterruptRequest(BaseModel):
    """中断请求。"""

    reason: str = Field(default="user_stop", description="中断原因")


class TaskApproveRequest(BaseModel):
    """批准执行请求。

    ``approved_tools`` 是**逐工具授权**通道：只有显式列出（且该工具对该角色属
    ``high_risk_tools``）的写/删除类步骤才会带 ``granted_by`` 通过审批门；未列出的
    高风险步骤会被门卫拒绝。**省略该字段 = 不授权任何高风险工具**（安全默认）。
    """

    approved_tools: list[str] | None = Field(
        default=None,
        description=(
            "本次批准授权的高风险工具名列表（如 [\"file_write\", \"file_delete\"]）；"
            "省略 = 不授权（高风险步骤将被门卫拒绝）"
        ),
    )


class TaskResumeRequest(BaseModel):
    """恢复请求（「到点暂停」后继续执行）。

    ``approved_tools`` 是本次放行的高风险工具名列表；服务端把它**累积**到任务的
    已授权集合后重新进入执行。省略 = 不放行新的工具（若仍有高风险步骤未授权，
    任务会再次挂起而不是失败）。
    """

    approved_tools: list[str] | None = Field(
        default=None,
        description="本次放行的高风险工具名列表；省略 = 不放行新工具（仍挂起等待）",
    )


class ApiKeyRequest(BaseModel):
    """设置/轮换 API 密钥请求。

    密钥明文只在本次请求体内出现，服务端仅存内存、不回显、不落盘。
    """

    api_key: str = Field(description="API 密钥明文（服务端仅内存保存，不回显）")
    ttl_s: int | None = Field(
        default=None,
        gt=0,
        le=MAX_TTL_S,
        description=f"生存期秒数（不传=不过期；上限 {MAX_TTL_S} 秒 = 30 天）",
    )


class ApiKeyStatusResponse(BaseModel):
    """API 密钥状态响应（绝不包含密钥明文）。

    只暴露派生自密钥的非敏感字段：是否已配置、掩码、非可逆指纹，
    以及 TTL 与轮换的元数据。
    """

    configured: bool = Field(description="是否已保存且未过期的密钥")
    masked: str | None = Field(default=None, description="掩码提示，如 sk-***；未配置为 null")
    fingerprint: str | None = Field(
        default=None,
        description="密钥非可逆指纹（sha256 前 8 位）；仅供同一把钥匙核对，未配置为 null",
    )
    expired: bool = Field(default=False, description="是否已到期失效")
    expires_at: str | None = Field(
        default=None, description="到期时间（UTC ISO8601）；不过期为 null"
    )
    remaining_s: int | None = Field(
        default=None, description="距到期剩余秒数；不过期或未配置为 null"
    )
    rotation_count: int = Field(default=0, description="当前密钥的累计轮换次数")
    rotated_at: str | None = Field(
        default=None, description="最近一次轮换时间（UTC ISO8601）；未轮换过为 null"
    )
    rotated_from_fingerprint: str | None = Field(
        default=None, description="本次轮换前的旧密钥指纹；仅轮换响应填充"
    )
    source: str = Field(
        default="none",
        description=(
            "当前密钥的**来源**（卡 47）：``runtime`` = 本次进程内经 POST 保存；"
            "``env`` = 启动时从 DEEPSEEK_API_KEY 读入；``file`` = 从落盘文件读入；"
            "``none`` = 没有可用密钥。用来解释「为什么重启后密钥没了」这类困惑"
        ),
    )
    persisted: bool = Field(
        default=False,
        description=(
            "是否已落盘（重启后仍可用）。**默认 false** —— 落盘是显式开启的可选项，"
            "因为它与「密钥只驻内存」的既有安全约束相反（见 api/secret_store.py 的说明）"
        ),
    )


# 状态响应允许出现的字段全集（全部为派生自密钥的非敏感值）。
# 回归测试与安全自检据此守住「响应体没有可直接读出明文的字段」。
API_KEY_STATUS_FIELDS: frozenset[str] = frozenset(
    {
        "configured",
        "masked",
        "fingerprint",
        "expired",
        "expires_at",
        "remaining_s",
        "rotation_count",
        "rotated_at",
        "rotated_from_fingerprint",
        "source",
        "persisted",
    }
)


class StepResult(BaseModel):
    """单个步骤的执行结果（用于回传给前端）。

    ``summary`` 是**默认展示**字段（一句话人话，如「已写入 a.md（1.2k 字）」）；
    ``thought`` 是 agentic 模式下这一轮的决策理由（workflow 模式为空串）；
    ``result`` / ``error`` 保留原始机器数据，仅供「查看原始数据」与审计。
    """

    step_id: str
    action: str
    status: str  # done | failed | skipped | pending_approval | pending
    executor: str
    summary: str = Field(
        default="",
        description="人读摘要（一句话人话）；前端默认展示它而不是原始 result/error",
    )
    thought: str = Field(
        default="",
        description="agentic 模式下这一轮的决策理由（人话）；workflow 模式为空串",
    )
    artifacts: list[str] = Field(
        default_factory=list,
        description="这一步真正产出的文件（相对工作区路径）；空列表 = 没产出，前端不显示「查看产物」",
    )
    result: str | None = None
    error: str | None = None
    retries: int = 0


class LoopDecision(BaseModel):
    """agentic 循环里一轮决策的可回传形态。"""

    kind: str = Field(description="agent | tool | propose | final | invalid")
    thought: str = Field(default="", description="一句话决策理由（人话，供审计与展示）")
    role: str | None = Field(default=None, description="承接该步骤的角色名")
    action: str | None = Field(default=None, description="要执行的动作名")
    inputs: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", description="更具体的决策理由")


class LoopRoundResult(BaseModel):
    """agentic 循环里一轮的结果。"""

    round: int = Field(description="第几轮（从 1 开始）")
    decision: LoopDecision
    status: str = Field(description="done | failed | skipped | pending_approval")
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    tokens: int = 0


class LoopBudgetState(BaseModel):
    """循环预算与用量快照（供前端展示「已用 / 上限」）。"""

    steps_used: int = 0
    max_steps: int = 0
    tokens_used: int = 0
    max_tokens: int = 0
    stagnant_rounds: int = 0
    max_stagnant_rounds: int = 0


class LoopState(BaseModel):
    """agentic 循环的上下文快照（供回看 / 审计 / ``/resume`` 续跑）。

    与 ``execution_results`` 的分工：``execution_results`` 是「步骤结果契约」
    （复用现有卡片渲染、一致性自检与回看）；``loop_state`` 只是**循环上下文**
    （续跑所需的待放行决策 + 预算 + 终止原因 + 归因）。
    """

    rounds: list[LoopRoundResult] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    budget: LoopBudgetState | None = Field(default=None, description="预算与用量快照")
    pending_decision: LoopDecision | None = Field(
        default=None,
        description="因高风险挂起的那一步；``/resume`` 从它继续（非 null 表示到点暂停）",
    )
    stopped_reason: str = Field(default="", description="终止原因：final / budget_steps / …")
    decided_by: str = Field(
        default="",
        description=(
            "实际拍板者：llm / llm-retry / static / scripted / resumed"
            "（resumed = 用户放行后由 PrefixedDecider 原样重放那一步，本轮无人重新决策）"
        ),
    )
    final_answer: str = Field(
        default="",
        description="模型以 kind=final 收尾时给出的结论（人话）；非空即「结论区」的正文",
    )


class ProposalView(BaseModel):
    """待批准提议的完整视图（批准卡直接渲染它）。

    ``proposal`` 是可审计的契约记录（``contracts.ChangeProposal``：状态 / 风险 /
    受影响文件 / diff 预览）；``accepts`` 与 ``rationale`` 是**做决定必需**、但契约里
    没有的字段（这个角色要承接哪些动作、为什么现有角色做不到），因此补在视图层，
    不改核心契约。
    """

    proposal: ChangeProposal
    accepts: list[str] = Field(default_factory=list, description="新角色需承接的动作")
    rationale: str = Field(default="", description="为什么现有角色做不到")


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
    steps: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "计划步骤明细（step_id → Step）。``plan`` 只含 order / parallel_groups，"
            "步骤本身的 action / inputs / title / description 在此；前端据此在刷新 / 换标签页"
            "后仍能完整回看计划（否则只有 step_id 占位）"
        ),
    )
    execution_results: list[StepResult] = Field(default_factory=list)
    gatekeeper_audit: list[dict[str, Any]] = Field(default_factory=list)
    high_risk_actions: list[str] = Field(
        default_factory=list,
        description="本计划中属高风险（需审批）的 action 去重列表（前端据此逐工具授权）",
    )
    self_check: dict[str, Any] | None = Field(
        default=None,
        description="副结构自检报告（{passed, checked, issues}）；未开启该选项时为 null",
    )
    council: dict[str, Any] | None = Field(
        default=None,
        description="评审会纪要（参会 / 一致 / 分歧 / 未决 / 建议决议）；未召开为 null",
    )
    title: str = Field(
        default="",
        description="任务标题（取自需求首行）；供前端任务列表展示与回看",
    )
    pending_approval: dict[str, Any] | None = Field(
        default=None,
        description=(
            "待用户放行的高风险步骤 ``{tools, steps}``；非 null 表示任务因高风险动作"
            "挂起（「到点暂停」），等待用户放行或放弃"
        ),
    )
    plan_text: str = Field(
        default="",
        description="可读的执行计划（agentic 下由 /plan 生成）；供刷新 / 回看重建计划区块",
    )
    parent_task_id: str = Field(
        default="",
        description="本任务接着哪个任务说的（续接归因）；无上文时为空串",
    )
    context_chars: int = Field(
        default=0,
        description=(
            "本次任务实际带上的「上文」字数（0 = 没带上）。它由**后端**从工作区级项目日志"
            "自动注入，前端据此如实告知用户「是否带上了本项目的历史」"
        ),
    )
    follow_ups: list[str] = Field(
        default_factory=list,
        description=(
            "**用户追加的修改要求**（卡 50，按时间顺序）：任务跑完之后，用户在**同一个任务**上"
            "继续说「把 X 改成 Y」的那些话。它们与原始需求分开进提示词"
            "（模型据此知道是在已有产物上改、不是从零重做），同时供界面回看"
        ),
    )
    budget_extensions: int = Field(
        default=0,
        description=(
            "用户点过几次「继续再跑一轮」（步数预算上浮）。用于回看与成本归因 —— "
            "续跑是**用户显式追加**的支出，不该混进「模型自己烧的」里"
        ),
    )
    running: bool = Field(
        default=False,
        description=(
            "任务是否正在**后台执行**（``POST /run|resume?wait=false``）。"
            "为 true 时 ``GET /tasks/{id}`` 的结果还在长，前端应继续轮询以边跑边渲染；"
            "同步执行路径下恒为 false"
        ),
    )
    loop_state: LoopState | None = Field(
        default=None,
        description=(
            "agentic 循环上下文（轮次记录 / 预算 / 待放行决策 / 终止原因 / 归因）；"
            "``mode=workflow`` 时为 null"
        ),
    )
    pending_proposal: ProposalView | None = Field(
        default=None,
        description=(
            "待用户批准的副结构提议（``kind=propose``）；非 null 表示任务因"
            "「等扩编决定」挂起，需调用 ``/proposal/approve`` 或 ``/proposal/reject``"
        ),
    )
    proposals: list[ChangeProposal] = Field(
        default_factory=list,
        description="提议历史（含已批准 / 已拒绝），供回看与审计",
    )
    conclusion: str = Field(
        default="",
        description=(
            "「结论区」的一句话人话结论（agentic 取模型 kind=final 的结论；固定工作流"
            "按执行结果如实汇总）。空串 = 没有可说的结论，前端不渲染结论块"
        ),
    )


class TaskSummary(BaseModel):
    """任务列表项（供前端「多任务并存、可回看」列表）。"""

    task_id: str
    title: str
    requirement: str
    status: str
    current_stage: str
    created_at: str
    updated_at: str
    has_pending_approval: bool = False
    stopped_reason: str = Field(
        default="",
        description=(
            "agentic 循环的终止原因；非收敛停下（budget_* / stagnant / invalid_decision）时"
            "列表项也要显示「已停下」，否则会与详情页的「已停下」自相矛盾"
        ),
    )


class CouncilRequest(BaseModel):
    """召开评审会请求。"""

    topic: str | None = Field(default=None, description="会议议题；不传则用任务原始需求")
    participants: list[str] | None = Field(
        default=None, description="参会角色名（可选）；不传则核心名单 + 关键词补位"
    )
    rounds: int = Field(
        default=1, ge=1, le=2, description="轮数：1 = 独立表态；2 = 追加一轮交叉质疑"
    )


class EffectiveOption(BaseModel):
    """一个底部栏选项在**本次**任务里的真实生效情况（卡 46b）。

    为什么要有：以前界面只画开关，不说条件，用户"不知道这些开关到底有没有用" ——
    实测四个开关里两个（加 advanced 共三个）在默认 agentic 模式下完全空转，
    界面却毫无说明。这里把事实回传给界面，由它**如实标注**（不适用的置灰 + 说明原因）。
    """

    option: str = Field(description="选项名（与 PlanRequest 字段同名，便于界面按名对上）")
    label: str = Field(description="中文名（界面上那个开关的文案）")
    applied: bool = Field(description="本次是否真的影响了什么")
    detail: str = Field(description="一句话说明：生效在哪 / 为什么不适用")
    value: Any = Field(default=None, description="本次请求里该选项的取值")


class DecomposeResponse(BaseModel):
    """分解结果响应。"""

    task_id: str
    status: str
    mode: Literal["agentic", "workflow"] = Field(
        default="agentic",
        description=(
            "本次生效的执行形态（回显请求）。``agentic`` 下**没有预先分解的 DAG** —— "
            "步骤由模型逐轮长出来，所以 ``steps`` 为空是正常结果（前端不该当成「暂无计划」）"
        ),
    )
    effective_options: list[EffectiveOption] = Field(
        default_factory=list,
        description=(
            "**本次实际生效的选项**清单（卡 46b）：每项含 applied / detail（含不适用原因）。"
            "界面据此如实标注，并在底部栏显示「本次实际生效的选项」"
        ),
    )
    steps: dict[str, Any] = Field(default_factory=dict)
    order: list[str] = Field(default_factory=list)
    parallel_groups: list[list[str]] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    high_risk_actions: list[str] = Field(
        default_factory=list,
        description=(
            "本计划中属高风险（写/删除/提交/回滚，需审批）的 action 去重列表；"
            "前端据此提示用户并选择要授权的工具"
        ),
    )
    council: dict[str, Any] | None = Field(
        default=None, description="评审会纪要；未开启评审会时为 null"
    )
    plan_text: str = Field(
        default="",
        description=(
            "**可读的执行计划**（3~6 条 + 一行「验证：」）。agentic 下没有预分解的 DAG，"
            "但用户仍需要看得见「打算怎么做」—— 否则整个任务是黑箱。它只用于展示，"
            "**不冻结执行**（执行仍由模型逐轮决策）。无密钥 / 生成失败时为空串"
        ),
    )


class FileNode(BaseModel):
    """文件树节点。"""

    name: str
    type: str  # "dir" | "file"
    path: str
    size: int | None = None  # 文件大小（字节），目录为 None
    children: list[FileNode] = Field(default_factory=list)


class FileContentResponse(BaseModel):
    """文件内容响应（只读预览）。

    只返回工作区相对路径，绝不返回服务端绝对路径。
    """

    path: str = Field(description="工作区相对路径")
    size: int = Field(description="文件大小（字节）")
    content: str = Field(description="UTF-8 文本内容")
    truncated: bool = Field(default=False, description="文件过大被截断（截断后前端禁止编辑）")


class FileWriteRequest(BaseModel):
    """写回文件内容请求（只允许覆盖已存在的工作区文件）。"""

    content: str = Field(description="要写入的 UTF-8 文本内容")


class ErrorResponse(BaseModel):
    """错误响应。"""

    error_code: int
    error_name: str
    message: str
    correlation_id: str


class ProjectResponse(BaseModel):
    """项目（工作区）信息。"""

    project_id: str = Field(description="项目 ID（default 为内置默认工作区）")
    name: str = Field(description="项目名（默认取目录名，重名自动追加序号）")
    path: str = Field(description="工作区目录的绝对路径")
    created_at: str = Field(description="登记时间（UTC ISO）")
    last_opened_at: str = Field(description="最近打开时间（UTC ISO）")


class ProjectListResponse(BaseModel):
    """项目列表 + 当前项目。"""

    current_project_id: str | None = Field(default=None, description="当前项目 ID")
    workspace_dir: str = Field(description="当前工作区目录的绝对路径")
    projects: list[ProjectResponse] = Field(default_factory=list, description="按最近打开倒序")


class ProjectCreateRequest(BaseModel):
    """登记并切换到指定目录作为工作区。"""

    path: str = Field(description="工作区目录的绝对路径")


class ProjectMkdirRequest(BaseModel):
    """在父目录下新建工作区文件夹。"""

    parent_path: str = Field(description="父目录绝对路径")
    name: str = Field(description="新文件夹名")


class DirectoryPickResponse(BaseModel):
    """系统文件夹选择结果；用户取消时 cancelled=True。"""

    cancelled: bool = Field(default=False, description="用户是否取消了选择")
    path: str | None = Field(default=None, description="选中的目录绝对路径")
    name: str | None = Field(default=None, description="目录名")


class HandoverOpenQuestion(BaseModel):
    """人的未决项（优先级高于机器未决）。"""

    text: str = Field(description="未决问题文本")
    source: str = Field(description="来源：pending_questions 或 self_check")


class HandoverMachineItem(BaseModel):
    """机器未决项（execution_results 中非 done 的步骤）。"""

    step_id: str = Field(description="步骤 ID")
    action: str = Field(description="步骤动作（英文工具名，供审计）")
    title: str = Field(default="", description="中文动作名（展示用；缺失才回退 action）")
    status: str = Field(description="步骤状态：failed / pending_approval / pending / skipped")
    detail: str = Field(default="", description="人话原因或结果摘要（不再是异常原文）")


class HandoverAckRequest(BaseModel):
    """记录交接提示的 ack（已查看 / 已忽略）。"""

    status: Literal["seen", "ignored"] = Field(description="ack 状态")


class HandoverResponse(BaseModel):
    """交接判定 + 结构化交接对象（规则式，不调 LLM）。"""

    task_id: str
    task_status: str
    should_suggest: bool
    strength: Literal["strong", "suggest", "none"]
    reasons: list[str] = Field(default_factory=list)
    generated_by: str = Field(default="rule", description="生成方式：rule（不调 LLM）")
    metric: dict[str, int] = Field(default_factory=dict)
    gate: dict[str, Any] = Field(default_factory=dict)
    ack: dict[str, Any] = Field(default_factory=dict)
    human_open_questions: list[HandoverOpenQuestion] = Field(default_factory=list)
    machine_open_items: list[HandoverMachineItem] = Field(default_factory=list)
    open_items_count: int = 0
    next_actions: list[str] = Field(default_factory=list)


class CapabilityGroupView(BaseModel):
    """一个**真实存在**的能力组（卡 45）。

    左栏「技能」面板的每一项都必须能追到这里 —— 不允许再有"没有出处的开关"。
    """

    group: str = Field(description="能力组标识（也是 PlanRequest.capabilities 里用的名字）")
    label: str = Field(description="中文名（界面直接用，不写死在前端）")
    note: str = Field(default="", description="一句话说明（做 tooltip）")
    tools: list[str] = Field(
        default_factory=list, description="该组包含的工具（**注册表里真实存在的**）"
    )
    high_risk_tools: list[str] = Field(
        default_factory=list, description="该组里需逐次放行的工具（删除 / 提交 / 回滚）"
    )
    roles: list[str] = Field(
        default_factory=list,
        description="该组工具被实授的角色（关掉该组后这些角色就调不到了）",
    )


class PluginItem(BaseModel):
    """插件项。

    ⚠️ **当前后端没有任何插件系统**，所以这里始终是空列表 ——
    界面必须如实显示"规划中"，不许再写死「海报生成 / 网页抓取 / Git 集成」这类假条目。
    """

    name: str
    note: str = ""


class CapabilitiesResponse(BaseModel):
    """`GET /capabilities`：**真实**能力清单（卡 45）。

    真源是 ``tools/permissions.py`` 的权限矩阵与能力组映射，**没有一行是写死的展示文案**：
    - ``capability_groups``：界面「技能」开关的每一项 = 一个能力组 → 一组真实工具；
    - ``roles``：角色 → 工具白名单（含高危项），供排查"这个角色到底能做什么"；
    - ``plugins``：始终为空 + ``plugins_note`` 说明（如实标注，不假装有）。
    """

    workspace: str = Field(description="当前工作区绝对路径（能力清单按它取）")
    default_capabilities: list[str] = Field(
        default_factory=list, description="默认启用的能力组（不传 capabilities 时等价于这一组）"
    )
    capability_groups: list[CapabilityGroupView] = Field(default_factory=list)
    roles: dict[str, list[str]] = Field(
        default_factory=dict, description="角色 → 工具白名单（含高危项）"
    )
    high_risk_tools: list[str] = Field(
        default_factory=list, description="全部需逐次放行的工具（跨角色并集）"
    )
    plugins: list[PluginItem] = Field(
        default_factory=list, description="插件清单；**当前恒为空**（后端未实现插件系统）"
    )
    plugins_note: str = Field(
        default="",
        description="插件区的如实说明（界面必须把它显示出来，而不是画假条目）",
    )


__all__ = [
    "ApiKeyRequest",
    "ApiKeyStatusResponse",
    "CapabilitiesResponse",
    "CapabilityGroupView",
    "CouncilRequest",
    "DecomposeResponse",
    "DirectoryPickResponse",
    "EffectiveOption",
    "ErrorResponse",
    "FileContentResponse",
    "FileNode",
    "FileWriteRequest",
    "HandoverAckRequest",
    "HandoverMachineItem",
    "HandoverOpenQuestion",
    "HandoverResponse",
    "InterruptRequest",
    "LoopBudgetState",
    "LoopDecision",
    "LoopRoundResult",
    "LoopState",
    "PlanRequest",
    "PluginItem",
    "ProjectCreateRequest",
    "ProjectListResponse",
    "ProjectMkdirRequest",
    "ProjectResponse",
    "ProposalView",
    "StepResult",
    "TaskApproveRequest",
    "TaskContinueRequest",
    "TaskCreateRequest",
    "TaskInstructRequest",
    "TaskResponse",
]
