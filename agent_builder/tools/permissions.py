"""默认角色权限矩阵（最小权限原则）。

门卫按此表放行：未列入 allowed_tools 的一律拒绝；high_risk_tools 需审批。
新增工具时，在此给真正需要的角色加工具名；不要给所有角色全开。
"""

from __future__ import annotations

from agent_builder.contracts.schemas import RolePerm

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
        ],
        high_risk_tools=[],
        notes="总指挥：编排任务全程、维护状态机、管理返工/中断/审批；只编排不执行，不可直接写文件/提交代码",
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
            "web_fetch",
            "citation_check",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=["file_write"],
        notes="文档执行者：组织文档+来源标注+不编造；素材不足请求补充；来源不可考标存疑",
    ),
    "code_worker": RolePerm(
        role="code_worker",
        allowed_tools=[
            "file_read",
            "file_list",
            "code_search",
            "file_write",
            "sandbox_run",
            "memory_read",
            "audit_log",
        ],
        high_risk_tools=["file_write"],
        notes="代码执行者：写/改代码+自测+变更说明；新依赖需批准；自测2次失败如实上报",
    ),
    "operator": RolePerm(
        role="operator",
        allowed_tools=[
            "file_read",
            "file_list",
            "code_search",
            "file_write",
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
        high_risk_tools=["file_write"],
        notes="操作者：可读/列白名单文件、代码搜索、抓取/搜索网页、校验引用、沙箱执行、跑测试、读数据、校验计划DAG、只读记忆、写审计日志、采集指标、读配置、生成diff预览、发起审批、变更通知、查git历史；写文件需审批",
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


__all__ = ["DEFAULT_ROLE_PERMS", "get_default_role_perms"]
