"""角色简报 A/B —— 规划阶段「强制排序盲评」样本生成器（第二轮）。

用法（项目根目录）：

    python -m agent_builder.evaluation.role_brief_ab_blind_rank
    python -m agent_builder.evaluation.role_brief_ab_blind_rank --seed 20261007

与第一轮「绝对分盲评」的区别（见 ``role_brief_ab_blind.py``）：

- 第一轮按四个维度各打 1–5 分 → 实测**量表饱和**（35/44 全 5），零区分度；
- 本轮**强制排序**：每个「组」恰好含 ``off`` / ``core`` / ``full`` 各一份计划，匿名打乱为
  甲 / 乙 / 丙，评者**必须给出唯一名次**（禁止并列）→ 逼出相对判断、天然抗饱和。

产出：

- ``docs/reports/role-brief-ab-blind-rank.html``：离线排序页（匿名，不含档位）；
- ``docs/reports/role-brief-ab-blind-rank-key.json``：揭盲映射（组 → 标签 → 档位）。

设计约束：
- 只用 stdlib，不调 LLM、不联网。
- 固定随机种子 → 同输入必得同输出（可复现、可追溯 = 预注册）。
- 每档位在各组中出现次数相等（平衡设计）；同一份计划至多出现一次。
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from agent_builder.evaluation.role_brief_ab_blind import (
    REQ_SUMMARY,
    RUNS_FILE,
    TASK_ALIAS,
    collect_samples,
    load_runs,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

HTML_FILE = _PROJECT_ROOT / "docs" / "reports" / "role-brief-ab-blind-rank.html"
KEY_FILE = _PROJECT_ROOT / "docs" / "reports" / "role-brief-ab-blind-rank-key.json"

# 固定种子：排序分组可复现（预注册）。与第一轮（20261006）不同，避免沿用同一套抽签。
DEFAULT_SEED = 20261007

ARMS: tuple[str, ...] = ("off", "core", "full")
LABELS: tuple[str, ...] = ("甲", "乙", "丙")


def build_groups(samples: list[dict], seed: int) -> list[dict]:
    """按任务构造排序组：每组 3 份计划（每档位各 1），标签顺序打乱。

    每任务的组数 = 该任务下各档位可用样本数的最小值；同一份计划不会被重复使用。
    """
    rng = random.Random(seed)
    pools: dict[str, dict[str, list[dict]]] = {}
    for sample in samples:
        pools.setdefault(sample["task"], {}).setdefault(sample["arm"], []).append(sample)

    groups: list[dict] = []
    for task in TASK_ALIAS:
        arms_pool = pools.get(task, {})
        avail = {arm: list(arms_pool.get(arm, [])) for arm in ARMS}
        if not all(avail.values()):
            continue
        rounds = min(len(items) for items in avail.values())
        for items in avail.values():
            rng.shuffle(items)
        for index in range(rounds):
            picks = [(arm, avail[arm][index]) for arm in ARMS]
            rng.shuffle(picks)
            options = []
            for label, (arm, sample) in zip(LABELS, picks):
                options.append(
                    {
                        "label": label,
                        "sample_id": sample["task_id"],
                        "arm": arm,
                        "rep": sample["rep"],
                        "plan": sample["plan"],
                        "pending": sample["pending"],
                    }
                )
            groups.append(
                {
                    "group_id": f"G{len(groups) + 1:02d}",
                    "task": task,
                    "task_alias": TASK_ALIAS[task],
                    "req": REQ_SUMMARY[task],
                    "options": options,
                }
            )
    return groups


def build_key(groups: list[dict]) -> list[dict]:
    """揭盲映射（不含计划正文）。"""
    return [
        {
            "group_id": group["group_id"],
            "task": group["task"],
            "options": [
                {
                    "label": opt["label"],
                    "sample_id": opt["sample_id"],
                    "arm": opt["arm"],
                    "rep": opt["rep"],
                }
                for opt in group["options"]
            ],
        }
        for group in groups
    ]


def _payload_for_html(groups: list[dict]) -> list[dict]:
    """页面数据：去掉档位（arm），只留标签、盲评编号、计划正文。"""
    payload: list[dict] = []
    for group in groups:
        payload.append(
            {
                "id": group["group_id"],
                "task": group["task_alias"],
                "req": group["req"],
                "options": [
                    {
                        "label": opt["label"],
                        "plan": opt["plan"],
                        "pending": opt["pending"],
                    }
                    for opt in group["options"]
                ],
            }
        )
    return payload


def build_html(groups: list[dict]) -> str:
    """渲染离线排序页（自包含；数据内联；无外部依赖、无 emoji）。"""
    data = json.dumps(_payload_for_html(groups), ensure_ascii=False).replace("<", "\\u003c")
    labels = json.dumps(list(LABELS), ensure_ascii=False)
    return _HTML_TEMPLATE.replace("__LABELS__", labels).replace("__GROUPS__", data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成角色简报强制排序盲评页（离线、可复现）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="分组种子（固定即预注册）")
    parser.add_argument("--runs", type=Path, default=RUNS_FILE, help="A/B 原始数据路径")
    parser.add_argument("--html-out", type=Path, default=HTML_FILE, help="排序页输出路径")
    parser.add_argument("--key-out", type=Path, default=KEY_FILE, help="揭盲映射输出路径")
    args = parser.parse_args(argv)

    samples = collect_samples(load_runs(args.runs))
    groups = build_groups(samples, args.seed)
    args.html_out.write_text(build_html(groups), encoding="utf-8")
    args.key_out.write_text(
        json.dumps(build_key(groups), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    used = sum(len(group["options"]) for group in groups)
    counts = {arm: 0 for arm in ARMS}
    for group in groups:
        for opt in group["options"]:
            counts[opt["arm"]] += 1
    print(f"排序组数：{len(groups)}（每组 3 份，用到 {used} / 有效 {len(samples)}）")
    print(f"各档位出现次数：{counts}")
    print(f"排序页：{args.html_out}")
    print(f"揭盲表：{args.key_out}")
    return 0


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>角色简报 A/B · 计划强制排序盲评</title>
<style>
  :root {
    --bg: #ffffff;
    --surface: #f6f8fa;
    --surface-2: #eef1f4;
    --border: #d0d7de;
    --text: #1f2328;
    --text-muted: #57606a;
    --accent: #0969da;
    --accent-soft: #ddf4ff;
    --ok: #1a7f37;
    --warn: #9a6700;
    --danger: #cf222e;
    --radius: 8px;
    --shadow: 0 1px 3px rgba(31, 35, 40, .12);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117;
      --surface: #161b22;
      --surface-2: #21262d;
      --border: #30363d;
      --text: #e6edf3;
      --text-muted: #8b949e;
      --accent: #4493f8;
      --accent-soft: #0c2d6b;
      --ok: #3fb950;
      --warn: #d29922;
      --danger: #f85149;
      --shadow: 0 1px 3px rgba(1, 4, 9, .6);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 14px/1.6 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  }
  header {
    padding: 20px 24px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  h1 { margin: 0 0 6px; font-size: 18px; }
  h2 { margin: 0 0 10px; font-size: 15px; }
  .sub { color: var(--text-muted); margin: 0 0 12px; max-width: 820px; }
  .rule {
    border: 1px solid var(--border); border-radius: var(--radius);
    background: var(--bg); padding: 10px 12px; font-size: 12px; margin: 0 0 12px;
    max-width: 820px;
  }
  .rule b { color: var(--text); }
  .rule em { color: var(--danger); font-style: normal; font-weight: 600; }
  .progress { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 12px; }
  .bar { flex: 1; min-width: 160px; height: 8px; background: var(--surface-2);
    border-radius: 999px; overflow: hidden; }
  .bar > i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .15s; }
  main { padding: 20px 24px 130px; max-width: 1080px; margin: 0 auto; }
  .card { border: 1px solid var(--border); border-radius: var(--radius);
    background: var(--surface); box-shadow: var(--shadow); padding: 18px; }
  .card-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 6px; }
  .badge { background: var(--accent-soft); color: var(--accent); font-weight: 600;
    border-radius: 999px; padding: 2px 10px; font-size: 12px; }
  .req { margin: 0 0 16px; padding: 10px 12px; border-left: 3px solid var(--accent);
    background: var(--bg); border-radius: 0 var(--radius) var(--radius) 0; }
  .req b { color: var(--text-muted); font-weight: 600; font-size: 12px; display: block; }
  .option { border: 1px solid var(--border); border-radius: var(--radius);
    background: var(--bg); padding: 12px; margin-bottom: 14px; }
  .option-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
  .opt-label { font-weight: 700; font-size: 15px; min-width: 28px; }
  .rank-group { display: flex; gap: 6px; align-items: center; margin-left: auto; }
  .rank-group .rk-hint { color: var(--text-muted); font-size: 12px; }
  .rank-group button {
    min-width: 40px; height: 34px; border: 1px solid var(--border); background: var(--surface);
    color: var(--text); border-radius: 6px; cursor: pointer; font: inherit; font-weight: 600;
  }
  .rank-group button:hover { border-color: var(--accent); }
  .rank-group button[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: #fff; }
  .rank-group button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .plan { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 10px; overflow: auto; max-height: 30vh;
    font: 12px/1.5 "Cascadia Mono", Consolas, "Courier New", monospace;
    white-space: pre; margin: 0; }
  .pending { margin: 8px 0 0; font-size: 12px; color: var(--text-muted); }
  .pending ul { margin: 4px 0 0; padding-left: 18px; }
  textarea {
    width: 100%; min-height: 54px; resize: vertical; border: 1px solid var(--border);
    border-radius: var(--radius); padding: 8px 10px; background: var(--bg); color: var(--text);
    font: inherit; margin-top: 4px;
  }
  nav.pager { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
  button.act {
    border: 1px solid var(--border); background: var(--surface); color: var(--text);
    border-radius: 6px; padding: 8px 14px; cursor: pointer; font: inherit;
  }
  button.act:hover { border-color: var(--accent); color: var(--accent); }
  button.act:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button.act.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.act:disabled { opacity: .5; cursor: not-allowed; }
  select { border: 1px solid var(--border); background: var(--surface); color: var(--text);
    border-radius: 6px; padding: 7px 10px; font: inherit; }
  .spacer { flex: 1; }
  footer.actions {
    position: fixed; left: 0; right: 0; bottom: 0; border-top: 1px solid var(--border);
    background: var(--surface); padding: 12px 24px; display: flex; align-items: center;
    gap: 10px; flex-wrap: wrap;
  }
  .status { color: var(--text-muted); font-size: 12px; }
  .status.ok { color: var(--ok); }
  .status.warn { color: var(--warn); }
  .done-banner { border: 1px solid var(--ok); border-radius: var(--radius); padding: 12px;
    background: var(--bg); color: var(--ok); margin-bottom: 14px; display: none; }
</style>
</head>
<body>
<header>
  <h1>角色简报 A/B · 计划强制排序盲评</h1>
  <p class="sub">
    共 <b id="total">0</b> 组。每组含三份匿名计划（甲 / 乙 / 丙），请<strong>比较后给出唯一名次</strong>：
    哪一份最像「可以直接拿去执行的好计划」。
  </p>
  <div class="rule">
    <b>排序标准（整体质量，三份一起比）：</b>更贴合需求、步骤更具体可执行、依赖更自洽、
    更不越权/更安全、遇到缺失信息更倾向转待确认而非自行臆造。<br>
    <em>禁止并列</em>——必须排出第 1 / 第 2 / 第 3，这是本轮相对第一轮（绝对分）的关键。
    评分页不暴露档位，请勿查看同目录下的 <code>role-brief-ab-blind-rank-key.json</code>。
  </div>
  <div class="progress">
    <div class="bar"><i id="bar"></i></div>
    <span id="progress-text">已排序 0 / 0</span>
  </div>
</header>

<main>
  <div class="done-banner" id="doneBanner">全部组已排序完成。请用底部「复制结果」或「下载 JSON」导出。</div>

  <section class="card" id="card">
    <div class="card-head">
      <span class="badge" id="groupId">--</span>
      <span id="groupTask">--</span>
      <span class="spacer"></span>
      <span class="status" id="ratedMark"></span>
    </div>
    <p class="req"><b>需求</b><span id="groupReq">--</span></p>

    <div id="options"></div>

    <h2>备注（可选）</h2>
    <textarea id="note" placeholder="记录你排序的依据，尤其是最难分的那两份"></textarea>

    <nav class="pager">
      <button class="act" type="button" id="prevBtn">上一组</button>
      <button class="act primary" type="button" id="nextBtn">下一组</button>
      <span class="spacer"></span>
      <label class="status" for="jump">跳转到</label>
      <select id="jump"></select>
    </nav>
  </section>
</main>

<footer class="actions">
  <span class="status" id="status">就绪</span>
  <span class="spacer"></span>
  <button class="act" type="button" id="copyBtn">复制结果</button>
  <button class="act" type="button" id="downloadBtn">下载 JSON</button>
  <button class="act" type="button" id="resetBtn">重置本页数据</button>
</footer>

<script>
var GROUPS = __GROUPS__;
var LABELS = __LABELS__;
var STORE_KEY = "agent-builder:blind-rank-eval";
var state = { idx: 0, ranks: {}, notes: {} };

function loadState() {
  var raw = null;
  try { raw = window.sessionStorage.getItem(STORE_KEY); } catch (e) { raw = null; }
  if (!raw) { return; }
  var parsed = null;
  try { parsed = JSON.parse(raw); } catch (e) { parsed = null; }
  if (parsed && parsed.ranks) {
    state.ranks = parsed.ranks;
    state.notes = parsed.notes || {};
    state.idx = Math.min(parsed.idx || 0, GROUPS.length - 1);
  }
}

function saveState() {
  try { window.sessionStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* 忽略 */ }
}

function ranksOf(groupId) {
  if (!state.ranks[groupId]) { state.ranks[groupId] = {}; }
  return state.ranks[groupId];
}

function isComplete(map) {
  var used = {};
  for (var i = 0; i < LABELS.length; i++) {
    var rank = map[LABELS[i]];
    if (!rank) { return false; }
    if (used[rank]) { return false; }
    used[rank] = true;
  }
  return true;
}

function groupComplete(group) {
  var map = state.ranks[group.id];
  return !!(map && isComplete(map));
}

function completeCount() {
  var n = 0;
  for (var i = 0; i < GROUPS.length; i++) { if (groupComplete(GROUPS[i])) { n++; } }
  return n;
}

function assignRank(group, label, rank) {
  var map = ranksOf(group.id);
  for (var i = 0; i < LABELS.length; i++) {
    if (LABELS[i] !== label && map[LABELS[i]] === rank) { delete map[LABELS[i]]; }
  }
  map[label] = rank;
  saveState();
  render();
}

function renderOptions(group) {
  var host = document.getElementById("options");
  host.textContent = "";
  var map = ranksOf(group.id);
  for (var i = 0; i < group.options.length; i++) {
    (function (opt) {
      var box = document.createElement("article");
      box.className = "option";

      var head = document.createElement("div");
      head.className = "option-head";

      var label = document.createElement("span");
      label.className = "opt-label";
      label.textContent = opt.label;
      head.appendChild(label);

      var groupEl = document.createElement("div");
      groupEl.className = "rank-group";
      groupEl.setAttribute("role", "group");
      groupEl.setAttribute("aria-label", opt.label + " 名次");

      var hint = document.createElement("span");
      hint.className = "rk-hint";
      hint.textContent = "名次";
      groupEl.appendChild(hint);

      for (var r = 1; r <= LABELS.length; r++) {
        (function (rank) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.textContent = String(rank);
          btn.setAttribute("aria-pressed", map[opt.label] === rank ? "true" : "false");
          btn.addEventListener("click", function () { assignRank(group, opt.label, rank); });
          groupEl.appendChild(btn);
        })(r);
      }
      head.appendChild(groupEl);
      box.appendChild(head);

      var plan = document.createElement("pre");
      plan.className = "plan";
      plan.textContent = opt.plan;
      box.appendChild(plan);

      if (opt.pending && opt.pending.length) {
        var pending = document.createElement("div");
        pending.className = "pending";
        pending.appendChild(document.createTextNode("计划自带待确认点："));
        var ul = document.createElement("ul");
        for (var p = 0; p < opt.pending.length; p++) {
          var li = document.createElement("li");
          li.textContent = opt.pending[p];
          ul.appendChild(li);
        }
        pending.appendChild(ul);
        box.appendChild(pending);
      }

      host.appendChild(box);
    })(group.options[i]);
  }
}

function jumpLabel(group) {
  return group.id + " · " + group.task + " · " + (groupComplete(group) ? "已排序" : "未排序");
}

function buildJump() {
  var jump = document.getElementById("jump");
  jump.textContent = "";
  for (var i = 0; i < GROUPS.length; i++) {
    var opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = jumpLabel(GROUPS[i]);
    jump.appendChild(opt);
  }
}

function updateJumpLabels() {
  var opts = document.getElementById("jump").options;
  for (var i = 0; i < opts.length && i < GROUPS.length; i++) {
    var label = jumpLabel(GROUPS[i]);
    if (opts[i].textContent !== label) { opts[i].textContent = label; }
  }
}

function render() {
  var group = GROUPS[state.idx];

  document.getElementById("groupId").textContent = group.id;
  document.getElementById("groupTask").textContent = group.task;
  document.getElementById("groupReq").textContent = group.req;

  renderOptions(group);

  var note = document.getElementById("note");
  note.value = state.notes[group.id] || "";
  note.oninput = function () { state.notes[group.id] = note.value; saveState(); };

  document.getElementById("ratedMark").textContent = groupComplete(group) ? "本组已完成" : "本组未完成";
  document.getElementById("prevBtn").disabled = state.idx === 0;
  document.getElementById("nextBtn").disabled = state.idx === GROUPS.length - 1;
  updateJumpLabels();
  document.getElementById("jump").value = String(state.idx);

  var done = completeCount();
  document.getElementById("total").textContent = String(GROUPS.length);
  document.getElementById("progress-text").textContent = "已排序 " + done + " / " + GROUPS.length;
  document.getElementById("bar").style.width = (GROUPS.length ? (done / GROUPS.length * 100) : 0) + "%";
  document.getElementById("doneBanner").style.display = (done === GROUPS.length) ? "block" : "none";
}

function collectResult() {
  var out = [];
  for (var i = 0; i < GROUPS.length; i++) {
    var group = GROUPS[i];
    var map = state.ranks[group.id];
    if (!map || !isComplete(map)) { continue; }
    var item = { group: group.id, note: (state.notes[group.id] || "").trim() };
    for (var j = 0; j < LABELS.length; j++) { item[LABELS[j]] = map[LABELS[j]]; }
    out.push(item);
  }
  return out;
}

function setStatus(text, kind) {
  var el = document.getElementById("status");
  el.textContent = text;
  el.className = "status" + (kind ? " " + kind : "");
}

function onCopy() {
  var result = collectResult();
  var text = JSON.stringify(result, null, 2);
  var done = function () { setStatus("已复制 " + result.length + " 组到剪贴板", "ok"); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () { setStatus("复制失败，请改用下载", "warn"); });
  } else {
    setStatus("当前环境不支持剪贴板，请改用下载", "warn");
  }
}

function onDownload() {
  var blob = new Blob([JSON.stringify(collectResult(), null, 2)], { type: "application/json" });
  var url = URL.createObjectURL(blob);
  var a = document.createElement("a");
  a.href = url;
  a.download = "role-brief-ab-blind-rank-ratings.json";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  setStatus("已下载排序结果", "ok");
}

function onReset() {
  if (!window.confirm("确定清空本页所有排序？此操作不可撤销。")) { return; }
  state.ranks = {};
  state.notes = {};
  state.idx = 0;
  saveState();
  render();
  setStatus("已重置", "warn");
}

function bind() {
  document.getElementById("prevBtn").addEventListener("click", function () {
    if (state.idx > 0) { state.idx--; saveState(); render(); }
  });
  document.getElementById("nextBtn").addEventListener("click", function () {
    if (state.idx < GROUPS.length - 1) { state.idx++; saveState(); render(); }
  });
  document.getElementById("jump").addEventListener("change", function (event) {
    state.idx = Number(event.target.value);
    saveState();
    render();
  });
  document.getElementById("copyBtn").addEventListener("click", onCopy);
  document.getElementById("downloadBtn").addEventListener("click", onDownload);
  document.getElementById("resetBtn").addEventListener("click", onReset);
}

loadState();
buildJump();
bind();
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
