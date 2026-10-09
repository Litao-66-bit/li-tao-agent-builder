"""Agent（角色）评分 —— 三方一致性 + 契约条目静态检查。

用法（在项目根目录）：

    python -m agent_builder.evaluation.scorecards
    python -m agent_builder.evaluation.scorecards --out docs/reports/agents-scorecard.md

评分维度（各 0–5，满分 30）：
1. 契约符合度 —— docs/execution-protocols.md 的授权清单 / 边界声明 / 异常处理表 vs 实现
2. 权限最小化 —— 边界声明里的禁止项不得被授权；角色不得悬空
3. 失败语义   —— 是否声明并真正产出降级态（pending / failed / error）
4. 可观测性   —— 结构化结果类型与字段丰富度
5. 测试覆盖   —— tests/test_roles_<role>.py 的 Functional / Edge 覆盖与用例量
6. 依赖清晰度 —— 只依赖注入的 executor_fn，不跨层依赖 api / tools 单例

三方真源：
- 契约：``docs/execution-protocols.md``（角色边界声明 + 失败处理表）
- 权限：``agent_builder/tools/permissions.py``（执行真源）
- 实现：``agent_builder/roles/<role>.py``

设计约束：不联网、不执行角色逻辑、同输入必得同输出（不写入时间戳）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DOC_PROTOCOLS = _PROJECT_ROOT / "docs" / "execution-protocols.md"
_ROLES_DIR = _PROJECT_ROOT / "agent_builder" / "roles"
_TESTS_DIR = _PROJECT_ROOT / "tests"

# 边界声明里的「禁止动作」→ 必须不被授权的工具。
FORBIDDEN_TOOL_MAP: tuple[tuple[str, str], ...] = (
    ("写文件", "file_write"),
    ("提交代码", "git_commit"),
    ("执行命令", "sandbox_run"),
)

# 高风险写类工具：授权即可写者必须挂审批门。
# （RolePerm 只保证 high_risk_tools ⊆ allowed_tools，反向不保证。）
WRITE_TOOLS = frozenset({"file_write", "git_commit", "rollback"})

# 跨层依赖：角色不应直接依赖这些层。
LAYER_VIOLATIONS = ("agent_builder.api", "agent_builder.tools", "agent_builder.llm")

# 正常路径用例类的命名提示（本仓库 test_roles_*.py 的既有约定）。
_FUNCTIONAL_HINTS = ("functional", "fullcycle", "basic", "cycle")


@dataclass(frozen=True, slots=True)
class Dimension:
    """单个评分维度。"""

    name: str
    score: int
    max_score: int
    note: str


@dataclass(frozen=True, slots=True)
class RoleScore:
    """单个角色的评分卡。

    ``permission_only`` 角色（仅存在于权限矩阵，无独立实现模块）只参与
    契约符合度与权限最小化两个维度，实现类维度记为空（不适用）。
    """

    role: str
    kind: str  # executor | orchestrator | permission_only
    doc_tools: tuple[str, ...]
    granted_tools: tuple[str, ...]
    dimensions: tuple[Dimension, ...]
    total: int
    applicable_max: int
    safety_flags: tuple[str, ...]
    actions: tuple[str, ...]


# ── 契约解析（docs/execution-protocols.md）────────────────────────


@dataclass(frozen=True, slots=True)
class DocRole:
    """文档中一个角色章节的结构化视图。

    ``mission`` / ``steps`` 供「角色简报」派生使用（同一份真源，不再另建解析）。
    """

    heading: str
    key: str
    tools: frozenset[str]
    boundary: str | None
    exception_rows: int
    external_impl: str | None
    layer: str | None
    mission: str | None = None
    steps: tuple[str, ...] = ()


def parse_protocols(text: str | None = None) -> dict[str, DocRole]:
    """解析执行协议文档，抽出每个角色的契约条目。

    Args:
        text: 可选的文档文本；None 时读 ``docs/execution-protocols.md``。
            （「角色简报」派生复用本解析，故留出文本入口便于测试格式变化。）
    """
    text = _DOC_PROTOCOLS.read_text(encoding="utf-8") if text is None else text
    sections = re.findall(r"^## (.+?)\n((?:(?!^## ).)*)", text, re.MULTILINE | re.DOTALL)
    roles: dict[str, DocRole] = {}
    for heading, body in sections:
        key_match = re.search(r"\|\s*角色名\s*\|\s*`(\w+)`\s*\|", body)
        if not key_match:
            continue
        impl_match = re.search(r"\*\*已实现\*\*为\s*`([^`]+)`", body)
        layer_match = re.search(r"\|\s*层级\s*\|\s*([^|]+?)\s*\|", body)
        roles[key_match.group(1)] = DocRole(
            heading=heading.strip(),
            key=key_match.group(1),
            tools=frozenset(_parse_authorized_tools(body)),
            boundary=_find_boundary(body),
            exception_rows=_count_exception_rows(body),
            external_impl=impl_match.group(1) if impl_match else None,
            layer=layer_match.group(1) if layer_match else None,
            mission=_find_mission(body),
            steps=tuple(_parse_steps(body)),
        )
    return roles


def _parse_authorized_tools(body: str) -> list[str]:
    """抽取「### 授权清单」小节的工具名。"""
    block = re.search(r"### 授权清单\n((?:(?!^### ).)*)", body, re.MULTILINE | re.DOTALL)
    if not block:
        return []
    return re.findall(r"^\|\s*`([a-z_]+)`\s*\|", block.group(1), re.MULTILINE)


