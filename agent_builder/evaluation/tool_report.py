"""工具评分 —— 以 ``registry.list_tools()`` 为清单做静态 + 元数据一致性检查。

用法（在项目根目录）：

    python -m agent_builder.evaluation.tool_report
    python -m agent_builder.evaluation.tool_report --out docs/reports/tools-scorecard.md

评分维度（各 0–5，满分 30）：
1. 元数据完整性 —— ToolSpec 七要素齐全且取值合法
2. Schema 质量 —— type=object / properties / required / additionalProperties:false / 属性有描述
3. 最小权限 —— 无越权声明（allowed_roles 不得超出实授）、无悬空工具
4. 风险分级 —— 写/提交/回滚类必须非 low 且进审批门；high 必须进审批门
5. 错误语义 —— 走契约异常、不吞异常、错误语义有文档
6. 测试覆盖 —— tests/test_tools_<name>.py 覆盖成功/失败（有数值参数时还需边界）

口径说明（避免误判）：
- ``permissions.py`` 是**执行真源**（门卫据此放行）；``spec.allowed_roles`` 是工具侧名义声明。
  因此「声明了但未实授」＝越权声明（安全项）；「实授但未声明」＝声明不完备（仅记备注，不扣分）。
- 审批门由 ``permissions.py`` 的 ``high_risk_tools`` 决定，``spec.risk_level`` 是元数据；
  二者不一致即为风险等级错标。
- 「吞异常」只统计 ``except: pass`` / ``except Exception: pass`` 这类兜底吞掉；
  对 ``except OSError: pass`` 式的显式降级（如 netns 不可用回退命令黑名单）不算。

设计约束：不联网、不执行任何工具实现、同输入必得同输出（不写入时间戳）。
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_builder.tools import registry
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS
from agent_builder.tools.spec import VALID_COST_BANDS, VALID_RISK_LEVELS, ToolSpec

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _PROJECT_ROOT / "tests"
_DOCS_TOOLS = _PROJECT_ROOT / "docs" / "tools.md"

# 写/提交/回滚类：必须至少 medium，且必须进审批门（high_risk_tools）。
MUTATING_TOOLS = frozenset({"file_write", "git_commit", "rollback"})
# 执行类：会跑外部命令/代码，不应标为 low。
EXEC_TOOLS = frozenset({"sandbox_run", "test_run"})

MAX_REASONABLE_TIMEOUT = 300.0
MIN_DESCRIPTION_CHARS = 10

# 测试用例命名的类别关键词（匹配测试函数名，不区分大小写）。
_FAILURE_KEYWORDS = (
    "empty",
    "invalid",
    "denied",
    "reject",
    "missing",
    "nonexistent",
    "too_long",
    "bad",
    "wrong",
    "negative",
    "zero",
    "outside",
    "no_approval",
    "unapproved",
    "unregistered",
    "error",
    "fail",
    "conflict",
)
_EDGE_KEYWORDS = (
    "truncat",
    "capped",
    "huge",
    "limit",
    "max_",
    "min_",
    "unicode",
    "boundary",
    "edge",
    "positive",
)


@dataclass(frozen=True, slots=True)
class Dimension:
    """单个评分维度。"""

    name: str
    score: int
    max_score: int
    note: str


@dataclass(frozen=True, slots=True)
class ToolScore:
    """单个工具的评分卡。"""

    tool: str
    risk_level: str
    timeout_s: float
    cost_band: str
    declared_roles: tuple[str, ...]
    granted_roles: tuple[str, ...]
    dimensions: tuple[Dimension, ...]
    total: int
    safety_flags: tuple[str, ...]
    actions: tuple[str, ...]


# ── 权限矩阵视图 ────────────────────────────────────────────────


def _grants_by_tool() -> dict[str, tuple[str, ...]]:
    """工具 → 在 permissions.py 中被实授的角色（执行真源）。"""
    grants: dict[str, list[str]] = {}
    for role, perm in DEFAULT_ROLE_PERMS.items():
        for tool in perm.allowed_tools:
            grants.setdefault(tool, []).append(role)
    return {tool: tuple(sorted(roles)) for tool, roles in grants.items()}


def _approval_gated_tools() -> set[str]:
    """被任一角色列入 high_risk_tools（需审批）的工具集合。"""
    gated: set[str] = set()
    for perm in DEFAULT_ROLE_PERMS.values():
        gated.update(perm.high_risk_tools)
    return gated


# ── 六个维度的评分 ──────────────────────────────────────────────


def _score_metadata(spec: ToolSpec) -> Dimension:
    missing: list[str] = []
    if not spec.name.strip():
        missing.append("name")
    if len(spec.description.strip()) < MIN_DESCRIPTION_CHARS:
        missing.append("description")
    if not isinstance(spec.parameters, dict) or not spec.parameters:
        missing.append("parameters")
    if spec.risk_level not in VALID_RISK_LEVELS:
        missing.append("risk_level")
    if not (0 < spec.timeout_s <= MAX_REASONABLE_TIMEOUT):
        missing.append("timeout_s")
    if spec.cost_band not in VALID_COST_BANDS:
        missing.append("cost_band")
    if not spec.allowed_roles:
        missing.append("allowed_roles")
    return Dimension(
        name="元数据完整性",
        score=max(0, 5 - len(missing)),
        max_score=5,
        note="七要素齐全" if not missing else f"缺失/非法: {missing}",
    )


def _score_schema(spec: ToolSpec) -> Dimension:
    params = spec.parameters if isinstance(spec.parameters, dict) else {}
    issues: list[str] = []
    if params.get("type") != "object":
        issues.append("type 不是 object")
    props = params.get("properties")
    if not isinstance(props, dict) or not props:
        issues.append("properties 缺失或为空")
    if "required" not in params:
        issues.append("缺 required 声明")
    if params.get("additionalProperties") is not False:
        issues.append("additionalProperties 未显式置为 false")
    undocumented = [
        key
        for key, value in (props or {}).items()
        if not (isinstance(value, dict) and str(value.get("description", "")).strip())
    ]
    if undocumented:
        issues.append(f"属性缺 description: {undocumented}")
    return Dimension(
        name="Schema 质量",
        score=max(0, 5 - len(issues)),
        max_score=5,
        note="契约完备" if not issues else "; ".join(issues),
    )


def _score_permissions(spec: ToolSpec, granted: tuple[str, ...]) -> tuple[Dimension, list[str]]:
    declared = set(spec.allowed_roles)
    granted_set = set(granted)
    over = sorted(declared - granted_set)  # 声明了但未实授 → 越权声明
    under = sorted(granted_set - declared)  # 实授但未声明 → 声明不完备
    score = 5
    flags: list[str] = []
    notes: list[str] = []
    if over:
        score -= 2
        flags.append(f"越权声明：allowed_roles 含未实授角色 {over}")
    if not granted_set:
        score -= 2
        flags.append("悬空工具：permissions.py 中没有任何角色被授予该工具")
    if under:
        notes.append(f"声明不完备（实授但未声明，非安全项）: {under}")
    detail = f"声明={sorted(declared)}; 实授={sorted(granted_set)}"
    return (
        Dimension(
            name="最小权限",
            score=max(0, score),
            max_score=5,
            note="; ".join([detail, *notes]),
        ),
        flags,
    )


def _score_risk(spec: ToolSpec, gated: set[str]) -> tuple[Dimension, list[str]]:
    score = 5
    flags: list[str] = []
    if spec.risk_level == "high" and spec.name not in gated:
        score -= 2
        flags.append("风险等级错标：risk_level=high 但不在任何角色的 high_risk_tools（审批门失效）")
    if spec.name in MUTATING_TOOLS:
        if spec.risk_level == "low":
            score -= 2
            flags.append("风险等级错标：写/提交/回滚类工具被标为 low")
        if spec.name not in gated:
            score -= 2
            flags.append("审批门缺失：写/提交/回滚类工具未列入任何角色的 high_risk_tools")
    if spec.name in EXEC_TOOLS and spec.risk_level == "low":
        score -= 1
        flags.append("风险等级偏低：执行类工具被标为 low（同类 sandbox_run 为 medium）")
    gait = "已挂" if spec.name in gated else "不需要"
    note = "; ".join(flags) if flags else f"risk={spec.risk_level}; 审批门={gait}"
    return Dimension(name="风险分级正确", score=max(0, score), max_score=5, note=note), flags


def _score_errors(spec: ToolSpec) -> Dimension:
    source_path = _impl_source_path(spec.name)
    if source_path is None:
        return Dimension(name="错误语义", score=0, max_score=5, note="无法定位实现源码")
    source = source_path.read_text(encoding="utf-8")
    score = 5
    notes: list[str] = []
    contract = [t for t in ("validation_error", "tool_error", "permission_error") if t in source]
    if not contract:
        score -= 1
        notes.append("未见契约异常构造（确认是否由上游统一映射）")
    if re.search(r"except(\s+Exception)?\s*:\s*\n\s*pass\b", source):
        score -= 2
        notes.append("检测到兜底吞异常（except: pass / except Exception: pass）")
    builtin_raises = re.findall(r"raise (?:ValueError|RuntimeError|KeyError)\(", source)
    if builtin_raises:
        score -= min(2, len(builtin_raises))
        notes.append(f"直接抛内置异常 {len(builtin_raises)} 处，未走契约异常")
    if "Raises:" not in source:
        score -= 1
        notes.append("缺 Raises 段（错误语义未文档化）")
    return Dimension(
        name="错误语义",
        score=max(0, score),
        max_score=5,
        note="契约异常映射完整" if not notes else "; ".join(notes),
    )


def _has_numeric_param(spec: ToolSpec) -> bool:
    props = spec.parameters.get("properties") if isinstance(spec.parameters, dict) else None
    return any(
        isinstance(value, dict) and value.get("type") in ("integer", "number")
        for value in (props or {}).values()
    )


def _test_sections(text: str) -> dict[str, list[str]]:
    """拆分测试文件：类名 → 该类下的测试函数名（模块级函数归入空串键）。

    本仓库的约定是 ``TestXxxFunctional`` / ``TestXxxEdge`` / ``TestXxxSecurity`` /
    ``TestXxxRegistration``，按类判定比按函数名判定更稳。
    """
    sections: dict[str, list[str]] = {"": []}
    current = ""
    for line in text.splitlines():
        matched_class = re.match(r"class (\w+)", line)
        if matched_class:
            current = matched_class.group(1)
            sections.setdefault(current, [])
            continue
        matched_fn = re.match(r"\s+def (test_\w+)\(", line)
        if matched_fn:
            sections.setdefault(current, []).append(matched_fn.group(1))
    return sections


def _score_tests(spec: ToolSpec) -> Dimension:
    test_file = _TESTS_DIR / f"test_tools_{spec.name}.py"
    if not test_file.exists():
        return Dimension(name="测试覆盖", score=0, max_score=5, note=f"缺 {test_file.name}")
    sections = _test_sections(test_file.read_text(encoding="utf-8"))
    classes = {name.lower() for name in sections}
    names = [fn for fns in sections.values() for fn in fns]
    has_success = any("functional" in name for name in classes) or any(
        not _matches(fn, _FAILURE_KEYWORDS) and not _matches(fn, _EDGE_KEYWORDS) for fn in names
    )
    has_failure = any("security" in name for name in classes) or any(
        _matches(fn, _FAILURE_KEYWORDS) for fn in names
    )
    has_edge = any("edge" in name for name in classes) or any(
        _matches(fn, _EDGE_KEYWORDS) for fn in names
    )
    need_edge = _has_numeric_param(spec)
    missing: list[str] = []
    score = 2
    score += _tick(has_success, "成功", missing)
    score += _tick(has_failure, "失败", missing)
    score += _tick(has_edge or not need_edge, "边界", missing)
    note = f"{len(names)} 个用例" if not missing else f"缺 {missing} 类用例（共 {len(names)} 个）"
    return Dimension(name="测试覆盖", score=min(5, score), max_score=5, note=note)


def _tick(ok: bool, label: str, missing: list[str]) -> int:
    """命中记 1 分，未命中记入缺失清单。"""
    if ok:
        return 1
    missing.append(label)
    return 0


def _matches(name: str, keywords: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(keyword in lowered for keyword in keywords)


def _impl_source_path(tool: str) -> Path | None:
    entry = registry.get(tool)
    if entry is None:
        return None
    _, impl = entry
    path = inspect.getsourcefile(impl)
    return Path(path) if path else None


# ── 汇总 ────────────────────────────────────────────────────────


def _suggestions(dimensions: tuple[Dimension, ...], safety_flags: tuple[str, ...]) -> tuple[str, ...]:
    """按优先级（安全 → 错误语义 → 测试 → Schema → 权限备注 → 元数据）给出建议动作。"""
    actions: list[str] = []
    if safety_flags:
        actions.append("【安全·优先】" + "；".join(safety_flags))
    label = {
        "错误语义": "【错误语义】",
        "测试覆盖": "【测试】",
        "Schema 质量": "【Schema】",
        "最小权限": "【权限·备注】",
        "元数据完整性": "【元数据】",
    }
    by_name = {dim.name: dim for dim in dimensions}
    for name, prefix in label.items():
        dim = by_name.get(name)
        if dim is not None and dim.score < dim.max_score:
            actions.append(prefix + dim.note)
    return tuple(actions) or ("无",)


def score_tool(tool: str, granted_by_tool: dict[str, tuple[str, ...]], gated: set[str]) -> ToolScore:
    """给单个工具打分。"""
    entry = registry.get(tool)
    if entry is None:  # pragma: no cover - registry 与 list_tools 同源
        raise KeyError(f"工具 {tool!r} 不在注册表中")
    spec, _ = entry
    granted = granted_by_tool.get(tool, ())
    perm_dim, perm_flags = _score_permissions(spec, granted)
    risk_dim, risk_flags = _score_risk(spec, gated)
    dimensions = (
        _score_metadata(spec),
        _score_schema(spec),
        perm_dim,
        risk_dim,
        _score_errors(spec),
        _score_tests(spec),
    )
    flags = tuple(sorted(set(perm_flags) | set(risk_flags)))
    return ToolScore(
        tool=tool,
        risk_level=spec.risk_level,
        timeout_s=spec.timeout_s,
        cost_band=spec.cost_band,
        declared_roles=tuple(sorted(spec.allowed_roles)),
        granted_roles=granted,
        dimensions=dimensions,
        total=sum(d.score for d in dimensions),
        safety_flags=flags,
        actions=_suggestions(dimensions, flags),
    )


def collect() -> list[ToolScore]:
    """给全部已注册工具打分（按工具名排序，保证同输入同输出）。"""
    granted_by_tool = _grants_by_tool()
    gated = _approval_gated_tools()
    return [score_tool(tool, granted_by_tool, gated) for tool in registry.list_tools()]


# ── 文档漂移 ────────────────────────────────────────────────────


def doc_drift() -> dict[str, list[str]]:
    """核对 docs/tools.md 与注册表：工具覆盖 + 各工具角色声明。

    角色行只取正向声明：先剥离全角括号内容，避免把「operator 不可调用」这类
    否定说明误当成授权声明。
    """
    text = _DOCS_TOOLS.read_text(encoding="utf-8")
    registered = set(registry.list_tools())
    sections = dict(re.findall(r"^### (\w+)\n((?:(?!^### ).)*)", text, re.MULTILINE | re.DOTALL))
    missing_docs = sorted(registered - set(sections))
    known_roles = set(DEFAULT_ROLE_PERMS)
    grants = _grants_by_tool()
    role_drift: list[str] = []
    for tool in sorted(registered & set(sections)):
        match = re.search(r"\*\*角色\*\*：(.+)", sections[tool])
        if not match:
            continue
        declared = re.sub(r"（[^）]*）", "", match.group(1))
        claimed = {role for role in known_roles if role in declared}
        actual = set(grants.get(tool, ()))
        if claimed and claimed != actual:
            role_drift.append(f"{tool}: 文档={sorted(claimed)} 实授={sorted(actual)}")
    return {"未收录进文档": missing_docs, "角色声明漂移": sorted(role_drift)}


# ── 报告渲染 ────────────────────────────────────────────────────


def render_markdown(scores: list[ToolScore], drift: dict[str, list[str]]) -> str:
    """渲染 Markdown 评分报告。"""
    total = len(scores)
    full = sum(1 for s in scores if s.total == 30)
    safety = [s for s in scores if s.safety_flags]
    average = f"{sum(s.total for s in scores) / total:.2f}" if total else "N/A"
    lines = [
        "# 工具评分报告",
        "",
        (
            "> 由 `python -m agent_builder.evaluation.tool_report` 生成；"
            "清单取自 `registry.list_tools()`（运行时真源），同输入必得同输出。"
        ),
        "",
        "## 结论摘要",
        "",
        f"- 工具总数：**{total}**",
        f"- 满分（30/30）：**{full}**",
        f"- 命中安全项的工具：**{len(safety)}**",
        f"- 均分：**{average} / 30**",
        "",
        "## 评分总表",
        "",
        "| 工具 | 元数据 | Schema | 最小权限 | 风险分级 | 错误语义 | 测试 | 总分 | 建议动作 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for score in scores:
        cells = " | ".join(f"{d.score}/{d.max_score}" for d in score.dimensions)
        lines.append(f"| `{score.tool}` | {cells} | **{score.total}** | {'；'.join(score.actions)} |")
    lines += ["", "## 安全项明细（风险等级错标 / 越权声明 / 悬空工具）", ""]
    if safety:
        lines += [f"- `{s.tool}`：{'；'.join(s.safety_flags)}" for s in safety]
    else:
        lines.append("- 无")
    lines += ["", "## 文档漂移（docs/tools.md ↔ registry）", ""]
    lines.append(f"- 未收录进文档的工具：{drift['未收录进文档'] or '无'}")
    lines.append("- 角色声明与 permissions.py 不一致：")
    if drift["角色声明漂移"]:
        lines += [f"  - {item}" for item in drift["角色声明漂移"]]
    else:
        lines.append("  - 无")
    lines += [
        "",
        "## 维度判定口径",
        "",
        "| 维度 | 判据 |",
        "|---|---|",
        "| 元数据完整性 | ToolSpec 七要素齐全且取值合法（name/description/parameters/risk_level/timeout_s/cost_band/allowed_roles） |",
        "| Schema 质量 | `type=object` + `properties` 非空 + `required` 存在 + `additionalProperties=false` + 每个属性有 description |",
        "| 最小权限 | 无越权声明（`allowed_roles` ⊆ `permissions.py` 实授）、无悬空工具；「实授但未声明」仅记备注 |",
        "| 风险分级正确 | 写/提交/回滚类必须非 low 且进 `high_risk_tools`；`risk_level=high` 必须进审批门 |",
        "| 错误语义 | 走 `validation_error`/`tool_error`/`permission_error`，无兜底吞异常，有 Raises 文档 |",
        "| 测试覆盖 | `tests/test_tools_<name>.py` 覆盖成功/失败，且含数值参数时覆盖边界 |",
        "",
        "## 真源与边界",
        "",
        (
            "- **执行真源**：`agent_builder/tools/permissions.py` 的 "
            "`allowed_tools` / `high_risk_tools`（门卫据此放行与要求审批）。"
        ),
        "- **工具清单真源**：`registry.list_tools()`。",
        (
            "- **元数据真源**：各 impl 模块内的 `ToolSpec`；`spec.risk_level` 与 "
            "`spec.allowed_roles` 是声明性元数据，不参与运行时判定，"
            "因此二者与执行真源的一致性靠本报告与回归测试守住。"
        ),
        "",
        "## 复现命令",
        "",
        "```powershell",
        "& \"$env:USERPROFILE\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" -m pytest tests/test_evaluation_tools.py -q",
        "& \"$env:USERPROFILE\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" -m agent_builder.evaluation.tool_report",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：生成工具评分报告。"""
    parser = argparse.ArgumentParser(description="工具评分")
    parser.add_argument(
        "--out",
        default="docs/reports/tools-scorecard.md",
        help="Markdown 报告输出路径（默认 docs/reports/tools-scorecard.md）",
    )
    parser.add_argument(
        "--json",
        default="docs/reports/tools-scorecard.json",
        help="JSON 报告输出路径（默认 docs/reports/tools-scorecard.json）",
    )
    args = parser.parse_args(argv)

    scores = collect()
    drift = doc_drift()
    payload: dict[str, Any] = {
        "tools": [
            {
                **asdict(score),
                "actions": list(score.actions),
                "safety_flags": list(score.safety_flags),
            }
            for score in scores
        ],
        "doc_drift": drift,
    }
    markdown_path = _PROJECT_ROOT / args.out
    json_path = _PROJECT_ROOT / args.json
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(scores, drift), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    flagged = [s for s in scores if s.total < 30]
    print(
        f"工具 {len(scores)} 个，未满分 {len(flagged)} 个，"
        f"安全项 {sum(1 for s in scores if s.safety_flags)} 个，"
        f"文档角色漂移 {len(drift['角色声明漂移'])} 条"
    )
    print(f"Markdown: {markdown_path}")
    print(f"JSON: {json_path}")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
