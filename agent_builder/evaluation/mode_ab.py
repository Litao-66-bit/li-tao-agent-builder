"""执行形态 A/B 跑批：``workflow`` vs ``agentic``（P3）。

用法（需后端已在 localhost:8000 运行，且已通过前端或 curl 提交 LLM 密钥）：

    python -m agent_builder.evaluation.mode_ab
    python -m agent_builder.evaluation.mode_ab --model deepseek-chat --reps 3
    python -m agent_builder.evaluation.mode_ab --arms workflow agentic --temperature 0.3

流程：``POST /tasks`` → ``POST /plan``（带 ``mode``）→ 执行 → ``GET /usage``。
两档的**执行入口不同**，脚本按档分派：

- ``workflow``：``POST /approve`` —— 计划里已给出 ``high_risk_actions``，一次性授权；
- ``agentic``：``POST /run`` 直跑（agentic 下 ``/plan`` 不预分解 DAG，拿不到高风险管理清单）；
  高风险动作「到点暂停」返回 ``pending_approval``，脚本按 ``pending_approval.tools``
  逐次 ``POST /resume`` 放行（最多 ``--max-resume`` 次，防死循环）。

输出：原始 JSON ``docs/reports/mode-ab-runs.json`` + 打印 Markdown 汇总
（**P0 质量 → P1 成本**，用户定的口径优先级）。

设计约束：
- 只用 stdlib，不引入新依赖。
- 不改后端 / 前端 / 现有测试。
- 假设后端已在 localhost:8000 运行，且工作区与 ``--ws`` 一致。
- 跑批产物全部落在工作区内一个**独占目录** ``_mode_ab_out/``（含标记文件）；
  跑批后整目录删除（``shutil.rmtree``），**不碰工作区里任何既有文件**。
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ── HTTP 配置 ──────────────────────────────────────────────

BASE = "http://127.0.0.1:8000"
CLIENT_HEADER = "X-Agent-Builder-Client"
HEADERS = {CLIENT_HEADER: "web", "Content-Type": "application/json"}

# 超时（秒）：执行阶段多步 + LLM 调用可能很慢。
PLAN_TIMEOUT = 120
EXEC_TIMEOUT = 300
GET_TIMEOUT = 30

# ── 工作区：独占目录（跑批产物都落在这里，跑完整目录删除）──

OUTPUT_DIR = "_mode_ab_out"
MARKER = ".mode-ab-owned"


def _ok(status: int) -> bool:
    """HTTP 状态码是否成功（2xx；创建任务返回 201）。"""
    return 200 <= status < 300


def _http_post(path: str, body: dict | None = None, timeout: int = 60) -> tuple[int, dict]:
    """发送 POST 请求，返回 (status, json_dict)。"""
    url = f"{BASE}{path}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, {"error": str(exc)}
    except urllib.error.URLError as exc:
        return 0, {"error": f"连接失败: {exc}"}


def _http_get(path: str, timeout: int = GET_TIMEOUT) -> tuple[int, dict]:
    """发送 GET 请求，返回 (status, json_dict)。"""
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={CLIENT_HEADER: "web"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, {"error": str(exc)}
    except urllib.error.URLError as exc:
        return 0, {"error": f"连接失败: {exc}"}


# ── 工作区准备（独占目录，绝对安全）─────────────────────────

# 调研素材：所有任务都从这里读（相对 ``_mode_ab_out/``）。
SEED_FILES: dict[str, str] = {
    "notes/literature.md": (
        "# 文献笔记\n\n"
        "来源：三篇关于「检索增强生成（RAG）」的论文。\n\n"
        "- 论文 A：提出用稠密向量召回替代关键词匹配，召回率提升明显。\n"
        "- 论文 B：指出向量召回对长尾实体不敏感，建议混合检索。\n"
        "- 论文 C：给出评测口径（答案正确率 / 引用命中率），但样本仅 200 条。\n\n"
        "TODO: 补充第三篇的消融实验数据。\n"
    ),
    "notes/draft.md": (
        "# 初稿要点\n\n"
        "调研论文建议结构：问题背景 → 方法对比 → 评测口径 → 结论与局限。\n\n"
        "TODO: 结论部分还没写。\n"
    ),
    "notes/refs.md": (
        "# 参考文献\n\n"
        "1. 论文 A（2023）\n"
        "2. 论文 B（2024）\n"
        "3. 论文 C（2024）\n"
    ),
}


def assert_workspace_safe(ws_root: Path) -> None:
    """跑批前安全守卫：``_mode_ab_out/`` 已存在但**无本脚本标记** → 中止。

    该目录由本脚本独占（跑完整目录删除）。若已存在同名目录却没有标记文件，
    说明是用户自己的内容，继续跑批会删除它，因此直接中止。
    """
    owned = ws_root / OUTPUT_DIR
    if owned.exists() and not (owned / MARKER).is_file():
        raise SystemExit(
            f"安全中止：{owned} 已存在且不是本脚本创建（缺 {MARKER}），"
            "继续跑批会整目录删除它。请先改名或手动清理。"
        )


def prepare_workspace(ws_root: Path) -> None:
    """准备独占工作目录：写标记 + 调研素材（幂等，已存在则覆盖）。"""
    owned = ws_root / OUTPUT_DIR
    owned.mkdir(parents=True, exist_ok=True)
    (owned / MARKER).write_text("本目录由 mode_ab 跑批脚本创建，跑完即删。\n", encoding="utf-8")
    for rel_path, content in SEED_FILES.items():
        full = owned / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")


def cleanup_workspace(ws_root: Path) -> None:
    """清理：整目录删除（仅当带本脚本标记时，防误删用户内容）。"""
    owned = ws_root / OUTPUT_DIR
    if not owned.exists() or not (owned / MARKER).is_file():
        return
    shutil.rmtree(owned, ignore_errors=True)


# ── 任务集（4 个真实需求；首条是「调研论文 agent」主任务）──

# 设计要点：**所有文件名都写死在需求里**。第一版 R2/R4 用「先列目录、再逐个处理」，
# 逼模型猜文件名（`<file1>.md` 占位符）、把 glob/正则当路径 —— 两档都踩，噪声淹没了形态差异。
# 现在每步只涉及显式路径，测的是「形态本身的稳定性与成本」，不是「模型猜名字的运气」。
TASKS: list[dict[str, str]] = [
    {
        "id": "R1-research-paper-agent",
        "requirement": (
            "帮我做一个「调研论文」agent：它能接收一个论文主题，读取 "
            f"{OUTPUT_DIR}/notes/literature.md、{OUTPUT_DIR}/notes/draft.md、"
            f"{OUTPUT_DIR}/notes/refs.md 三份调研素材，产出一份结构化调研论文。"
            f"请把 agent 的设计方案写入 {OUTPUT_DIR}/research_agent_design.md，"
            f"再把示例调研论文写入 {OUTPUT_DIR}/research_paper.md"
        ),
    },
    {
        "id": "R2-read-only",
        "requirement": (
            f"读取 {OUTPUT_DIR}/notes/literature.md，用三行分别总结三篇论文的结论差异。"
            "只做只读分析，不要写任何文件"
        ),
    },
    {
        "id": "R3-write-file",
        "requirement": (
            f"读取 {OUTPUT_DIR}/notes/literature.md 与 {OUTPUT_DIR}/notes/draft.md，"
            f"合并要点后写入 {OUTPUT_DIR}/merged.md"
        ),
    },
    {
        "id": "R4-copy-todo",
        "requirement": (
            f"找出 {OUTPUT_DIR}/notes/draft.md 中包含「TODO」的行，"
            f"把这些行抄写到 {OUTPUT_DIR}/todo_found.md"
        ),
    },
]


# ── 指标抽取（纯函数，便于单测）─────────────────────────────

# 走到这些状态即视为「完成」（本产品收尾即转 verifying）。
COMPLETED_STATUSES: frozenset[str] = frozenset({"verifying", "completed", "done"})


def extract_metrics(arm: str, final: dict, usage: dict) -> dict:
    """从执行响应（``/approve`` 或 ``/run`` 的 TaskResponse）+ ``/usage`` 抽取指标。

    纯函数、不联网：给定同一份响应必得同一批数字，因此可脱离后端单测。
    """
    exec_results = final.get("execution_results") or []
    loop_state = final.get("loop_state") or {}
    status = str(final.get("status", ""))

    total_tokens = usage.get("total_tokens")
    if total_tokens is None:
        total_tokens = usage.get("total", 0)

    return {
        "task_status": status,
        "completed": 1 if status in COMPLETED_STATUSES else 0,
        "step_count": len(exec_results),
        "done_steps": sum(1 for r in exec_results if r.get("status") == "done"),
        "failed_steps": sum(1 for r in exec_results if r.get("status") == "failed"),
        # 返工：与后端 routes.py 同一算法（execution_results 的 retries 求和）。
        "rework_count": sum(int(r.get("retries", 0) or 0) for r in exec_results),
        "loop_rounds": len(loop_state.get("rounds") or []),
        "loop_stopped_reason": str(loop_state.get("stopped_reason", "")),
        # agentic 档却没进循环 → 说明无密钥回退成了固定工作流，该 run 不能算 agentic。
        "agentic_fell_back": bool(arm == "agentic" and not loop_state),
        "tokens": int(total_tokens) if isinstance(total_tokens, (int, float)) else 0,
    }


# ── 单次跑批 ───────────────────────────────────────────────


def _run_workflow(task_id: str, high_risk: list[str], record: dict) -> tuple[dict, int]:
    """workflow 档：一次性授权计划里的高风险工具后 ``/approve``。"""
    body = {"approved_tools": high_risk} if high_risk else {}
    status, resp = _http_post(f"/tasks/{task_id}/approve", body, timeout=EXEC_TIMEOUT)
    record["exec_status"] = status
    if not _ok(status):
        record["error"] = f"POST /approve 失败 ({status}): {resp}"
    return resp, 0


def _run_agentic(
    task_id: str, high_risk: list[str], max_resume: int, record: dict
) -> tuple[dict, int]:
    """agentic 档：``/run`` 直跑；「到点暂停」按 pending_approval 逐次 ``/resume`` 放行。"""
    body = {"approved_tools": high_risk} if high_risk else {}
    status, resp = _http_post(f"/tasks/{task_id}/run", body, timeout=EXEC_TIMEOUT)
    record["exec_status"] = status
    if not _ok(status):
        record["error"] = f"POST /run 失败 ({status}): {resp}"
        return resp, 0

    rounds = 0
    while resp.get("pending_approval") and rounds < max_resume:
        tools = list((resp.get("pending_approval") or {}).get("tools") or [])
        rounds += 1
        status, resumed = _http_post(
            f"/tasks/{task_id}/resume", {"approved_tools": tools}, timeout=EXEC_TIMEOUT
        )
        if not _ok(status):
            record["error"] = f"POST /resume 失败 ({status}): {resumed}"
            return resp, rounds
        resp = resumed
    return resp, rounds


def run_once(
    task_def: dict[str, str],
    model: str,
    temperature: float,
    arm: str,
    rep: int,
    max_resume: int,
) -> dict:
    """跑一次完整的「创建 → 规划 → 执行 → 取计量」流程。"""
    record: dict = {
        "task": task_def["id"],
        "mode": arm,
        "rep": rep,
        "model": model,
        "temperature": temperature,
        "task_id": "",
        "plan_status": 0,
        "plan_mode": "",
        "exec_status": 0,
        "order": [],
        "pending_questions": [],
        "high_risk_actions": [],
        "execution_results": [],
        "approval_rounds": 0,
        "usage": {},
        "error": None,
    }

    # 1. 创建任务
    status, resp = _http_post("/tasks", {"requirement": task_def["requirement"]})
    if not _ok(status):
        record["error"] = f"POST /tasks 失败 ({status}): {resp}"
        return record
    task_id = resp.get("task_id", "")
    record["task_id"] = task_id

    # 2. 规划（带 mode：agentic 下后端不预分解 DAG）
    plan_body = {
        "use_llm": True,
        "model": model,
        "temperature": temperature,
        "mode": arm,
    }
    status, plan_resp = _http_post(f"/tasks/{task_id}/plan", plan_body, timeout=PLAN_TIMEOUT)
    record["plan_status"] = status
    if not _ok(status):
        record["error"] = f"POST /plan 失败 ({status}): {plan_resp}"
        return record
    record["plan_mode"] = plan_resp.get("mode", "")
    record["order"] = plan_resp.get("order", [])
    record["pending_questions"] = plan_resp.get("pending_questions", [])
    high_risk = list(plan_resp.get("high_risk_actions") or [])
    record["high_risk_actions"] = high_risk

    # 3. 执行（按档分派）
    if arm == "workflow":
        final, rounds = _run_workflow(task_id, high_risk, record)
    else:
        final, rounds = _run_agentic(task_id, high_risk, max_resume, record)
    record["approval_rounds"] = rounds
    if record["error"] is not None:
        return record

    # 4. 取 token 计量
    status, usage_resp = _http_get(f"/tasks/{task_id}/usage")
    record["usage"] = usage_resp if _ok(status) else {"error": f"GET /usage 失败 ({status})"}

    # 5. 指标
    record["execution_results"] = final.get("execution_results", []) or []
    record.update(extract_metrics(arm, final, record["usage"]))
    return record


# ── 汇总（纯函数，便于单测）────────────────────────────────


def _mean(values: list[float]) -> float:
    """均值（空列表返回 0）。"""
    return statistics.fmean(values) if values else 0.0


def _median(values: list[float]) -> float:
    """中位数（空列表返回 0）。"""
    return statistics.median(values) if values else 0.0


def _valid(runs: list[dict], arm: str) -> list[dict]:
    """取该档无错误的 run。"""
    return [r for r in runs if r["mode"] == arm and not r.get("error")]


def summarize_arm(runs: list[dict], arm: str) -> dict:
    """按档聚合（P0 质量 + P1 成本）。"""
    ok = _valid(runs, arm)
    return {
        "runs": len(ok),
        "completion_rate": _mean([r["completed"] for r in ok]),
        "failed_steps": sum(r["failed_steps"] for r in ok),
        "rework": sum(r["rework_count"] for r in ok),
        "loop_rounds_median": _median([r["loop_rounds"] for r in ok]),
        "tokens_median": _median([r["tokens"] for r in ok]),
        "tokens_mean": _mean([r["tokens"] for r in ok]),
    }


def _per_task_table(runs: list[dict], arms: list[str], field: str, fmt: str = "{:.0f}") -> list[str]:
    """逐任务 × 档位的一张 Markdown 均值表。"""
    lines = ["| 任务 | " + " | ".join(arms) + " |", "|---|" + "|".join(["---:"] * len(arms)) + "|"]
    for task in TASKS:
        cells = []
        for arm in arms:
            values = [
                float(r[field]) for r in _valid(runs, arm) if r["task"] == task["id"]
            ]
            cells.append(fmt.format(_mean(values)))
        lines.append(f"| {task['id']} | " + " | ".join(cells) + " |")
    return lines


def render_summary(runs: list[dict], arms: list[str]) -> str:
    """渲染 Markdown 汇总（P0 质量在前、P1 成本在后）。"""
    reps = max((r.get("rep", 0) for r in runs), default=0)
    lines = [
        "# 执行形态 A/B（workflow vs agentic）",
        "",
        (
            f"> 共 {len(runs)} 次运行；{len(TASKS)} 任务 × {len(arms)} 档 × {reps} 次。"
            "口径优先级：**P0 质量 → P1 成本**。"
        ),
        "",
    ]

    # ── P0 质量 ──
    lines += ["## P0 质量", "", "### 完成率（到达 verifying 的比例，越高越好）", ""]
    lines += _per_task_table(runs, arms, "completed", "{:.2f}")
    lines += ["", "### 执行失败步骤数（合计，越低越好）", ""]
    lines += _per_task_table(runs, arms, "failed_steps")
    lines += ["", "### 返工数（retries 合计，越低越好）", ""]
    lines += _per_task_table(runs, arms, "rework_count")

    # ── P1 成本 ──
    lines += ["", "## P1 成本", "", "### token（中位数 / 均值）", ""]
    for arm in arms:
        s = summarize_arm(runs, arm)
        lines.append(
            f"- `{arm}`：中位数 {s['tokens_median']:.0f} / 均值 {s['tokens_mean']:.0f}"
            f"（{s['runs']} 次有效运行）"
        )
    lines += ["", "### agentic 循环轮数（中位数；仅 agentic 档有意义）", ""]
    for arm in arms:
        s = summarize_arm(runs, arm)
        lines.append(f"- `{arm}`：{s['loop_rounds_median']:.1f}")

    # ── 判定（质量优先）──
    lines += ["", "## 判定", ""]
    if len(arms) >= 2:
        a, b = arms[0], arms[1]
        sa, sb = summarize_arm(runs, a), summarize_arm(runs, b)
        lines.append(
            f"- **质量**：完成率 `{b}` {sb['completion_rate']:.0%} vs `{a}` "
            f"{sa['completion_rate']:.0%}；失败步骤 {sb['failed_steps']} vs {sa['failed_steps']}；"
            f"返工 {sb['rework']} vs {sa['rework']}"
        )
        lines.append(
            f"- **成本**：token 中位数 `{b}` {sb['tokens_median']:.0f} vs "
            f"`{a}` {sa['tokens_median']:.0f}"
        )
        if sa["completion_rate"] != sb["completion_rate"]:
            winner = b if sb["completion_rate"] > sa["completion_rate"] else a
            lines.append(f"- **判定**：按质量优先，`{winner}` 完成率更高 → 倾向 `{winner}`")
        elif (sa["failed_steps"] + sa["rework"]) != (sb["failed_steps"] + sb["rework"]):
            loser = b if (sb["failed_steps"] + sb["rework"]) > (sa["failed_steps"] + sa["rework"]) else a
            winner = a if loser == b else b
            lines.append(f"- **判定**：完成率持平，按失败+返工，`{winner}` 更稳 → 倾向 `{winner}`")
        elif sa["tokens_median"] != sb["tokens_median"]:
            winner = b if sb["tokens_median"] < sa["tokens_median"] else a
            lines.append(f"- **判定**：质量持平，按成本，`{winner}` 更省 → 倾向 `{winner}`")
        else:
            lines.append("- **判定**：两档在质量与成本上均无差异")

    # ── 明细 ──
    lines += ["", "## 逐任务明细", ""]
    lines.append("| 任务 | 档 | rep | 状态 | 步数 | 失败 | 返工 | 循环轮数 | 终止原因 | token |")
    lines.append("|---|---|---:|---|---:|---:|---:|---:|---|---:|")
    for r in runs:
        lines.append(
            f"| {r['task']} | {r['mode']} | {r.get('rep', '')} | {r.get('task_status', '')} "
            f"| {r.get('step_count', 0)} | {r.get('failed_steps', 0)} | {r.get('rework_count', 0)} "
            f"| {r.get('loop_rounds', 0)} | {r.get('loop_stopped_reason', '') or '-'} "
            f"| {r.get('tokens', 0)} |"
        )

    lines.append("")
    return "\n".join(lines)


# ── 入口 ───────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="执行形态 A/B 跑批：workflow vs agentic")
    parser.add_argument("--model", default="deepseek-chat", help="LLM 模型名")
    parser.add_argument("--temperature", type=float, default=0.3, help="采样温度")
    parser.add_argument("--reps", type=int, default=3, help="每任务重复次数")
    parser.add_argument(
        "--arms", nargs="+", default=["workflow", "agentic"], help="执行档位（默认两档）"
    )
    parser.add_argument("--max-resume", type=int, default=6, help="agentic 每次跑批最多放行次数")
    parser.add_argument(
        "--out", default="docs/reports/mode-ab-runs.json", help="原始 JSON 输出路径"
    )
    parser.add_argument(
        "--ws", default=None, help="工作区目录（默认项目根，需与后端一致）"
    )
    args = parser.parse_args(argv)

    ws_root = Path(args.ws).resolve() if args.ws else _PROJECT_ROOT
    out_path = _PROJECT_ROOT / args.out

    print(f"工作区: {ws_root}")
    print(f"模型: {args.model} / 温度: {args.temperature} / 档位: {args.arms} / 重复: {args.reps}")
    print(f"输出: {out_path}")
    print(f"后端: {BASE}")
    print()

    assert_workspace_safe(ws_root)

    status, _ = _http_get("/health", timeout=10)
    if not _ok(status):
        print(f"后端不可用（{status}），请先启动后端：")
        print('  $env:PYTHONPATH = ".deps"')
        print('  python -m uvicorn agent_builder.api.app:create_app --factory --port 8000')
        return 1

    all_runs: list[dict] = []
    total = len(TASKS) * len(args.arms) * args.reps
    done = 0

    for task in TASKS:
        for arm in args.arms:
            for rep in range(1, args.reps + 1):
                done += 1
                print(f"[{done}/{total}] {task['id']} / {arm} / rep {rep} ...", end=" ", flush=True)
                prepare_workspace(ws_root)
                t0 = time.time()
                record = run_once(
                    task, args.model, args.temperature, arm, rep, args.max_resume
                )
                elapsed = time.time() - t0
                cleanup_workspace(ws_root)
                all_runs.append(record)
                if record.get("error"):
                    print(f"ERROR ({elapsed:.1f}s): {str(record['error'])[:80]}")
                else:
                    print(
                        f"completed={record['completed']} / 失败 {record['failed_steps']} / "
                        f"循环 {record['loop_rounds']} / {record['task_status']} "
                        f"({elapsed:.1f}s)"
                    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(all_runs, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"\n原始数据: {out_path}")

    print()
    print(render_summary(all_runs, args.arms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