def _find_boundary(body: str) -> str | None:
    match = re.search(r"\*\*边界声明\*\*：(.+)", body)
    return match.group(1).strip() if match else None


def _find_mission(body: str) -> str | None:
    """抽取「角色规格」表里的使命一句话。"""
    match = re.search(r"^\|\s*使命\s*\|\s*(.+?)\s*\|\s*$", body, re.MULTILINE)
    return match.group(1).strip() if match else None


def _parse_steps(body: str) -> list[str]:
    """抽取「#### 分步流程」代码块里的编号步骤（只取首行，忽略子项）。"""
    block = re.search(r"^#### 分步流程\n+```\n(.*?)\n```", body, re.MULTILINE | re.DOTALL)
    if not block:
        return []
    steps: list[str] = []
    for line in block.group(1).splitlines():
        match = re.match(r"^\s*\d+\.\s*(.+?)\s*$", line)
        if match:
            steps.append(match.group(1))
    return steps


def _count_exception_rows(body: str) -> int:
    """统计异常处理 / 决策规则 / 校验分支表中的数据行（标题层级不限）。"""
    total = 0
    for title in ("异常处理", "决策规则", "校验分支"):
        block = re.search(
            r"^#{3,4} " + title + r"\n((?:(?!^#{2,4} ).)*)", body, re.MULTILINE | re.DOTALL
        )
        if not block:
            continue
        rows = [
            line
            for line in block.group(1).splitlines()
            if line.startswith("|") and not re.match(r"^\|[\s\-|]+\|$", line)
        ]
        total += max(0, len(rows) - 1)  # 去掉表头
    return total


# ── 实现视图 ────────────────────────────────────────────────────


def _granted_tools(role: str) -> tuple[str, ...]:
    perm = DEFAULT_ROLE_PERMS.get(role)
    return tuple(sorted(perm.allowed_tools)) if perm else ()


def _high_risk_tools(role: str) -> tuple[str, ...]:
    perm = DEFAULT_ROLE_PERMS.get(role)
    return tuple(sorted(perm.high_risk_tools)) if perm else ()


def _module_path(role: str) -> Path | None:
    path = _ROLES_DIR / f"{role}.py"
    return path if path.exists() else None


