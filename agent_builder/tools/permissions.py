"""默认角色权限矩阵（最小权限原则）。

门卫按此表放行：未列入 allowed_tools 的一律拒绝；high_risk_tools 需审批。
新增工具时，在此给真正需要的角色加工具名；不要给所有角色全开。

**审批门只拦"不可逆 / 越界"的动作**（卡 44，2026-10-11 起）：

- ``file_write`` / ``file_edit`` **不是**审批项 —— 它们在**工作区内**产出与修改文件是本职
  工作，边界由门卫的路径沙箱兜底（工作区外的路径一律 ``E_PERMISSION`` 硬拒绝，
  见 ``tools/gatekeeper.py``）。此前把它们与删除/提交/回滚同等对待，等于让用户为
  "写一个文件"每次点一次放行 —— 实测用户报"它并不能去修改文件"就是这个审批门造成的。
- 仍是审批项的是**不可逆或跨版本边界**的动作：``file_delete``（删数据）、
  ``git_commit``（写版本历史）、``rollback``（回滚版本）。

所以要表达的是"高风险"这件事本身，而不是给工具改名：权限矩阵里只放**真实工具名**
（评估层有断言 ''权限矩阵引用的工具都已注册''），"哪种写入需要审批"用下面两个常量表达。
"""

from __future__ import annotations

from agent_builder.contracts.schemas import RolePerm

#: 写盘类工具（真实工具名）。用于说明"哪些动作会改磁盘"，**不等于**需要审批。
WRITE_TOOLS = frozenset({"file_write", "file_edit", "file_delete"})

#: 写盘类工具里**按工作区边界判定**的那两个：工作区内放行、工作区外被沙箱硬拒绝。
#:
#: 这是卡 44 对"把 ``file_write`` / ``file_edit`` 从 high_risk 换成语义更准的高危项"的实现：
#: 真正的风险不是"写了文件"，而是"写到工作区外面去了"。该判定由门卫完成
#: （没有工具名叫 ``file_write_outside_workspace``，所以不进权限矩阵，否则会变成悬空引用）。
WORKSPACE_SCOPED_WRITE_TOOLS = frozenset({"file_write", "file_edit"})

#: 真正需要逐工具放行的写盘类：**不可逆 / 跨版本边界**（删数据 / 写版本历史 / 回滚版本）。
APPROVAL_GATED_WRITE_TOOLS = frozenset({"file_delete", "git_commit", "rollback"})

#: 上面两组之外的写类工具（保留派生式写法：新增写类工具时不会被悄悄漏掉）。
UNCLASSIFIED_WRITE_TOOLS = WRITE_TOOLS - WORKSPACE_SCOPED_WRITE_TOOLS - APPROVAL_GATED_WRITE_TOOLS

