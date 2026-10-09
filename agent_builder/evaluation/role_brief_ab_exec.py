"""执行阶段 off vs core 角色简报 A/B 对照跑批脚本。

用法（需后端已在 localhost:8000 运行，且已通过前端或 curl 提交 LLM 密钥）：

    python -m agent_builder.evaluation.role_brief_ab_exec
    python -m agent_builder.evaluation.role_brief_ab_exec --model deepseek-chat --reps 3
    python -m agent_builder.evaluation.role_brief_ab_exec --arms off core --temperature 0.3

本脚本通过 HTTP API 完成「创建任务 → /plan（带 role_brief）→ /approve → 取 rework_count 和 usage」
的完整流程，输出原始 JSON（``docs/reports/role-brief-ab-exec-runs.json``）和 Markdown 汇总（stdout）。

设计约束：
- 只用 stdlib，不引入新依赖。
- 不改后端、前端、现有测试。
- 假设后端已在 localhost:8000 运行。
- 每次跑批前准备工作区测试文件，跑批后清理。

返工率取数：``/approve`` 返回的 ``TaskResponse.execution_results`` 含
``StepResult.retries``，客户端求和即得 ``rework_count``（与后端 ``routes.py`` 的算法一致）。
"""

from __future__ import annotations

import argparse
import json
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

# 超时（秒）：approve 多步执行 + LLM 调用可能很慢。
PLAN_TIMEOUT = 120
APPROVE_TIMEOUT = 300
GET_TIMEOUT = 30


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


# ── 工作区准备 ─────────────────────────────────────────────

# 初始工作区文件（5 个 .md），覆盖写入确保幂等。
WORKSPACE_FILES: dict[str, str] = {
    "notes/a.md": (
        "# 笔记 A\n\n"
        "这是第一个笔记文件。\n\n"
        "TODO: 需要补充更多内容。\n\n"
        "内容来自会议记录，涉及项目初期讨论。"
    ),
    "notes/b.md": (
        "# 笔记 B\n\n"
        "这是第二个笔记文件。\n\n"
        "内容来自项目文档，记录了架构设计。"
    ),
    "notes/c.md": (
        "# 笔记 C\n\n"
        "这是第三个笔记文件。\n\n"
        "TODO: 待审核。\n\n"
        "内容来自用户反馈，需要整理。"
    ),
    "notes/summary.md": (
        "# 旧摘要\n\n"
        "这是旧的摘要内容，需要被覆盖。"
    ),
    "notes/old.md": (
        "# 旧文件\n\n"
        "这个文件应该被删除。"
    ),
}

# 初始文件集合（用于清理时识别「脚本生成的」文件）。
_INITIAL_FILES: frozenset[str] = frozenset(WORKSPACE_FILES.keys())


def assert_workspace_safe(ws_root: Path) -> None:
    """跑批前安全守卫：``notes/`` 若已存在非脚本文件，则拒绝执行以免误删。

    本脚本的 ``cleanup_workspace`` 会删除 ``notes/`` 下的产出文件；若该目录
    已存在用户自己的内容，删除会造成数据丢失。首次跑批（无 ``notes/``）或
    仅含本脚本初始文件时放行，否则中止并提示改用隔离的 ``--ws``。
    """
    notes_dir = ws_root / "notes"
    if not notes_dir.is_dir():
        return
    unknown = sorted(
        item.name for item in notes_dir.iterdir()
        if item.is_file() and f"notes/{item.name}" not in _INITIAL_FILES
    )
    if unknown:
        raise SystemExit(
            f"安全中止：工作区 {notes_dir} 已存在非脚本文件，"
            f"继续跑批会将其删除：{unknown}\n"
            f"请改用 --ws 指向隔离目录，或先手动备份/清理该目录。"
        )


def prepare_workspace(ws_root: Path) -> None:
    """在工作区写入初始测试文件（幂等：已存在则覆盖）。"""
    notes_dir = ws_root / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in WORKSPACE_FILES.items():
        full = ws_root / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")


def cleanup_workspace(ws_root: Path) -> None:
    """清理工作区：只删除本脚本生成的文件，然后恢复初始文件。

    安全约束：只删 ``notes/`` 下的**常规文件**，不递归删子目录（避免误删
    用户内容）；目录本身保留。跑批前已由 ``assert_workspace_safe`` 把关。
    """
    notes_dir = ws_root / "notes"
    if not notes_dir.is_dir():
        return
    for item in notes_dir.iterdir():
        if not item.is_file():
            continue
        rel = f"notes/{item.name}"
        if rel not in _INITIAL_FILES:
            item.unlink()
    # 恢复初始文件内容（防止执行阶段改写了初始文件）
    prepare_workspace(ws_root)


# ── 任务集 ────────────────────────────────────────────────