@dataclass(frozen=True, slots=True)
class _ImplView:
    """角色实现模块的静态视图。"""

    source: str
    result_fields: frozenset[str]
    statuses: frozenset[str]

    @property
    def has_result_type(self) -> bool:
        return bool(self.result_fields)


def _impl_view(role: str) -> _ImplView | None:
    path = _module_path(role)
    if path is None:
        return None
    source = path.read_text(encoding="utf-8")
    fields: set[str] = set()
    statuses: set[str] = set()
    for line in source.splitlines():
        field_match = re.match(r"\s{4}(\w+)\s*:", line)
        if field_match:
            fields.add(field_match.group(1))
        comment = re.search(r"status:\s*str\s*#\s*(.+)", line)
        if comment:
            statuses.update(re.findall(r"[a-z_]{3,}", comment.group(1)))
    return _ImplView(source=source, result_fields=frozenset(fields), statuses=frozenset(statuses))


# ── 六个维度 ────────────────────────────────────────────────────


def _score_contract(doc: DocRole | None, granted: tuple[str, ...]) -> tuple[Dimension, list[str]]:
    score = 5
    notes: list[str] = []
    flags: list[str] = []
    if doc is None:
        score -= 2
        flags.append("契约缺失：docs/execution-protocols.md 无该角色章节")
    else:
        doc_tools = set(doc.tools)
        granted_set = set(granted)
        if doc_tools != granted_set:
            score -= 2
            flags.append(
                f"契约违反：文档授权清单={sorted(doc_tools)} 与 permissions.py 实授={sorted(granted_set)} 不一致"
            )
        if not doc.boundary:
            score -= 1
            notes.append("缺边界声明")
        if doc.exception_rows == 0:
            score -= 1
            notes.append("缺异常处理表")
    return (
        Dimension(
            name="契约符合度",
            score=max(0, score),
            max_score=5,
            note="; ".join(flags + notes) or "授权清单/边界声明/异常处理表三者齐备且与 permissions.py 一致",
        ),
        flags,
    )


def _score_permissions(role: str, doc: DocRole | None, granted: tuple[str, ...]) -> tuple[Dimension, list[str]]:
    score = 5
    notes: list[str] = []
    flags: list[str] = []
    if not granted:
        score -= 2
        flags.append("悬空角色：permissions.py 未授予任何工具")
    if doc is not None and doc.boundary:
        for keyword, tool in FORBIDDEN_TOOL_MAP:
            if keyword in doc.boundary and tool in granted:
                score -= 3
                flags.append(f"权限过宽：边界声明禁止「{keyword}」，但仍实授 `{tool}`")
    write_granted = sorted(WRITE_TOOLS & set(granted))
    gated = set(_high_risk_tools(role))
    ungated_write = [tool for tool in write_granted if tool not in gated]
    if ungated_write:
        score -= 3
        flags.append(f"审批门缺失：实授写类工具 {ungated_write} 却未列入 high_risk_tools")
    granted_set = set(granted)
    if not (granted_set & WRITE_TOOLS):
        notes.append("只读角色，无写类工具")
    return (
        Dimension(
            name="权限最小化",
            score=max(0, score),
            max_score=5,
            note="; ".join(flags + notes) or f"实授 {len(granted)} 个工具，写类均挂审批门",
        ),
        flags,
    )


def _normalized(checks: dict[str, bool], note_ok: str) -> Dimension:
    """把「命中项 / 总项」折算成 0–5 分。"""
    hits = sum(checks.values())
    missed = [label for label, ok in checks.items() if not ok]
    return Dimension(
        name="",
        score=round(5 * hits / len(checks)),
        max_score=5,
        note=note_ok if not missed else f"缺: {missed}",
    )


