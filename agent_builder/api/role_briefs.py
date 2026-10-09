"""角色简报（role brief）—— 从现有真源派生「角色身份前缀」。

阶段 1（默认关闭）：
- 注入层：LLM 客户端层统一注入（角色代码零改动）；注入 system 段**最前**，
  输出契约在后，硬约束不被稀释。
- 内容来源：``docs/execution-protocols.md``（角色 / 使命 / 边界 / 分步流程）+
  ``tools/permissions.py``（授权工具，执行真源）。**不新增真源**，只做派生。
- 三档：``off``（现状基线）/ ``core``（职责 + 工具 + 边界）/ ``full``（core + 流程）。
- 开关：环境变量 ``AGENT_BUILDER_ROLE_BRIEF``，默认 ``off``。

边界说明：本模块属编排层（``api/``），只做「读真源 + 渲染字符串」，不调 LLM。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache

from agent_builder.evaluation.scorecards import DocRole, parse_protocols
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS

logger = logging.getLogger(__name__)

# 档位开关（环境变量；非法值回落默认档 off）。
MODE_ENV = "AGENT_BUILDER_ROLE_BRIEF"
MODE_OFF = "off"
MODE_CORE = "core"
MODE_FULL = "full"
MODES: tuple[str, ...] = (MODE_OFF, MODE_CORE, MODE_FULL)
DEFAULT_MODE = MODE_OFF

# 覆盖范围：会调 LLM 的角色（3 个权限层角色不调 LLM，不覆盖）。
LLM_ROLES: tuple[str, ...] = (
    "decomposer",
    "searcher",
    "doc_worker",
    "fact_checker",
    "code_worker",
    "summarizer",
)


@dataclass(frozen=True, slots=True)
class RoleBriefSource:
    """单个角色简报的结构化来源（全部派生自现有真源）。"""

    role: str
    cn_name: str
    layer: str
    mission: str
    boundary: str
    tools: tuple[str, ...]
    steps: tuple[str, ...]


def current_mode() -> str:
    """当前档位（读环境变量；非法值回落 ``off``，保证默认不注入）。"""
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    return raw if raw in MODES else DEFAULT_MODE


def is_enabled() -> bool:
    """当前是否启用了角色简报（非 ``off`` 档）。"""
    return current_mode() != MODE_OFF


def granted_tools(role: str) -> tuple[str, ...]:
    """角色的实授工具（执行真源 ``permissions.py``）。"""
    perm = DEFAULT_ROLE_PERMS.get(role)
    return tuple(sorted(perm.allowed_tools)) if perm else ()


def _cn_name(heading: str) -> str:
    """从章节标题里取中文名（``FactChecker（事实核验者）`` → ``事实核验者``）。"""
    match = re.search(r"[（(]([^（()）]+)[）)]", heading)
    return match.group(1).strip() if match else ""


def _layer_desc(layer: str | None) -> str:
    """从层级字段里取层级描述（``executor（主架构·验证层）`` → ``主架构·验证层``）。"""
    if not layer:
        return ""
    match = re.search(r"[（(]([^（()）]+)[）)]", layer)
    return (match.group(1) if match else layer).strip()


def _to_source(doc: DocRole) -> RoleBriefSource:
    return RoleBriefSource(
        role=doc.key,
        cn_name=_cn_name(doc.heading),
        layer=_layer_desc(doc.layer),
        mission=(doc.mission or "").strip(),
        boundary=(doc.boundary or "").strip(),
        tools=granted_tools(doc.key),
        steps=tuple(doc.steps),
    )


@lru_cache(maxsize=1)
def build_role_brief_sources() -> dict[str, RoleBriefSource]:
    """派生全部角色的简报来源（复用评分器的契约解析；进程内缓存）。"""
    return {role: _to_source(doc) for role, doc in parse_protocols().items()}


def parse_brief_sources(text: str) -> dict[str, RoleBriefSource]:
    """从给定文档文本派生简报来源（供测试注入「格式变化」的文本）。"""
    return {role: _to_source(doc) for role, doc in parse_protocols(text).items()}


def render_brief(
    role: str,
    mode: str,
    *,
    sources: dict[str, RoleBriefSource] | None = None,
) -> str:
    """按档位渲染角色简报。

    Args:
        role: 角色名（须在 ``LLM_ROLES`` 覆盖范围内）。
        mode: ``off`` / ``core`` / ``full``。
        sources: 可选的来源表（默认读真源并缓存）。

    Returns:
        ``off`` 档返回空串；``core`` 为 职责 + 工具 + 边界；``full`` 追加分步流程。

    Raises:
        ValueError: 档位非法 / 角色不在覆盖范围 / 文档缺字段（格式可能已变化）。
    """
    if mode not in MODES:
        raise ValueError(f"未知档位: {mode}")
    if mode == MODE_OFF:
        return ""
    if role not in LLM_ROLES:
        raise ValueError(f"角色 {role} 不在简报覆盖范围（只覆盖会调 LLM 的角色）")
    table = build_role_brief_sources() if sources is None else sources
    source = table.get(role)
    if source is None:
        raise ValueError(f"执行协议文档缺 {role} 章节")
    missing = [
        label
        for label, value in (("使命", source.mission), ("边界", source.boundary), ("授权工具", source.tools))
        if not value
    ]
    if mode == MODE_FULL and not source.steps:
        missing.append("分步流程")
    if missing:
        raise ValueError(
            f"{role} 简报缺字段 {missing}：docs/execution-protocols.md 格式可能已变化"
        )

    identity = f"{role}（{source.cn_name}）" if source.cn_name else role
    if source.layer:
        identity += f"· {source.layer}"
    lines = [
        f"【角色】{identity}",
        f"【使命】{source.mission}",
        f"【授权工具】{'、'.join(source.tools)}",
        f"【边界】{source.boundary}",
    ]
    if mode == MODE_FULL:
        flow = " → ".join(f"{index} {step}" for index, step in enumerate(source.steps, 1))
        lines.append(f"【流程】{flow}")
    return "\n".join(lines)


def brief_for(role: str, mode: str | None = None) -> str:
    """按档位生成角色简报。

    Args:
        role: 角色名。
        mode: 档位覆盖（``off`` / ``core`` / ``full``）；``None`` 用环境变量
            ``AGENT_BUILDER_ROLE_BRIEF``（默认 ``off``）。供 A/B 对照按请求切档。

    关闭 / 角色不在覆盖范围 / 档位非法 / 真源格式失效时返回空串
    （只记警告，不阻断执行）；格式问题由 ``tests/test_role_briefs.py``
    的对账测试负责暴露。
    """
    resolved = current_mode() if mode is None else mode
    if resolved == MODE_OFF or role not in LLM_ROLES:
        return ""
    try:
        return render_brief(role, resolved)
    except ValueError as exc:
        logger.warning("角色简报生成失败，已跳过注入: %s", exc)
        return ""


__all__ = [
    "DEFAULT_MODE",
    "LLM_ROLES",
    "MODES",
    "MODE_CORE",
    "MODE_ENV",
    "MODE_FULL",
    "MODE_OFF",
    "RoleBriefSource",
    "brief_for",
    "build_role_brief_sources",
    "current_mode",
    "granted_tools",
    "is_enabled",
    "parse_brief_sources",
    "render_brief",
]