TASKS: list[dict[str, str]] = [
    {
        "id": "E1-list-read",
        "requirement": (
            "列出 notes/ 下所有 .md 文件，读取 notes/a.md 的完整内容，"
            "把文件列表和 a.md 内容汇总写入 notes/result.md"
        ),
    },
    {
        "id": "E2-write-overwrite",
        "requirement": (
            "读取 notes/a.md 和 notes/b.md 的内容，合并后写入 notes/summary.md"
            "（必须覆盖旧内容）"
        ),
    },
    {
        "id": "E3-search-todo",
        "requirement": (
            "在 notes/ 目录下搜索包含「TODO」的段落，"
            "把匹配的行汇总写入 notes/todo-found.md"
        ),
    },
    {
        "id": "E4-delete-file",
        "requirement": "删除 notes/old.md 文件",
    },
    {
        "id": "E5-rename-file",
        "requirement": "把 notes/a.md 改名为 notes/a_renamed.md",
    },
]


# ── 单次跑批 ───────────────────────────────────────────────


def run_once(
    task_def: dict[str, str],
    model: str,
    temperature: float,
    arm: str,
    rep: int,
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
        "approve_status": 0,
        "task_status": "",
        "order": [],
        "pending_questions": [],
        "high_risk_actions": [],
        "execution_results": [],
        "rework_count": 0,
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

    # 2. 规划
    plan_body = {
        "use_llm": True,
        "model": model,
        "temperature": temperature,
        "role_brief": arm,
    }
    status, plan_resp = _http_post(f"/tasks/{task_id}/plan", plan_body, timeout=PLAN_TIMEOUT)
    record["plan_status"] = status
    if not _ok(status):
        record["error"] = f"POST /plan 失败 ({status}): {plan_resp}"
        return record
    record["order"] = plan_resp.get("order", [])
    record["pending_questions"] = plan_resp.get("pending_questions", [])
    # 逐工具授权：高风险（写/删除类）步骤需显式授权才放行；清单由 /plan 服务端给出。
    high_risk = list(plan_resp.get("high_risk_actions") or [])
    record["high_risk_actions"] = high_risk

    # 3. 执行
    approve_body = {"approved_tools": high_risk} if high_risk else {}
    status, approve_resp = _http_post(
        f"/tasks/{task_id}/approve", approve_body, timeout=APPROVE_TIMEOUT,
    )
    record["approve_status"] = status
    if not _ok(status):
        record["error"] = f"POST /approve 失败 ({status}): {approve_resp}"
        return record
    record["task_status"] = approve_resp.get("status", "")
    exec_results = approve_resp.get("execution_results", [])
    record["execution_results"] = exec_results
    # 返工计量：与后端 routes.py 算法一致。
    record["rework_count"] = sum(int(r.get("retries", 0)) for r in exec_results)

    # 4. 取 token 计量
    status, usage_resp = _http_get(f"/tasks/{task_id}/usage")
    if _ok(status):
        record["usage"] = usage_resp
    else:
        record["usage"] = {"error": f"GET /usage 失败 ({status})"}

    return record


# ── 汇总 ───────────────────────────────────────────────────


def _median(values: list[int]) -> float:
    """中位数（空列表返回 0）。"""
    return statistics.median(values) if values else 0.0


def _mean(values: list[int]) -> float:
    """均值（空列表返回 0）。"""
    return statistics.fmean(values) if values else 0.0


def render_summary(runs: list[dict], arms: list[str]) -> str:
    """渲染 Markdown 汇总表（供回填 role-brief-ab.md）。"""
    lines = [
        "# 执行阶段 off vs core A/B（返工率补测）",
        "",
        (f"> 共 {len(runs)} 次运行；{len(TASKS)} 任务 × {len(arms)} 档 × "
         f"{max(r.get('rep', 0) for r in runs) if runs else 0} 次。"),
        "",
    ]

    # 按任务分档汇总
    lines += ["## 逐任务返工率（rework_count 均值 / 中位数）", ""]
    lines.append("| 任务 | " + " | ".join(
        f"{arm} 均值" for arm in arms
    ) + " | " + " | ".join(
        f"{arm} 中位数" for arm in arms
    ) + " |")
    lines.append("|---|" + "|".join(["---:"] * len(arms) * 2) + "|")
    for task in TASKS:
        tid = task["id"]
        means = []
        medians = []
        for arm in arms:
            reworks = [
                r["rework_count"] for r in runs
                if r["task"] == tid and r["mode"] == arm and r.get("error") is None
            ]
            means.append(f"{_mean(reworks):.2f}")
            medians.append(f"{_median(reworks):.1f}")
        lines.append(
            f"| {tid} | " + " | ".join(means) + " | " + " | ".join(medians) + " |"
        )

    # 合计
    lines += ["", "## 合计", ""]
    for arm in arms:
        reworks = [
            r["rework_count"] for r in runs
            if r["mode"] == arm and r.get("error") is None
        ]
        total = sum(reworks)
        lines.append(f"- `{arm}`: 总返工 {total} / {len(reworks)} 次运行；"
                     f"均值 {_mean(reworks):.2f}；中位数 {_median(reworks):.1f}")

    # 成本（token）
    lines += ["", "## 成本（token）", ""]
    for arm in arms:
        tokens = []
        for r in runs:
            if r["mode"] != arm or r.get("error") is not None:
                continue
            usage = r.get("usage", {})
            total_tokens = usage.get("total_tokens") or usage.get("total", 0)
            if isinstance(total_tokens, (int, float)):
                tokens.append(int(total_tokens))
        if tokens:
            lines.append(f"- `{arm}`: token 中位数 {_median(tokens):.0f} / 均值 {_mean(tokens):.0f}")

    # 执行失败步骤数
    lines += ["", "## 执行失败步骤数（status != done）", ""]
    lines.append("| 任务 | " + " | ".join(f"{arm}" for arm in arms) + " |")
    lines.append("|---|" + "|".join(["---:"] * len(arms)) + "|")
    for task in TASKS:
        tid = task["id"]
        fails = []
        for arm in arms:
            count = 0
            for r in runs:
                if r["task"] != tid or r["mode"] != arm or r.get("error") is not None:
                    continue
                for step in r.get("execution_results", []):
                    if step.get("status") != "done":
                        count += 1
            fails.append(str(count))
        lines.append(f"| {tid} | " + " | ".join(fails) + " |")

    # 判定
    lines += ["", "## 判定", ""]
    if len(arms) >= 2:
        off_reworks = [
            r["rework_count"] for r in runs
            if r["mode"] == arms[0] and r.get("error") is None
        ]
        core_reworks = [
            r["rework_count"] for r in runs
            if r["mode"] == arms[1] and r.get("error") is None
        ]
        off_mean = _mean(off_reworks)
        core_mean = _mean(core_reworks)
        if off_mean > 0:
            delta = (core_mean - off_mean) / off_mean * 100
            lines.append(
                f"- `{arms[1]}` 较 `{arms[0]}` 返工率 {delta:+.1f}%"
                f"（{off_mean:.2f} → {core_mean:.2f}）"
            )
            if delta < -15:
                lines.append(f"- **判定**：`{arms[1]}` 返工率降幅 >15%，显著改善")
            elif delta < 0:
                lines.append(f"- **判定**：`{arms[1]}` 返工率有改善但未达 15% 阈值")
            else:
                lines.append(f"- **判定**：`{arms[1]}` 返工率未改善")
        else:
            lines.append(f"- `{arms[0]}` 返工率为 0，无法计算降幅")

    lines.append("")
    return "\n".join(lines)


# ── 入口 ───────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="执行阶段 off vs core 角色简报 A/B 对照跑批",
    )
    parser.add_argument("--model", default="deepseek-chat", help="LLM 模型名")
    parser.add_argument("--temperature", type=float, default=0.3, help="采样温度")
    parser.add_argument("--reps", type=int, default=3, help="每任务重复次数")
    parser.add_argument(
        "--arms", nargs="+", default=["off", "core"],
        help="档位（默认 off core）",
    )
    parser.add_argument(
        "--out", default="docs/reports/role-brief-ab-exec-runs.json",
        help="原始 JSON 输出路径",
    )
    parser.add_argument(
        "--ws", default=None,
        help="工作区目录（默认项目根，需与后端 _resolve_workspace_dir 一致）",
    )
    args = parser.parse_args(argv)

    ws_root = Path(args.ws).resolve() if args.ws else _PROJECT_ROOT
    out_path = _PROJECT_ROOT / args.out

    print(f"工作区: {ws_root}")
    print(f"模型: {args.model} / 温度: {args.temperature} / 档位: {args.arms} / 重复: {args.reps}")
    print(f"输出: {out_path}")
    print(f"后端: {BASE}")
    print()

    # 安全守卫：工作区若已有非脚本的 notes/ 内容，直接中止（防误删）。
    assert_workspace_safe(ws_root)

    # 健康检查
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
                # 每次跑批前准备工作区
                prepare_workspace(ws_root)
                t0 = time.time()
                record = run_once(task, args.model, args.temperature, arm, rep)
                elapsed = time.time() - t0
                # 跑批后清理
                cleanup_workspace(ws_root)
                all_runs.append(record)
                rc = record["rework_count"]
                err = record.get("error")
                if err:
                    print(f"ERROR ({elapsed:.1f}s): {err[:80]}")
                else:
                    print(f"rework={rc} / status={record['task_status']} ({elapsed:.1f}s)")

    # 写原始 JSON
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(all_runs, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"\n原始数据: {out_path}")

    # 打印 Markdown 汇总
    print()
    print(render_summary(all_runs, args.arms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