def _score_failure_semantics(view: _ImplView | None, kind: str, doc: DocRole | None) -> Dimension:
    """失败语义：不同角色类型的降级落点不同，按契约分别判定。

    - 执行类：结果需声明 pending / failed，代码中确实产生降级结果，并有 error 字段。
    - 编排/规划类：契约要求「不执行」，降级落点是 pending_questions / fallback / FAILED 状态，
      并要求错误走契约异常、契约里有异常处理表。
    """
    if view is None:
        return Dimension(name="失败语义", score=0, max_score=5, note="无实现模块（不适用）")
    source = view.source
    if kind == "orchestrator":
        checks = {
            "声明降级出口（pending_questions/fallback/FAILED/pending）": bool(
                re.search(r"pending_questions|fallback|FAILED|pending", source)
            ),
            "错误走契约异常": bool(re.search(r"contracts\.errors|\w+_error\(", source)),
            "契约含异常处理表": bool(doc and doc.exception_rows > 0),
        }
        dimension = _normalized(checks, "降级出口与错误路径均符合契约")
    else:
        checks = {
            "声明 pending（待确认/降级）": "pending" in view.statuses,
            "声明 failed（失败态）": "failed" in view.statuses,
            "代码中产生降级结果": bool(
                re.search(r"status\s*=\s*[\"'](?:pending|failed)", source)
            ),
            "有 error 字段": "error" in view.result_fields,
        }
        dimension = _normalized(checks, "超时/权限不足/无来源/样本不足均有降级落点")
    return Dimension(
        name="失败语义",
        score=dimension.score,
        max_score=dimension.max_score,
        note=dimension.note,
    )


def _score_observability(view: _ImplView | None, kind: str) -> Dimension:
    """可观测性：是否产出结构化、可消费的结果对象。"""
    if view is None:
        return Dimension(name="可观测性", score=0, max_score=5, note="无实现模块（不适用）")
    source = view.source
    if kind == "orchestrator":
        checks = {
            "有结构化结果类型": view.has_result_type,
            "字段 ≥ 3": len(view.result_fields) >= 3,
            "引用契约模型（TaskState/Plan/Step）": bool(
                re.search(r"TaskState|Plan\(|Step\b", source)
            ),
            "docstring 声明交付/返回": bool(re.search(r"Returns:|交付物|返回", source)),
        }
        dimension = _normalized(checks, "编排产物为结构化契约对象")
    else:
        checks = {
            "有结构化结果类型": view.has_result_type,
            "结果字段 ≥ 3": len(view.result_fields) >= 3,
            "含 status 字段": "status" in view.result_fields,
            "含 error 字段": "error" in view.result_fields,
        }
        dimension = _normalized(checks, f"结构化字段 {len(view.result_fields)} 个")
    return Dimension(
        name="可观测性",
        score=dimension.score,
        max_score=dimension.max_score,
        note=dimension.note,
    )


def _score_tests(role: str) -> Dimension:
    test_file = _TESTS_DIR / f"test_roles_{role}.py"
    if not test_file.exists():
        return Dimension(name="测试覆盖", score=0, max_score=5, note=f"缺 {test_file.name}（零测试）")
    text = test_file.read_text(encoding="utf-8")
    classes = {name.lower() for name in re.findall(r"^class (\w+)", text, re.MULTILINE)}
    cases = re.findall(r"^\s+def (test_\w+)\(", text, re.MULTILINE)
    checks = {
        "正常路径用例类": any(any(hint in name for hint in _FUNCTIONAL_HINTS) for name in classes),
        "Edge 用例类": any("edge" in name for name in classes),
        "用例数 ≥ 10": len(cases) >= 10,
    }
    hits = sum(checks.values())
    missed = [label for label, ok in checks.items() if not ok]
    return Dimension(
        name="测试覆盖",
        score=2 + hits,
        max_score=5,
        note=f"{len(cases)} 个用例" if not missed else f"缺 {missed}（共 {len(cases)} 个用例）",
    )


