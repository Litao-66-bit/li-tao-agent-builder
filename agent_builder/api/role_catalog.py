"""角色能力目录 —— agentic 模式下模型「可选谁」的唯一真源。

背景：现状的派发是写死的 ``ACTION_ROLE_MAP``（action → 角色）。agentic 模式下改由
模型决策，因此必须给它一份**可枚举的能力目录**，并在执行前跑一遍校验链。
本模块只做「描述 + 过滤」：不执行、不决策、不调 LLM、无 IO。

架构分层依据 ``docs/execution-protocols.md`` 每节的「层级」字段
（与评分层 ``evaluation/scorecards.role_universe()`` 同源）。名单是**契约数据**，
漂移由 ``tests/test_role_catalog.py`` 交叉核对文档与权限矩阵捕获。

设计约束：
- 目录只列「能作为步骤执行者的角色」。流程控制角色（conductor / decomposer /
  scheduler / router）**不进目录** —— 让模型把"编排"当成一步去"执行"会造成
  递归与降级混乱。
- 权限层身份 ``memory_manager`` / ``sub_architect`` 暂不进目录：前者与
  ``memory_keeper`` 同权（同时可选会让模型在两个同权身份间选混），后者是
  版本治理入口（高风险提交/回滚），需要时再单独开。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent_builder.api.orchestrator import ACTION_ROLE_MAP
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS
from agent_builder.tools.registry import registry

# ── 架构分类（取值）──────────────────────────────────────────────

MAIN = "main"
SUB = "sub"
PERMISSION = "permission"

_ARCHITECTURE_LABELS: dict[str, str] = {
    MAIN: "主架构",
    SUB: "副架构",
    PERMISSION: "权限层",
}

# ── 名单（依据 docs/execution-protocols.md 的「层级」字段）─────────

# 主架构 12：总指挥层 / 规划层 2 / 执行层 5 / 验证层 2 / 记忆层 / 总结层。
MAIN_ARCHITECTURE: frozenset[str] = frozenset(
    {
        "conductor",
        "decomposer",
        "scheduler",
        "router",
        "code_worker",
        "doc_worker",
        "data_analyst",
        "searcher",
        "test_runner",
        "fact_checker",
        "memory_keeper",
        "summarizer",
    }
)

# 副架构 5：观测层 / 优化层 2 / 看门人 / 记录层。
SUB_ARCHITECTURE: frozenset[str] = frozenset(
    {"auditor", "proposer", "impact_analyzer", "gatekeeper", "historian"}
)

# 权限层 3（无独立实现模块，只有权限矩阵行）。
PERMISSION_LAYER: frozenset[str] = frozenset({"operator", "sub_architect", "memory_manager"})

# 流程控制角色：不作为「步骤执行者」进入目录。
NON_STEP_ROLES: frozenset[str] = frozenset({"conductor", "decomposer", "scheduler", "router"})

# 主架构里的步骤执行者（8）= 主架构 12 − 流程控制 4。
MAIN_EXECUTORS: frozenset[str] = MAIN_ARCHITECTURE - NON_STEP_ROLES

# 关闭副结构开关时可选的主架构外身份：operator 是 executor_fn 的默认身份
# （17 个 allowed_tools，含 file_write），不放它会让「纯工具步骤」没有合法归属。
PERMISSION_EXECUTORS: frozenset[str] = frozenset({"operator"})

# 给模型看的一句话能力（不照抄协议文档原文，只描述"能做什么"）。
ROLE_MISSIONS: dict[str, str] = {
    "searcher": "多路关键词检索并去重、标注来源",
    "doc_worker": "抓取网页 / 文档并组织结构化正文",
    "fact_checker": "核验引用与结论的事实性",
    "code_worker": "读写代码文件并给出变更说明",
    "data_analyst": "读数据、计算并给出结论（样本不足时只出质量报告）",
    "test_runner": "运行测试并报告结果",
    "summarizer": "汇总多来源产出、交付最终报告",
    "memory_keeper": "记忆的读取 / 写入 / 遗忘",
    "operator": "通用工具身份（默认）：直接执行已注册工具，无 LLM 角色行为",
    "auditor": "观测：采集指标与审计记录（副架构）",
    "proposer": "生成方案与优化提案（副架构）",
    "impact_analyzer": "分析变更的影响面（副架构）",
    "gatekeeper": "看门人：变更落地审批（副架构）",
    "historian": "变更记录（副架构）",
}


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """目录里的一个可选角色。

    Attributes:
        name: 角色名（= ``permissions.py`` / 执行协议里的 key）。
        architecture: ``main`` / ``sub`` / ``permission``。
        mission: 一句话能力描述（给模型看）。
        accepts: 可承接的 action 集合 —— 真实工具（须过权限矩阵）与**角色内行为**
            （如 ``summarize`` / ``propose``，不经工具门卫）的并集；
            空集表示"当前没有可承接的动作"，如看门人 / 记录员。
        high_risk_tools: 该角色下需审批的高风险工具。
    """

    name: str
    architecture: str
    mission: str
    accepts: frozenset[str]
    high_risk_tools: frozenset[str]

    @property
    def executable(self) -> bool:
        """当前是否有可承接的动作（无 → 只能等 P2 的「提议新工具」）。"""
        return bool(self.accepts)

    def to_prompt_line(self) -> str:
        """渲染成提示词里的一行（给模型看）。

        动作带上**参数签名**（见 :func:`action_signature`）—— 只给动作名的话，
        模型只能靠猜 ``inputs``（实测因此连着两次失败：缺 key、scope 猜成 long_term）。

        签名解决「参数名」，**参数语义**另起一行补（见 ``ACTION_PARAM_NOTES``）：
        实测模型把 ``file_read`` 的 path 传成目录 ``.``、``file_list`` 的 path 传成
        通配符 ``*``，连着两步 ``E_VALIDATION`` 后判空转停下。
        """
        label = _ARCHITECTURE_LABELS.get(self.architecture, self.architecture)
        if not self.accepts:
            return f"- {self.name}（{label}）：{self.mission}；暂无可执行的动作"
        actions = " / ".join(action_signature(a) for a in sorted(self.accepts))
        line = f"- {self.name}（{label}）：{self.mission}；可执行 {actions}"
        notes = [
            ACTION_PARAM_NOTES[a] for a in sorted(self.accepts) if a in ACTION_PARAM_NOTES
        ]
        if notes:
            line += "\n" + "\n".join(f"    · {note}" for note in notes)
        return line


# 单个参数的枚举值最多列几个（防某个动作把提示词撑爆）。
_MAX_ENUM_IN_SIGNATURE = 6

# 动作参数的**语义**补充：签名只说得出「参数名 + 必填/枚举」，说不出
# 「这个 path 该填文件还是目录」。实测缺了它模型只能靠猜，连着撞 E_VALIDATION。
# 只给容易搞错的动作写，避免把目录撑大。
ACTION_PARAM_NOTES: dict[str, str] = {
    "file_list": "path 传**目录**（工作区相对路径，`.` 就是工作区根目录；不支持通配符）",
    "file_read": (
        "path 传**具体文件**（不能传目录，也不支持通配符）；大文件只用 start_line/end_line "
        "读某段行范围（行号看观察里的「全文符号轮廓」）"
    ),
    "file_write": "path 传要写入的文件（父目录须已存在）；覆盖已有文件要带 `overwrite=true`",
    "file_edit": (
        "小改动用 file_edit，**不要整份重写**：old_string 得与文件里的原文完全一致"
        "（含缩进/空白），且在文件里必须唯一（否则带更多上下文或传 replace_all=true）"
    ),
    "file_delete": "path 传要删除的文件",
    "test_run": "target 传具体的测试文件或目录（如 tests/test_narrate.py）",
}

# 可选、但「会改变行为」的参数：必须进签名，否则模型根本不知道它存在 ——
# 实测 file_write 撞「文件已存在且未授权覆盖」，模型只能靠错误信息才知道有 overwrite。
_EXTRA_SIGNATURE_PARAMS: dict[str, tuple[str, ...]] = {
    "file_write": ("overwrite",),
    # 大文件读不到尾部是实测卡死的根因（观察窗口 2000 字），行范围参数必须进签名，
    # 否则模型根本不知道它可以只读某一段。
    "file_read": ("start_line", "end_line"),
    # 出现多次时是否全部替换 —— 不列进签名，模型就不知道有这条出路，只能干瞪"出现 N 次"。
    "file_edit": ("replace_all",),
}


def _enum_of(schema: object) -> list[str]:
    """取 JSON Schema 片段里声明的枚举值（没有则返回空列表）。"""
    if not isinstance(schema, dict):
        return []
    values = schema.get("enum")
    if not isinstance(values, (list, tuple)):
        return []
    return [str(item) for item in values][:_MAX_ENUM_IN_SIGNATURE]


def action_signature(action: str) -> str:
    """动作的紧凑参数签名：``key*`` 必填、``scope=long|short`` 带枚举。

    **为什么必须给**：决策提示词此前只给「角色 + 动作名」，模型只能猜 ``inputs`` ——
    实测连着两次因「缺 key」「scope 猜成 long_term」失败（``file_list`` 缺 path 同理），
    白跑两轮就空转停下。只列**必填参数 + 带枚举的参数**（``limit`` 这类可选又无枚举的不铺开），
    既够模型给出合法调用，也不至于把提示词撑大。

    角色内行为（``summarize`` / ``propose`` 等非注册工具）没有 schema，原样返回动作名。
    """
    entry = registry.get(action)
    if entry is None:
        return action
    spec = entry[0]  # registry.get → (spec, impl)
    properties = spec.parameters.get("properties") or {}
    required = set(spec.parameters.get("required") or [])
    parts: list[str] = []
    listed: set[str] = set()
    for name, schema in properties.items():
        enum = _enum_of(schema)
        is_required = name in required
        if not is_required and not enum:
            continue
        text = name + ("*" if is_required else "")
        if enum:
            text += "=" + "|".join(enum)
        parts.append(text)
        listed.add(name)
    # 可选但会改变行为的参数（如 file_write 的 overwrite）也列出来 —— 不列等于藏起来。
    for name in _EXTRA_SIGNATURE_PARAMS.get(action, ()):
        if name in properties and name not in listed:
            parts.append(name)
    return f"{action}({', '.join(parts)})" if parts else action


def architecture_of(role: str) -> str | None:
    """角色 → 架构分类；未知角色返回 None。"""
    if role in MAIN_ARCHITECTURE:
        return MAIN
    if role in SUB_ARCHITECTURE:
        return SUB
    if role in PERMISSION_LAYER:
        return PERMISSION
    return None


def all_roles() -> frozenset[str]:
    """全量计分角色（20 个）。"""
    return MAIN_ARCHITECTURE | SUB_ARCHITECTURE | PERMISSION_LAYER


def selectable_roles(*, sub_arch: bool) -> frozenset[str]:
    """模型可选的角色集合。

    Args:
        sub_arch: 副结构开关是否开启。关闭时只给主架构执行者 + 权限身份；
            开启时追加副架构（其中 ``proposer`` / ``impact_analyzer`` / ``auditor``
            是可执行的，看门人与记录员要等新工具）。
    """
    base = MAIN_EXECUTORS | PERMISSION_EXECUTORS
    return (base | SUB_ARCHITECTURE) if sub_arch else base


def _accepts_by_role() -> dict[str, frozenset[str]]:
    """角色 → 可承接的 action。

    - **有实现类的角色**：取 ``ACTION_ROLE_MAP`` 反查。它既含真实工具，也含
      ``summarize`` / ``report`` / ``propose`` / ``optimize`` / ``impact_analyze`` /
      ``assess`` 这类**角色内行为**（这些名字并未注册为工具，不经工具门卫）。
    - **权限层身份**（无实现类，如 ``operator``）：取权限矩阵的 ``allowed_tools``
      —— 它们没有角色行为，能做的只有"直接执行已注册工具"。
    """
    mapping: dict[str, set[str]] = {}
    for action, role in ACTION_ROLE_MAP.items():
        mapping.setdefault(role, set()).add(action)
    for role in PERMISSION_LAYER:
        perm = DEFAULT_ROLE_PERMS.get(role)
        if perm is not None:
            mapping.setdefault(role, set()).update(perm.allowed_tools)
    return {role: frozenset(actions) for role, actions in mapping.items()}


def build_catalog(*, sub_arch: bool) -> tuple[RoleSpec, ...]:
    """构造能力目录（按角色名排序，保证同输入同输出）。

    Args:
        sub_arch: 副结构开关；False 时目录里不含副架构角色。
    """
    accepts = _accepts_by_role()
    specs: list[RoleSpec] = []
    for role in sorted(selectable_roles(sub_arch=sub_arch)):
        perm = DEFAULT_ROLE_PERMS.get(role)
        specs.append(
            RoleSpec(
                name=role,
                architecture=architecture_of(role) or PERMISSION,
                mission=ROLE_MISSIONS.get(role, "（未登记能力描述）"),
                accepts=accepts.get(role, frozenset()),
                high_risk_tools=frozenset(perm.high_risk_tools) if perm else frozenset(),
            )
        )
    return tuple(specs)


def catalog_prompt(*, sub_arch: bool, specs: Sequence[RoleSpec] | None = None) -> str:
    """把目录渲染成给决策模型的提示词片段。

    Args:
        sub_arch: 副结构开关（决定结尾那句"能不能提议"）。
        specs: 预构造的目录（如 ``LoopContext.catalog``）；None 时按 ``sub_arch`` 现构。
            允许外部传入是为了让提示词与**当前实际可选目录**保持一致，
            而不是各自按开关重新推导一遍。
    """
    resolved = tuple(specs) if specs is not None else build_catalog(sub_arch=sub_arch)
    role_lines = [spec.to_prompt_line() for spec in resolved]
    lines = ["可用角色（只能从中选择，不得自造角色名）：", *role_lines]
    # 参数签名用**半角**括号（角色标签那对是全角）→ 据此判断要不要给「参数写法」说明。
    if any("(" in line for line in role_lines):
        lines.append(
            "参数写法：动作(必填参数*, 带枚举的参数=值1|值2)；请**按签名给 inputs**，不要猜参数名。"
        )
    lines.append(
        "禁止选择：" + " / ".join(sorted(NON_STEP_ROLES)) + "（流程控制角色，不是可执行步骤）"
    )
    if sub_arch:
        lines.append("副结构已开启：你可以用 kind=propose 提议新角色或新工具（需用户批准后才可用）。")
    else:
        lines.append("副结构未开启：只能调用以上主架构角色，不得提议新角色或新工具。")
    return "\n".join(lines)


def is_high_risk(role: str, action: str) -> bool:
    """该角色下这个 action 是否属高风险（需审批）。"""
    perm = DEFAULT_ROLE_PERMS.get(role)
    return bool(perm and perm.is_high_risk(action))


__all__ = [
    "MAIN",
    "MAIN_ARCHITECTURE",
    "MAIN_EXECUTORS",
    "NON_STEP_ROLES",
    "PERMISSION",
    "PERMISSION_EXECUTORS",
    "PERMISSION_LAYER",
    "ROLE_MISSIONS",
    "SUB",
    "SUB_ARCHITECTURE",
    "RoleSpec",
    "all_roles",
    "architecture_of",
    "build_catalog",
    "catalog_prompt",
    "is_high_risk",
    "selectable_roles",
]
