"""角色简报 A/B 对照报告 —— 生成 ``docs/reports/role-brief-ab.md``。

用法（在项目根目录）：

    python -m agent_builder.evaluation.role_brief_ab
    python -m agent_builder.evaluation.role_brief_ab --out docs/reports/role-brief-ab.md

本脚本**不调 LLM、不联网**（阶段 1 默认关闭，且测试环境无密钥）：
- 静态部分：三档位定义、逐角色简报字符数与 token 区间估算、单任务成本增量；
- 动态部分：P0/P1 指标表留白，由人工 A/B 实测后回填（见「判定规则」）。

设计约束：同输入必得同输出（不写入时间戳、不引入随机）。
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_builder.api.role_briefs import (
    LLM_ROLES,
    MODE_CORE,
    MODE_FULL,
    MODE_OFF,
    render_brief,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 三档位（off 为现状基线）。
ARMS: tuple[str, ...] = (MODE_OFF, MODE_CORE, MODE_FULL)

# 中文约 0.6–1 token/字（含标点与格式符号）。
TOKEN_PER_CHAR_LOW = 0.6
TOKEN_PER_CHAR_HIGH = 1.0

# 典型任务约 6 次 LLM 调用（每角色各 1 次），用于折算单任务增量。
CALLS_PER_TASK = 6

# P0（质量）四项全测；P1（成本）看增幅。
P0_METRICS: tuple[str, ...] = (
    "编造率（fact_checker 代理）",
    "越权建议数",
    "返工率（TaskEntry.rework_count）",
    "人工盲评（组内互评）",
)
P1_METRICS: tuple[str, ...] = ("token 增幅（cached / uncached 分开）",)

ARM_QUESTIONS: dict[str, str] = {
    MODE_OFF: "现状基线，回答「不注入会怎样」",
    MODE_CORE: "光靠身份约束能拿到多少质量",
    MODE_FULL: "分步流程值不值这个钱",
}


@dataclass(frozen=True, slots=True)
class RoleCost:
    """单个角色的简报成本（按档位）。"""

    role: str
    core_chars: int
    full_chars: int

    @property
    def core_tokens(self) -> tuple[int, int]:
        return _token_range(self.core_chars)

    @property
    def full_tokens(self) -> tuple[int, int]:
        return _token_range(self.full_chars)


def _token_range(chars: int) -> tuple[int, int]:
    """字符数 → (最少 tokens, 最多 tokens)。"""
    return math.ceil(chars * TOKEN_PER_CHAR_LOW), math.ceil(chars * TOKEN_PER_CHAR_HIGH)


def collect_costs() -> list[RoleCost]:
    """逐角色统计简报字符数（off 为 0，故只统计 core / full）。"""
    costs: list[RoleCost] = []
    for role in sorted(LLM_ROLES):
        core = len(render_brief(role, MODE_CORE))
        full = len(render_brief(role, MODE_FULL))
        costs.append(RoleCost(role=role, core_chars=core, full_chars=full))
    return costs


def arm_totals() -> dict[str, tuple[int, int]]:
    """各档位「单任务」的 token 增量区间（每角色各 1 次调用，共 CALLS_PER_TASK 次）。"""
    costs = collect_costs()
    return {
        MODE_OFF: (0, 0),
        MODE_CORE: (
            sum(cost.core_tokens[0] for cost in costs),
            sum(cost.core_tokens[1] for cost in costs),
        ),
        MODE_FULL: (
            sum(cost.full_tokens[0] for cost in costs),
            sum(cost.full_tokens[1] for cost in costs),
        ),
    }


def _fmt_range(value: tuple[int, int]) -> str:
    low, high = value
    return "0" if high == 0 else f"{low}–{high}"


def render_markdown() -> str:
    """渲染报告（确定性；指标表留白待实测回填）。"""
    costs = collect_costs()
    totals = arm_totals()
    arms_text = " / ".join(f"`{arm}`" for arm in ARMS)
    lines = [
        "# 角色简报 A/B 对照报告（阶段 1）",
        "",
        (
            "> 由 `python -m agent_builder.evaluation.role_brief_ab` 生成；本脚本不调 LLM，"
            "静态成本部分可复现，P0/P1 指标需人工实测后回填。"
        ),
        "",
        "## 结论摘要",
        "",
        (
            f"- 覆盖角色：**{len(LLM_ROLES)}** 个会调 LLM 的角色（3 个权限层角色不调 LLM，不覆盖）"
        ),
        f"- 三档位：{arms_text}（默认 `{MODE_OFF}`，环境变量 `AGENT_BUILDER_ROLE_BRIEF`）",
        (
            f"- 单任务 token 增量估算：`off` 0；`core` {_fmt_range(totals[MODE_CORE])}；"
            f"`full` {_fmt_range(totals[MODE_FULL])}"
        ),
        (
            "- 回本唯一路径是减少返工（`conductor.MAX_RETRY=2` / `searcher` 多轮检索）；"
            "简报是稳定前缀，provider 若启用前缀缓存，实际增幅会低于线性估算。"
        ),
        "",
        "## 三档位设计",
        "",
        "| 档位 | 内容 | 回答的问题 |",
        "|---|---|---|",
    ]
    for arm in ARMS:
        content = {
            MODE_OFF: "现状（不注入）",
            MODE_CORE: "职责 + 工具 + 边界（无流程）",
            MODE_FULL: "core + 分步流程",
        }[arm]
        lines.append(f"| `{arm}` | {content} | {ARM_QUESTIONS[arm]} |")

    lines += [
        "",
        "## 静态成本估算（逐角色）",
        "",
        "| 角色 | core 字符 | core tokens | full 字符 | full tokens |",
        "|---|---|---|---|---|",
    ]
    for cost in costs:
        lines.append(
            f"| `{cost.role}` | {cost.core_chars} | {_fmt_range(cost.core_tokens)} | "
            f"{cost.full_chars} | {_fmt_range(cost.full_tokens)} |"
        )
    lines += [
        "",
        (
            f"口径：中文约 {TOKEN_PER_CHAR_LOW}–{TOKEN_PER_CHAR_HIGH} token/字；单任务按"
            f"「{CALLS_PER_TASK} 次调用、每角色各 1 次」折算。注入位置为 system 段最前，"
            "输出契约在后（硬约束不被稀释）。"
        ),
        "",
        "## A/B 指标表（待实测回填）",
        "",
        "### P0（质量，四项全测）",
        "",
        "| 指标 | off | core | full | 判定 |",
        "|---|---|---|---|---|",
    ]
    for metric in P0_METRICS:
        lines.append(f"| {metric} | — | — | — | 待实测 |")
    lines += [
        "",
        "### P1（成本）",
        "",
        "| 指标 | off | core | full | 判定 |",
        "|---|---|---|---|---|",
    ]
    for metric in P1_METRICS:
        lines.append(f"| {metric} | — | — | — | 待实测 |")
    lines += [
        "",
        "### 计量落位",
        "",
        (
            "- `TaskEntry.usage`：`total` + `by_role`（cached / uncached 分开）+ 记录时档位 `mode`；"
            "由 `LLMClient` 采集、`UsageAccumulator` 累计、`routes.py` 写入。"
        ),
        "- `TaskEntry.rework_count`：Router 层重试次数累计（返工率分子）。",
        "- 采集器为 `agent_builder.llm.client.UsageAccumulator`；按角色派生实例共享同一 sink。",
        "",
        "## 判定规则",
        "",
        (
            "- **推广**：P0 四项中至少 2 项显著改善（优先「越权建议数」「编造率」），"
            "**且** P1 成本增幅在阈值内（建议 ≤15%，cached 可抵扣）。"
        ),
        "- **回退**：否则回退，并重新评估「是否只保留 `core` 档」。",
        "- 若 A/B 显示质量没涨，本方案应当被否掉（这是「以成本换质量」的投资）。",
        "",
        "## 复现命令",
        "",
        "```powershell",
        "# 基线（默认关闭，现有测试与运行行为零变化）",
        "& \"$env:USERPROFILE\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" -m pytest -q",
        "",
        "# 开启档位（PowerShell 当前会话）",
        "$env:AGENT_BUILDER_ROLE_BRIEF = 'core'   # 或 'full' / 'off'",
        "",
        "# 生成本报告",
        (
            "& \"$env:USERPROFILE\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" "
            "-m agent_builder.evaluation.role_brief_ab"
        ),
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：生成角色简报 A/B 对照报告。"""
    parser = argparse.ArgumentParser(description="角色简报 A/B 对照报告")
    parser.add_argument(
        "--out",
        default="docs/reports/role-brief-ab.md",
        help="Markdown 报告输出路径（默认 docs/reports/role-brief-ab.md）",
    )
    args = parser.parse_args(argv)
    markdown_path = _PROJECT_ROOT / args.out
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(), encoding="utf-8")
    totals = arm_totals()
    print(f"覆盖角色 {len(LLM_ROLES)} 个；单任务增量估算 core={_fmt_range(totals[MODE_CORE])} / "
          f"full={_fmt_range(totals[MODE_FULL])} tokens")
    print(f"Markdown: {markdown_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