def _score_dependencies(view: _ImplView | None, kind: str) -> tuple[Dimension, list[str]]:
    """依赖清晰度：不跨层依赖；需要执行的角色必须靠注入 executor_fn。"""
    if view is None:
        return Dimension(name="依赖清晰度", score=0, max_score=5, note="无实现模块（不适用）"), []
    score = 5
    notes: list[str] = []
    flags: list[str] = []
    violations = [module for module in LAYER_VIOLATIONS if module in view.source]
    if violations:
        score -= 3
        flags.append(f"跨层依赖：{violations}（角色层不应依赖 api/tools/llm）")
    if kind == "executor" and "executor_fn" not in view.source:
        score -= 2
        flags.append("未通过 executor_fn 注入执行器（可能依赖隐式全局）")
    if re.search(r"^_[a-z][a-z_]*\s*[:=]", view.source, re.MULTILINE):
        score -= 1
        notes.append("存在模块级私有可变状态")
    note = "; ".join(flags + notes)
    if not note:
        note = (
            "只注入 executor_fn，无跨层依赖与隐式全局"
            if kind == "executor"
            else "契约声明不执行，无需注入执行器；无跨层依赖与隐式全局"
        )
    return Dimension(name="依赖清晰度", score=max(0, score), max_score=5, note=note), flags


# ── 汇总 ────────────────────────────────────────────────────────


def _classify(role: str, doc: DocRole | None) -> str:
    """角色类型：executor / orchestrator / permission_only。

    编排类由契约的边界声明判定（声明「不执行」者不要求注入执行器）。
    """
    if _module_path(role) is None:
        return "permission_only"
    if doc is not None and doc.boundary and "不执行" in doc.boundary:
        return "orchestrator"
    return "executor"


def _suggestions(dimensions: tuple[Dimension, ...], flags: tuple[str, ...]) -> tuple[str, ...]:
    actions: list[str] = []
    if flags:
        actions.append("【安全·优先】" + "；".join(flags))
    label = {
        "失败语义": "【失败语义】",
        "可观测性": "【可观测性】",
        "测试覆盖": "【测试】",
        "契约符合度": "【契约】",
    }
    by_name = {dim.name: dim for dim in dimensions}
    for name, prefix in label.items():
        dim = by_name.get(name)
        if dim is not None and dim.score < dim.max_score:
            actions.append(prefix + dim.note)
    return tuple(actions) or ("无",)


def score_role(role: str, doc: DocRole | None) -> RoleScore:
    """给单个角色打分。"""
    granted = _granted_tools(role)
    kind = _classify(role, doc)
    view = _impl_view(role)
    contract_dim, contract_flags = _score_contract(doc, granted)
    perm_dim, perm_flags = _score_permissions(role, doc, granted)
    if kind == "permission_only":
        dimensions = (contract_dim, perm_dim)
    else:
        dep_dim, dep_flags = _score_dependencies(view, kind)
        dimensions = (
            contract_dim,
            perm_dim,
            _score_failure_semantics(view, kind, doc),
            _score_observability(view, kind),
            _score_tests(role),
            dep_dim,
        )
        perm_flags = perm_flags + dep_flags
    flags = tuple(sorted(set(contract_flags) | set(perm_flags)))
    return RoleScore(
        role=role,
        kind=kind,
        doc_tools=tuple(sorted(doc.tools)) if doc else (),
        granted_tools=granted,
        dimensions=dimensions,
        total=sum(dim.score for dim in dimensions),
        applicable_max=sum(dim.max_score for dim in dimensions),
        safety_flags=flags,
        actions=_suggestions(dimensions, flags),
    )


def role_universe() -> list[str]:
    """三方并集：文档契约 ∪ 权限矩阵 ∪ roles/ 实现模块。

    文档中标注了外部实现（如 ToolGuardian → tools/gatekeeper.py）的角色不参与
    角色评分，其实现由工具评分报告覆盖。
    """
    docs = parse_protocols()
    modules = {path.stem for path in _ROLES_DIR.glob("*.py") if path.stem != "__init__"}
    external = {key for key, doc in docs.items() if doc.external_impl}
    return sorted((set(docs) | set(DEFAULT_ROLE_PERMS) | modules) - external)