# 角色 → 权限（按需扩展；未列出的角色调用任何工具都会被门卫拒绝）
DEFAULT_ROLE_PERMS: dict[str, RolePerm] = {
    "conductor": RolePerm(
        role="conductor",
        allowed_tools=[
            "plan_validate",
            "approval_request",
            "change_notify",
            "audit_log",
            "memory_read",
            "config_read",
            "council_build_minutes",
            "council_check_opinion",
        ],
        high_risk_tools=[],
        notes="总指挥：编排任务全程、维护状态机、管理返工/中断/审批、收敛评审会纪要；只编排不执行，不可直接写文件/提交代码",
    ),
    "decomposer": RolePerm(
        role="decomposer",
        allowed_tools=[
            "config_read",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="分解器：拆需求为步骤DAG、去重/标注依赖/分组/歧义检查；只规划不执行",
    ),
    "scheduler": RolePerm(
        role="scheduler",
        allowed_tools=[
            "plan_validate",
            "config_read",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="调度器：步骤DAG→执行计划（拓扑排序+并行分组+失败预案）；只调度不执行",
    ),
    "router": RolePerm(
        role="router",
        allowed_tools=[
            "plan_validate",
            "config_read",
            "audit_log",
            "metric_collect",
        ],
        high_risk_tools=[],
        notes="路由者：匹配执行者+派发步骤+监控进度+回收产出；只路由不执行",
    ),
    "historian": RolePerm(
        role="historian",
        allowed_tools=[
            "audit_log",
            "git_log",
            "memory_read",
            "memory_write",
            "change_notify",
            "file_read",
        ],
        high_risk_tools=[],
        notes="记录员（副架构记录层）：记录全流程+维护版本历史+生成变更说明；日志不可篡改；审批被拒也记录",
    ),
    "gatekeeper": RolePerm(
        role="gatekeeper",
        allowed_tools=[
            "git_commit",
            "rollback",
            "test_run",
            "sandbox_run",
            "audit_log",
            "memory_read",
        ],
        high_risk_tools=["git_commit", "rollback"],
        notes="看门人（副架构执行层）：应用变更+跑回归+失败回滚；无批准不动作；批准过期重审",
    ),
    "impact_analyzer": RolePerm(
        role="impact_analyzer",
        allowed_tools=[
            "file_read",
            "code_search",
            "diff_preview",
            "memory_read",
            "config_read",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="影响分析者（副架构）：圈定波及范围+评估回归风险+估算成本；高风险需人工审",
    ),
    "proposer": RolePerm(
        role="proposer",
        allowed_tools=[
            "memory_read",
            "config_read",
            "audit_log",
            "metric_collect",
            "diff_preview",
            "file_read",
        ],
        high_risk_tools=[],
        notes="方案生成者（副架构）：定位根因+最小改动方案；无根因不下方案；多根因拆分提案",
    ),
    "auditor": RolePerm(
        role="auditor",
        allowed_tools=[
            "metric_collect",
            "audit_log",
            "memory_read",
            "config_read",
            "file_read",
        ],
        high_risk_tools=[],
        notes="审计员（副架构）：采集指标+对照基线+客观描述不下结论；连续N次异常触发优化",
    ),
    "summarizer": RolePerm(
        role="summarizer",
        allowed_tools=[
            "file_read",
            "memory_read",
            "memory_write",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="汇报员：汇集产出+结构化整理+不新增判断；未完成项如实列出不粉饰",
    ),
    "memory_keeper": RolePerm(
        role="memory_keeper",
        allowed_tools=[
            "memory_read",
            "memory_write",
            "memory_forget",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="记忆管家：内容分级+敏感加密+遗忘策略；未确认结论不写入长期；无结果不编造",
    ),
    "fact_checker": RolePerm(
        role="fact_checker",
        allowed_tools=[
            "citation_check",
            "web_fetch",
            "file_read",
            "code_search",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="事实核验者：核对来源+复核关键数据+核验报告；存疑不放行；证伪打回修改",
    ),
    "test_runner": RolePerm(
        role="test_runner",
        allowed_tools=[
            "test_run",
            "sandbox_run",
            "file_read",
            "code_search",
            "memory_read",
            "audit_log",
            "citation_check",
        ],
        high_risk_tools=[],
        notes="测试执行者：跑用例+权限边界检查+测试报告；不修改被测代码；环境异常重试1次",
    ),
    "searcher": RolePerm(
        role="searcher",
        allowed_tools=[
            "web_search",
            "web_fetch",
            "citation_check",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="检索执行者：多路关键词+去重排序+来源标注+置信度；结果不足换关键词再搜1轮",
    ),
    "data_analyst": RolePerm(
        role="data_analyst",
        allowed_tools=[
            "data_query",
            "sandbox_run",
            "file_read",
            "memory_read",
            "audit_log",
            "citation_check",
        ],
        high_risk_tools=[],
        notes="数据分析者：读数据+工具计算（绝不心算）+结论区分事实/推断/待验证；样本不足出质量报告",
    ),
    "doc_worker": RolePerm(
        role="doc_worker",
        allowed_tools=[
            "file_read",
            "file_write",
            "file_edit",
            "web_fetch",
            "citation_check",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="文档执行者：组织文档+来源标注+不编造；素材不足请求补充；来源不可考标存疑；写/改工作区内文件不需审批（越界由路径沙箱硬拒）",
    ),
    "code_worker": RolePerm(
        role="code_worker",
        allowed_tools=[
            "file_read",
            "file_list",
            "code_search",
            "file_write",
            "file_edit",
            "sandbox_run",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=[],
        notes="代码执行者：写/改代码+自测+变更说明；新依赖需批准；自测2次失败如实上报；写/改工作区内文件不需审批（越界由路径沙箱硬拒）",
    ),
    "operator": RolePerm(
        role="operator",
        allowed_tools=[
            "file_read",
            "file_list",
            "code_search",
            "file_write",
            "file_edit",
            "file_delete",
            "web_fetch",
            "web_search",
            "citation_check",
            "sandbox_run",
            "test_run",
            "data_query",
            "plan_validate",
            "memory_read",
            "audit_log",
            "metric_collect",
            "config_read",
            "diff_preview",
            "approval_request",
            "change_notify",
            "git_log",
        ],
        high_risk_tools=["file_delete"],
        notes="操作者：可读/列白名单文件、代码搜索、抓取/搜索网页、校验引用、沙箱执行、跑测试、读数据、校验计划DAG、只读记忆、写审计日志、采集指标、读配置、生成diff预览、发起审批、变更通知、查git历史；写/改工作区内文件不需审批（越界由路径沙箱硬拒），删除文件需审批",
    ),
    "sub_architect": RolePerm(
        role="sub_architect",
        allowed_tools=["git_commit", "rollback", "git_log"],
        high_risk_tools=["git_commit", "rollback"],
        notes="副架构师：提交版本/回滚版本/查git历史；提交和回滚需审批",
    ),
    "memory_manager": RolePerm(
        role="memory_manager",
        allowed_tools=[
            "memory_read",
            "memory_write",
            "memory_forget",
        ],
        high_risk_tools=[],
        notes="记忆管家：读写/清理记忆；敏感信息加密存储（专用角色，operator 不可写/清记忆）",
    ),
}


def get_default_role_perms() -> dict[str, RolePerm]:
    """返回默认权限矩阵的深拷贝（避免调用方修改全局状态）。"""
    return {role: perm.model_copy(deep=True) for role, perm in DEFAULT_ROLE_PERMS.items()}


# ── 能力组（卡 45）：把「工具白名单」再分一层，供界面开关真生效 ──────────
#
# 为什么要有这一层：左栏原来那三个开关（文件读写 / 联网搜索 / 命令执行）**没有后端出处**，
# 点了没有任何效果（前端自己写着"尚未接入后端，仅作展示"）。这里把"能力组 → 工具集合"
# 做成**唯一真源**，于是 `GET /capabilities` 的每一项都能追到工具白名单，
# 任务级开关也能真的把工具从角色白名单里剔除。
#
# 口径：**能力组是"额外放行"的粒度，不是"必须勾上才有"的粒度** —— 不传 = 全部启用
# （保持既有行为不变）；传了 = 只有列出的组启用。未知组名在请求层被拒绝（不静默忽略）。

#: 能力组 → 工具集合。**只放注册表里真实存在的工具名**（评估层有"引用的工具都已注册"断言）。
CAPABILITY_GROUPS: dict[str, frozenset[str]] = {
    "file": frozenset(
        {
            "file_read",
            "file_list",
            "file_write",
            "file_edit",
            "file_delete",
            "code_search",
            "diff_preview",
        }
    ),
    "network": frozenset({"web_search", "web_fetch"}),
    "shell": frozenset({"sandbox_run", "test_run"}),
    "git": frozenset({"git_commit", "git_log", "rollback"}),
}

#: 中文名（界面与文档共用，后端下发 → 前端不写死文案）。
CAPABILITY_LABELS: dict[str, str] = {
    "file": "文件读写",
    "network": "联网搜索",
    "shell": "命令执行",
    "git": "版本控制",
}

#: 说明（给界面做 tooltip，后端下发）。
CAPABILITY_NOTES: dict[str, str] = {
    "file": "读/列/写/改/删工作区内文件、搜代码；工作区内写入不需放行，越界一律拒绝",
    "network": "联网检索与抓取网页；关闭后本任务的角色白名单里不再有 web_search / web_fetch",
    "shell": "在工作区里跑命令与 pytest；⚠ 会执行外部程序",
    "git": "提交版本 / 回滚 / 查历史；⚠ 提交与回滚不可逆，需逐次放行",
}

#: 默认启用的能力组（不传 capabilities 时等价于这一组 = 全部）。
DEFAULT_CAPABILITIES: frozenset[str] = frozenset(CAPABILITY_GROUPS)

#: 这些工具**不受能力组开关管辖**（流程/记忆/审计类基础设施）。
#: 为什么单列：把它们也塞进组里，关掉任何一组都会顺带打断记账与审计 —— 那不是在"关能力"。
CAPABILITY_EXEMPT_TOOLS: frozenset[str] = frozenset(
    {
        "approval_request",
        "audit_log",
        "change_notify",
        "citation_check",
        "config_read",
        "council_build_minutes",
        "council_check_opinion",
        "data_query",
        "memory_forget",
        "memory_read",
        "memory_write",
        "metric_collect",
        "plan_validate",
    }
)


def capability_group_of(tool: str) -> str | None:
    """工具属于哪个能力组；不属于任何组（= 豁免）时返回 None。"""
    for group, tools in CAPABILITY_GROUPS.items():
        if tool in tools:
            return group
    return None


def tools_of_capabilities(
    enabled: set[str] | frozenset[str] | None, *, all_tools: set[str]
) -> set[str]:
    """按启用的能力组从 ``all_tools`` 里挑出可用工具（**豁免类总是保留**）。

    Args:
        enabled: 启用的能力组；None = 全部启用（既有行为）。
        all_tools: 待过滤的工具白名单。

    Returns:
        过滤后的工具集合。未知组名不在这里报错（请求层已校验），只是没有任何效果。
    """
    if enabled is None:
        return set(all_tools)
    allowed: set[str] = set(CAPABILITY_EXEMPT_TOOLS & all_tools)
    for group in enabled:
        allowed |= CAPABILITY_GROUPS.get(group, frozenset()) & all_tools
    return allowed


def filter_role_perms(
    role_perms: dict[str, RolePerm], enabled: set[str] | frozenset[str] | None
) -> dict[str, RolePerm]:
    """按能力开关收窄每个角色的 ``allowed_tools``（不动门卫语义）。

    ``high_risk_tools`` 一并收窄到剩余工具上 —— 否则会出现"高危项指向一个已被关掉的工具"
    这类自相矛盾的配置（``RolePerm`` 只保证 high_risk ⊆ allowed）。
    """
    if enabled is None:
        return role_perms
    narrowed: dict[str, RolePerm] = {}
    for role, perm in role_perms.items():
        allowed = tools_of_capabilities(enabled, all_tools=set(perm.allowed_tools))
        narrowed[role] = perm.model_copy(
            update={
                "allowed_tools": sorted(allowed),
                "high_risk_tools": sorted(set(perm.high_risk_tools) & allowed),
            }
        )
    return narrowed


__all__ = [
    "APPROVAL_GATED_WRITE_TOOLS",
    "CAPABILITY_EXEMPT_TOOLS",
    "CAPABILITY_GROUPS",
    "CAPABILITY_LABELS",
    "CAPABILITY_NOTES",
    "DEFAULT_CAPABILITIES",
    "DEFAULT_ROLE_PERMS",
    "UNCLASSIFIED_WRITE_TOOLS",
    "WORKSPACE_SCOPED_WRITE_TOOLS",
    "WRITE_TOOLS",
    "capability_group_of",
    "filter_role_perms",
    "get_default_role_perms",
    "tools_of_capabilities",
]
