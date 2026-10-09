"""评审会（council）—— 调动若干 Agent 角色就同一议题独立表态，收敛为结构化纪要。

流程（与已确认方案一致）：
1. 组会：核心名单 + 按议题关键词自动补位 + 强制反方（fact_checker / impact_analyzer）；
2. 表态：以「角色视角」调用 LLM 生成结构化意见（stance / content / evidence），
   逐条经 ``council_check_opinion`` 工具校验（结构 / 长度 / 注入清洗）；
3. 收敛：``council_build_minutes`` 工具（规则式）产出 一致 / 分歧 / 未决 / 建议决议骨架；
4. 决议：再用 LLM 生成一句「建议决议」，失败时回落到规则式结论；
5. 无可用 LLM 客户端时直接拒绝发起（不产出空壳纪要）。

边界说明：本模块属编排层（``api/``），可 import roles / llm / tools。参会角色以
「角色视角」生成表态，不驱动各角色自身的工具执行链（那是 ``run_plan`` 的职责）；
因此会议不产生文件写入等副作用。
"""

from __future__ import annotations

from typing import Any

from agent_builder.contracts.errors import AgentError, validation_error
from agent_builder.tools.impl.council_build_minutes import build_minutes
from agent_builder.tools.impl.council_check_opinion import check_opinion

# 核心参会名单（每次评审会的固定班底，含两名强制反方）。
CORE_PARTICIPANTS: tuple[str, ...] = (
    "proposer",
    "impact_analyzer",
    "fact_checker",
    "summarizer",
)

# 强制反方：必须在场，避免「一边倒」的会议。
DEVIL_ADVOCATES: tuple[str, ...] = ("fact_checker", "impact_analyzer")

# 议题关键词 → 自动补位的领域角色。
KEYWORD_ROLES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("代码", "重构", "接口", "函数", "报错", "bug", "code", "api"), "code_worker"),
    (("文档", "说明", "readme", "规格", "手册"), "doc_worker"),
    (("数据", "指标", "统计", "报表", "csv"), "data_analyst"),
    (("测试", "用例", "回归", "test"), "test_runner"),
    (("检索", "搜索", "来源", "资料", "调研"), "searcher"),
    (("审计", "日志", "基线", "监控"), "auditor"),
)

# 角色 → 一句话视角（用于构造「以该角色身份表态」的提示词）。
ROLE_BRIEFS: dict[str, str] = {
    "proposer": "方案生成者：定位根因，给出最小改动的实施方案",
    "impact_analyzer": "影响分析者：圈定波及范围、评估回归风险与成本",
    "fact_checker": "事实核验者：核对来源与关键数据，存疑不放行",
    "summarizer": "汇总者：把多方产出归纳为结论 / 依据 / 来源 / 未定项",
    "code_worker": "代码执行者：从实现可行性与改动面视角评估",
    "doc_worker": "文档工作者：从资料完整性与引用可靠性视角评估",
    "data_analyst": "数据分析者：从数据口径与可量化证据视角评估",
    "test_runner": "测试执行者：从可验证性与回归风险视角评估",
    "searcher": "检索者：从外部资料可获得性视角评估",
    "auditor": "审计者：从指标与基线偏差视角评估",
}

# 议题长度上限、参会角色上限（超出的补位角色会被截断）、轮数上限。
MAX_TOPIC_CHARS = 300
MAX_PARTICIPANTS = 8
MAX_ROUNDS = 2
# 提示词里对单条表态正文的约束（与工具侧上限保持一致口径）。
OPINION_CHARS_HINT = 300


def _validation(message: str, correlation_id: str) -> AgentError:
    """构造 E_VALIDATION（统一 source）。"""
    return validation_error(f"council: {message}", source="api.council", correlation_id=correlation_id)


def select_participants(topic: str, requested: list[str] | None = None) -> list[str]:
    """确定参会角色：核心名单 + 关键词补位（或用户指定）+ 强制反方，再去重截断。

    Args:
        topic: 会议议题（用于关键词补位）。
        requested: 可选，用户显式指定的参会角色；给出时不再做关键词补位。

    Returns:
        去重保序的角色名列表（未知角色名被忽略，强制反方始终在内）。
    """
    if requested:
        selected = [role for role in requested if role in ROLE_BRIEFS]
    else:
        selected = list(CORE_PARTICIPANTS)
        lowered = topic.lower()
        for keywords, role in KEYWORD_ROLES:
            if any(keyword in lowered for keyword in keywords):
                selected.append(role)
    for role in DEVIL_ADVOCATES:
        if role not in selected:
            selected.append(role)
    # 去重保序后截断：核心名单（含反方）排在前面，补位角色超出上限时被丢弃。
    return list(dict.fromkeys(selected))[:MAX_PARTICIPANTS]