def collect() -> list[RoleScore]:
    """给全部角色打分（按角色名排序，保证同输入同输出）。"""
    docs = parse_protocols()
    return [score_role(role, docs.get(role)) for role in role_universe()]


def coverage_gaps() -> dict[str, list[str]]:
    """三方一致性：文档 / 权限 / 实现 的差集。

    契约中已声明为「权限层」的角色（层级含「权限层」）不设独立模块是设计如此，
    不计入差集。
    """
    docs = parse_protocols()
    doc_keys = set(docs)
    perm_keys = set(DEFAULT_ROLE_PERMS)
    module_keys = {path.stem for path in _ROLES_DIR.glob("*.py") if path.stem != "__init__"}
    external = {key for key, doc in docs.items() if doc.external_impl and key not in perm_keys}
    declared = {
        key for key, doc in docs.items() if doc.layer and "权限层" in doc.layer
    }
    return {
        "文档有但无实现模块（未声明权限层）": sorted(doc_keys - module_keys - external - declared),
        "权限有但无实现模块（未声明权限层）": sorted(perm_keys - module_keys - declared),
        "权限有但文档无章节": sorted(perm_keys - doc_keys),
        "权限无但文档有章节（外部实现）": sorted(external),
        "实现模块有但文档无章节": sorted(module_keys - doc_keys),
        "已声明的权限层角色（无独立模块，设计如此）": sorted(declared),
    }


# ── 报告渲染 ────────────────────────────────────────────────────