def _role_opinion(
    role: str,
    topic: str,
    llm_client: Any,
    *,
    round_index: int = 1,
    prior: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """让一个角色就议题表态。

    LLM 不可用 / 返回非法结构 / 表态内容未通过校验时返回 None（该角色记为缺席，
    不阻塞整场会议）。
    """
    brief = ROLE_BRIEFS.get(role, role)
    lines = [
        "你正在参加一次多角色评审会，请只输出你的独立判断。",
        f"你的角色：{role}（{brief}）",
        f"会议议题：{topic}",
    ]
    if prior:
        lines.append("其它角色已表态（供参考，请独立判断，允许直接反对）：")
        lines.extend(
            f"- {item['role']}（{item['stance']}）：{item['content']}" for item in prior
        )
    lines.append(
        "以该角色视角给出结论，并只返回 JSON："
        '{"stance": "support|oppose|neutral", '
        f'"content": "结论与理由（不超过 {OPINION_CHARS_HINT} 字）", '
        '"evidence": ["支撑依据，可为空数组"]}'
    )
    data = llm_client.complete_json(
        "\n".join(lines), schema_hint="stance / content / evidence"
    )
    if not isinstance(data, dict) or not data:
        return None
    evidence = data.get("evidence")
    try:
        return check_opinion(
            role=role,
            stance=str(data.get("stance", "")),
            content=str(data.get("content", "")),
            evidence=evidence if isinstance(evidence, list) else None,
            round_index=round_index,
        )
    except AgentError:
        # 单条意见非法（缺字段 / 立场非法 / 仅含注入指令）→ 该角色缺席，不阻塞会议。
        return None


def _decision_note(topic: str, minutes: dict[str, Any], llm_client: Any) -> str:
    """用 LLM 生成一句建议决议；失败或为空时回落到规则式结论。"""
    fallback = str(minutes.get("suggested_decision", ""))
    summary = "；".join(
        f"{item['role']}：{item['content']}"
        for item in list(minutes.get("support", [])) + list(minutes.get("oppose", []))
    )
    prompt = (
        f"议题：{topic}\n各方意见：{summary or '（无明确表态）'}\n"
        "请用一句话给出建议决议（不得新增事实，不得改变各方立场）。"
    )
    data = llm_client.complete_json(prompt, schema_hint='{"decision": "一句话建议决议"}')
    if isinstance(data, dict):
        note = str(data.get("decision", "")).strip()
        if note:
            return note
    return fallback


def run_council(
    task_id: str,
    topic: str,
    *,
    llm_client: Any,
    participants: list[str] | None = None,
    rounds: int = 1,
) -> dict[str, Any]:
    """召开评审会：多角色独立表态 → 规则式收敛为结构化纪要。

    Args:
        task_id: 任务 ID（作 correlation_id）。
        topic: 会议议题。
        llm_client: 可用的 LLM 客户端；不可用时直接拒绝发起。
        participants: 可选，显式指定参会角色；默认核心名单 + 关键词补位。
        rounds: 轮数（1 = 仅独立表态；2 = 追加一轮交叉质疑，取第 2 轮结果）。

    Returns:
        纪要字典：council_build_minutes 的产出 + rounds / decision_note。

    Raises:
        AgentError(E_VALIDATION): 议题为空或超长 / 无可用 LLM 密钥 / 轮数非法 /
            全部角色都未能给出有效表态。
    """
    cleaned_topic = (topic or "").strip()
    if not cleaned_topic:
        raise _validation("议题不能为空", task_id)
    if len(cleaned_topic) > MAX_TOPIC_CHARS:
        raise _validation(f"议题过长（最多 {MAX_TOPIC_CHARS} 字符）", task_id)
    if llm_client is None or not getattr(llm_client, "is_available", False):
        raise _validation("评审会需要可用的 LLM 密钥，请先在底部栏配置密钥后重试", task_id)
    if rounds not in (1, 2):
        raise _validation(f"rounds 只能是 1 或 2（收到 {rounds}）", task_id)

    selected = select_participants(cleaned_topic, participants)
    opinions: list[dict[str, Any]] = []
    absent: list[str] = []

    # 第 1 轮：独立表态（互不可见，避免相互带偏）。
    for role in selected:
        opinion = _role_opinion(role, cleaned_topic, llm_client)
        if opinion is None:
            absent.append(role)
        else:
            opinions.append(opinion)

    # 第 2 轮（可选）：带上前一轮意见再次表态，取第 2 轮结果。
    if rounds >= 2 and opinions:
        refreshed: list[dict[str, Any]] = []
        for role in selected:
            if role in absent:
                continue
            opinion = _role_opinion(
                role, cleaned_topic, llm_client, round_index=2, prior=opinions
            )
            if opinion is None:
                if role not in absent:
                    absent.append(role)
            else:
                refreshed.append(opinion)
        if refreshed:
            opinions = refreshed

    if not opinions:
        raise _validation("全部参会角色都未能给出有效表态，请检查密钥后重试", task_id)

    minutes = build_minutes(topic=cleaned_topic, opinions=opinions, absent=absent)
    minutes["rounds"] = rounds
    minutes["decision_note"] = _decision_note(cleaned_topic, minutes, llm_client)
    return minutes


__all__ = [
    "CORE_PARTICIPANTS",
    "DEVIL_ADVOCATES",
    "KEYWORD_ROLES",
    "MAX_PARTICIPANTS",
    "MAX_ROUNDS",
    "MAX_TOPIC_CHARS",
    "OPINION_CHARS_HINT",
    "ROLE_BRIEFS",
    "run_council",
    "select_participants",
]