def render_markdown(scores: list[RoleScore], gaps: dict[str, list[str]]) -> str:
    """渲染 Markdown 评分报告。"""
    impl_roles = [score for score in scores if score.kind != "permission_only"]
    perm_roles = [score for score in scores if score.kind == "permission_only"]
    full = sum(1 for score in impl_roles if score.total == score.applicable_max)
    safety = [score for score in scores if score.safety_flags]
    average = (
        f"{sum(score.total for score in impl_roles) / len(impl_roles):.2f}" if impl_roles else "N/A"
    )
    lines = [
        "# Agent（角色）评分报告",
        "",
        (
            "> 由 `python -m agent_builder.evaluation.scorecards` 生成；角色集合取"
            "「执行协议文档 ∪ `permissions.py` ∪ `roles/`」三方并集，同输入必得同输出。"
        ),
        "",
        "## 结论摘要",
        "",
        f"- 有实现模块的角色：**{len(impl_roles)}**（满分 {full} 个）",
        f"- 仅权限层角色（无独立模块）：**{len(perm_roles)}** —— 只参与契约/权限两个维度",
        f"- 命中安全项的角色：**{len(safety)}**",
        f"- 实现类角色均分：**{average} / 30**",
        "",
        "## 评分总表（有实现模块的角色）",
        "",
        "| 角色 | 类型 | 契约符合度 | 权限最小化 | 失败语义 | 可观测性 | 测试覆盖 | 依赖清晰度 | 总分 | 建议动作 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for score in impl_roles:
        cells = " | ".join(f"{dim.score}/{dim.max_score}" for dim in score.dimensions)
        lines.append(
            f"| `{score.role}` | {score.kind} | {cells} | **{score.total}** | {'；'.join(score.actions)} |"
        )
    lines += [
        "",
        "## 权限层角色（无独立实现模块，仅评契约与权限）",
        "",
        "| 角色 | 契约符合度 | 权限最小化 | 小计 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for score in perm_roles:
        cells = " | ".join(f"{dim.score}/{dim.max_score}" for dim in score.dimensions)
        lines.append(f"| `{score.role}` | {cells} | **{score.total}/10** | {'；'.join(score.actions)} |")
    lines += [
        "",
        "## 安全项明细（契约违反 / 权限过宽 / 跨层依赖）",
        "",
    ]
    if safety:
        lines += [f"- `{s.role}`：{'；'.join(s.safety_flags)}" for s in safety]
    else:
        lines.append("- 无")
    lines += ["", "## 三方一致性（文档 / 权限 / 实现）", ""]
    for label, values in gaps.items():
        lines.append(f"- {label}：{values or '无'}")
    lines += [
        "",
        "## 维度判定口径",
        "",
        "| 维度 | 判据 |",
        "|---|---|",
        "| 契约符合度 | `docs/execution-protocols.md` 有该角色章节；「授权清单」工具集合 == `permissions.py` 实授；有边界声明与异常处理表 |",
        "| 权限最小化 | 边界声明禁止的动作（写文件/提交代码/执行命令）不得被授权；实授写类工具必须挂 `high_risk_tools`；角色不得悬空 |",
        "| 失败语义 | 执行类：结果声明 pending/failed、代码确产降级结果、含 error 字段；编排类：pending_questions/fallback/FAILED 降级出口 + 契约异常 + 异常处理表 |",
        "| 可观测性 | 执行类：结构化结果类型、字段 ≥ 3、含 status、含 error；编排类：结构化类型 + 契约模型（TaskState/Plan/Step）+ 交付说明 |",
        "| 测试覆盖 | `tests/test_roles_<role>.py` 存在且含 Functional / Edge 用例类、用例 ≥ 10 |",
        "| 依赖清晰度 | 执行类必须通过 `executor_fn` 注入；所有角色不 import `agent_builder.api/tools/llm`；无模块级私有可变状态 |",
        "",
        "## 类型判定",
        "",
        "- **executor**：有 `roles/<role>.py` 实现模块，且契约未声明「不执行」。",
        (
            "- **orchestrator**：有实现模块，但契约边界声明含「不执行」"
            "（只编排/规划/调度/路由），因此不要求注入执行器，"
            "失败语义与可观测性按编排类判据评估。"
        ),
        "- **permission_only**：仅存在于 `permissions.py`，无独立实现模块；只评契约与权限两个维度。",
        (
            "- **外部实现**：文档标注 `**已实现**为 <路径>` 的角色（如 ToolGuardian）"
            "不参与角色评分，其实现由工具评分报告覆盖。"
        ),
        "",
        "## 复现命令",
        "",
        "```powershell",
        "& \"$env:USERPROFILE\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" -m pytest tests/test_evaluation_agents.py -q",
        "& \"$env:USERPROFILE\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" -m agent_builder.evaluation.scorecards",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：生成角色评分报告。"""
    parser = argparse.ArgumentParser(description="Agent（角色）评分")
    parser.add_argument(
        "--out",
        default="docs/reports/agents-scorecard.md",
        help="Markdown 报告输出路径（默认 docs/reports/agents-scorecard.md）",
    )
    parser.add_argument(
        "--json",
        default="docs/reports/agents-scorecard.json",
        help="JSON 报告输出路径（默认 docs/reports/agents-scorecard.json）",
    )
    args = parser.parse_args(argv)

    scores = collect()
    gaps = coverage_gaps()
    payload: dict[str, Any] = {
        "roles": [
            {
                **asdict(score),
                "actions": list(score.actions),
                "safety_flags": list(score.safety_flags),
            }
            for score in scores
        ],
        "coverage_gaps": gaps,
    }
    markdown_path = _PROJECT_ROOT / args.out
    json_path = _PROJECT_ROOT / args.json
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(scores, gaps), encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    flagged = [
        score
        for score in scores
        if score.kind != "permission_only" and score.total < score.applicable_max
    ]
    print(
        f"角色 {len(scores)} 个（权限层 {sum(1 for s in scores if s.kind == 'permission_only')} 个），"
        f"实现类未满分 {len(flagged)} 个，"
        f"安全项 {sum(1 for s in scores if s.safety_flags)} 个"
    )
    print(f"Markdown: {markdown_path}")
    print(f"JSON: {json_path}")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
