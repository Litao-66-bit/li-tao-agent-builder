# 后端 agentic 化设计稿：由模型掌控选 agent

> **状态：P2 已完成** —— 独立「副结构」开关（UI + 门控）+ ``propose`` 闭环
> （提议浮出 → 批准生成脚手架 / 拒绝 → 回看）全部落地。
> **批准只写工作区 ``proposals/<id>/``，不改仓库既有文件**：新角色要人工并入
> ``tools/permissions.py`` 授权后才会被派发。
> **P3（A/B）跑批脚本已就绪**：``evaluation/mode_ab.py``（待密钥跑批）。决策来源见 §12。

## 1. 目标与非目标

**目标**：执行阶段不再走「静态 `ACTION_ROLE_MAP` + 固定五阶段流水线」，改由模型**每一轮**决策「下一个由谁做、做什么」，拿到结果后继续决策或收敛，直到它自己判定完成。

**非目标（本轮不动）**：工具权限矩阵、沙箱、审计、状态机契约、前端四约束。

## 2. 现状：哪里是"死的"

| 位置 | 现状 |
|---|---|
| `agent_builder/api/orchestrator.py` · `ACTION_ROLE_MAP` | action → 角色，**写死的字典**，`dispatch_to_role` 只查它 |
| `agent_builder/roles/router.py` · `EXECUTOR_MAP` / `_match_executor` | action → 执行者类型，同样写死 |
| 各角色模块的 `*_ACTIONS` | 角色只接受自己的 action 集合，模型选错即 `rejected` |
| `agent_builder/contracts/schemas.py` · `Step.assignee` | 注释写「由路由者填写」，**全仓库无人写入**；但 `orchestrator.executor_fn` 已会读它（`role = step.assignee or "operator"`）——现成钩子 |
| 模型职责 | 只用 `Decomposer._llm_split` 拆步骤（固定 JSON：`id/action/inputs/depends_on`），之后的顺序、派发、重试全由确定性规则决定 |

## 3. 不动的护栏（安全前提）

| 护栏 | 位置 | 为什么不能动 |
|---|---|---|
| 工具门卫：权限矩阵 + 沙箱 + 审计 | `agent_builder/tools/gatekeeper.py` | 工具执行的唯一入口，模型自由度不能穿透 |
| 高风险审批门 | `contracts/schemas.py::Approval` + `api/routes.py` 的 `_pending_high_risk` / `_pause_for_high_risk` | 循环里每次高风险动作前仍要能「到点暂停」 |
| roles 层禁 import `api`/`tools`/`llm` | `tests/test_evaluation_agents.py`（`LAYER_VIOLATIONS`） | 跨层依赖会扣分 → 循环控制器必须放 `api/` |
| 新角色三道门 | `docs/execution-protocols.md` + `tools/permissions.py` + `tests/test_roles_<name>.py`（≥10 例） | 防止模型借"新角色"绕开授权与评分 |
| 前端四约束 | sessionStorage / CSS token / `<button>` / 静态接线测试 | 既有基线 |

## 4. 新增部件（全部在 `api/`）

1. **`api/role_catalog.py` — 能力目录**（模型"可选项"的唯一真源）
   汇总 17 个角色 + `tools/registry.py` 的 `registry.list_tools()`：角色名 / 一句话能力 / 可接受 action / 高风险工具 / **所属架构（主 | 副）**。
   现有 `ACTION_ROLE_MAP` 从"派发真源"降级为"**回退**真源"；目录由它 + 各角色 `*_ACTIONS` 反向生成，避免两处真相。

2. **`api/agent_loop.py` — 控制循环**（纯逻辑 + 依赖注入，**不 import llm**）
   循环体、双预算、终止条件、审批插桩、回退。`decide` / `execute` / `approval` / `clock` 全部由参数注入 → 可单测。

3. **`api/deciders.py` — 决策契约 + 校验链 + 回退**（P0 已实现）
   - `Decision` / `LoopContext`：决策契约与上下文
   - `RouteDecider`（协议）：`decide(ctx) -> Decision`
   - `validate_decision()`：校验链（kind 合法 → 门控 → 角色在目录内 → 角色可承接该 action
     → **仅对已注册工具**再查权限矩阵）
   - `static_fallback()`：回退实现，等价于今天的 `ACTION_ROLE_MAP`（**不受副结构门控影响**）
   - `ScriptedDecider`：按脚本吐决策的假实现（仅测试 / 评估）
   - `LLMRouteDecider`：生产实现，走 `complete_json`（**P1**）

## 5. 决策契约（模型每轮只产这一个 JSON）

```json
{
  "thought": "一句话说明为什么选它",
  "next": {
    "kind": "agent",
    "role": "code_worker",
    "action": "file_write",
    "inputs": {"path": "a.md", "overwrite": true},
    "reason": "已有调研结论，需要落地成文件"
  }
}
```

`kind` ∈ `agent | tool | propose | final`：
- `agent` / `tool`：要走真实工具调用链的下一步；
- `propose`：提议新角色 / 新工具，**仅副结构开关开启时可选**（见 §7）；
- `final`：模型判定完成，走现有 `summarizer` 角色收敛结论。

**P0 行为**：校验通过的 `propose` 会让循环**立即停下**（`STOP_PROPOSED`）并把提议浮出水面，
**绝不把提议当成一个可执行步骤跑掉**；批准 → 落盘 → 注册的闭环在 P2。

**校验链**（P0 已实现于 `deciders.validate_decision`；任一步不过 → 不终止，**回退静态决策**，
结果标 `decided_by: "static"` + `fallback_reasons`）：

1. `kind` 合法；
2. `propose` 仅在副结构开启时允许；
3. `role` 在当前可选目录内；
4. `action` 在该角色可承接的集合内（`RoleSpec.accepts`）；
5. **仅当该 action 是已注册工具时**再查权限矩阵（角色内行为如 `summarize` / `propose`
   不经工具门卫，故不查）。

**绝不"猜一个"替代**：无法回退时循环以 `STOP_INVALID_DECISION` 停下，不执行任何东西。

### 5.1 提示词模板（P1 已实现，详细版）

`deciders.build_decision_prompt()` 渲染；**稳定内容在前**（目录 + 硬规则，吃 DeepSeek 前缀缓存），
**每轮变化的在后**（需求 / 历史 / 产物 / 预算）：

```text
【任务】你是执行编排决策者：每轮只决定「下一步由谁做什么」，拿到结果后再决定下一步。

【可用角色】
- code_worker（主架构）：…；可执行 code_search(path*, pattern*) / file_list(path*) / file_read(path*) / file_write(path*, content*) / file_edit(path*, old_string*, new_string*)
参数写法：动作(必填参数*, 带枚举的参数=值1|值2)；请**按签名给 inputs**，不要猜参数名。
…（catalog_prompt 渲染，含门控说明与"禁止选择"的流程控制角色）

【硬性规则】
1. 只输出一个 JSON 对象：不要解释、不要 markdown 代码块。
2. 每轮只做一步；已足以回答需求时用 kind=final 收尾，**且必须给出 answer** —— 它是直接写给用户看的结论（做成了什么 / 关键结论是什么），不要写「可以收尾」「需求已满足」这类内部独白。
3. role 必须是【可用角色】里的名字；action 必须是该角色「可执行」列表里的项。
4. 不得编造角色名、工具名，也不要塞 inputs 里没有依据的参数。
5. 高风险动作（写/删文件、提交、回滚）会被系统「到点暂停」等用户放行，你照常给出即可，不要因此绕开。
6. 上一轮失败的步骤不要原样重来：换 inputs 或换动作；实在做不下去就用 kind=final 并说明原因。
7. 分清「工作区原有的文件」和「你本任务的产出」：**只有本任务写过的、出现在【已产出】里的才算你的产出**。
   工作区里本来就有的文件一律不是你的成果；【已产出】是「无」时绝不要说「已构建 / 已生成 / 已产出 / 已完成某实现」。
   **answer 里写到的文件名 / 运行命令必须真的在【已产出】里**（不要写「用法：python -m pkg.main」而 main.py 并不存在）。
8. 不要通读整个仓库：先想清楚产出落在**哪个具体文件**，再按需读取；需求要的是可交付的代码/文件时，
   先把它写出来再验证，不要用「列出文件 / 读取文件 / 搜索代码」把仓库翻一遍就当成做完了。
9. 写代码要**真做事**，不是搭空壳：不许用 `pass` / `return True` / `NotImplementedError` / 「TODO」/ 硬编码假结果
   充当实现；收尾前必须有**真实运行证据**（跑测试或跑命令并看到输出），确实没实现的部分如实说明。
   **改了对外接口就必须同步更新调用方（含自己写的测试）**，否则测试必然全红。
（10. 副结构门控开启时才追加：可用 kind=propose 提议一个新角色。）

【输出结构】{thought, next:{kind, role, action, inputs, reason}}；收尾时 {thought, next:{kind:"final", answer:"给用户看的结论"}}

【当前进度】
用户需求：…
已完成（最早在前）：1. searcher / web_search → done：搜索到 3 条结果
已产出：a.md、b.md
已用预算：3/20 步 · 8200/60000 tokens
请给出下一步决策。
```

- **历史窗口 = 最近 6 轮**（`DEFAULT_HISTORY_WINDOW`）；更早轮次靠"已产出"与预算进度间接体现。
- 预算进度由 `run_agent_loop` 每轮写入 `LoopContext.budget_note`（避免 deciders ↔ agent_loop 循环 import，也不重复计数）。

### 5.2 JSON 容错分层（P1 已实现）

| 层 | 做什么 | 位置 |
|---|---|---|
| L1 提取 | 裸 JSON / ```` ```json ```` 代码块 / 首个 `{...}` | `LLMClient._extract_json`（已有，复用） |
| L2 归一化 | `next` 缺失取顶层（扁平写法）；`kind` 缺失时有 role+action 视为 `agent`；`inputs` 非字典记空；文本字段转字符串并截断到 200 字 | `deciders.parse_decision` |
| L3 校验 | kind / 门控 / 角色在目录内 / 角色可承接该 action / 已注册工具再查权限矩阵 | `deciders.validate_decision` |
| L4 重试 | **重试 1 次**（与 `Router.max_retries` 同尺度），第二次问话追加"上次输出无法解析" | `deciders.LLMRouteDecider` |
| L5 回退 / 停下 | 有 action → `static_fallback`（静态映射）；没有 → `STOP_INVALID_DECISION` 停下 | `agent_loop` |

**两条硬性安全约定**（都有断言）：
1. `KIND_INVALID` **不在** `KINDS` 里，且 `parse_decision` 对空返回 / 非字典一律判 invalid ——
   **"模型没答出来"绝不会被伪装成 `final`**（那等于假装任务完成）。
2. `parse_decision` **不替模型挑动作**：只给 `role` 没给 `action` 时判 invalid，不做推断。

`decided_by` 归因：`llm` / `llm-retry`（发生过重试）/ `static`（回退）/ `scripted`（测试）。

## 6. 循环骨架、终止条件、挂起续跑

```
ctx = {requirement, catalog, history: [], artifacts: [], budget: {steps, tokens}}

while not 终止:
    decision = decider.decide(ctx)          # 失败 → StaticRouteDecider 兜底
    if decision.kind == "final": break
    step = to_step(decision)                # 写入 step.assignee = decision.role
    if step 属高风险 且 未放行:
        保存 next_decision → 挂起 pending_approval   # 沿用现有「到点暂停」
        /resume 放行后从这一步继续
    result = dispatch(step)                 # 复用 dispatch_to_role + ToolGatekeeper
    ctx.history.append({decision, result, 用量, 耗时})
    ctx.artifacts.extend(提取产物路径)
```

**终止条件**：模型 `final` ／ 步数预算耗尽 ／ token 预算耗尽 ／ **连续 N 次无进展（防空转；成功但紧接着重复同一「动作 + 参数」不算进展）** ／ 用户中断 ／ 高风险待放行。

**挂起后怎么续跑（关键设计）**：循环上下文（`history` / `budget` / 待放行的那一步 `next_decision`）挂在 `agent_builder/api/store.py::TaskEntry` 上（新增 `loop_state` 字段，与现有 `execution_results` / `approved_tools` / `pending_approval` 同级）；`interrupted` 时保存待放行决策，`/resume` 从该步继续。**审批门语义完全不用改。**

**状态机不动**：粗粒度阶段（`received → planning → executing → verifying → …`）保留，循环发生在 `executing` 内部 → 审计、审批门、前端状态标签、`/usage`、`/handover` 全部可复用。

## 7. 副结构门控：允许提议新角色 / 新工具

- 门控信号：`POST /tasks/{id}/plan` 新增 `sub_arch` 字段（对应**新增的独立前端开关**，与现有 `#selfCheckToggle`「副结构自检」语义不同：那是执行后一致性核对），落到 `TaskEntry.options`。
- **关闭（默认）**：目录只注入主架构角色，`kind=propose` 在契约层不可选 —— 模型连"能提议"都看不到。
- **开启**：目录额外注入副架构角色 + 一句授权说明，允许产出 `ChangeProposal`（复用 `contracts/schemas.py::ChangeProposal`：`target_module` / `change_desc` / `diff_preview` / `risk` / `verification_plan`）。
- **提议 ≠ 可用**：提议 → 用户批准 → 落盘 `roles/<name>.py` + `docs/execution-protocols.md` 章节 + `tools/permissions.py` 授权 + `tests/test_roles_<name>.py`（≥10 例） → 注册进目录 → **下一轮决策才可选**。绝不直接调用未注册角色。
- **落盘方式 = 半自动**（已定）：模型出 diff 预览 → 用户确认 → 才写文件，且同时生成协议章节 / 授权 / 测试脚手架。
- **名单已定（按文档原文；固化在 `role_catalog.py` 并由测试交叉核对）**：
  **主架构 12 / 副架构 5 / 权限层 3**。关闭开关时的可选集 = 主架构执行者 8
  （主架构 12 − 流程控制 4）+ `operator`；开启时追加副架构 5。
  流程控制角色（`conductor` / `decomposer` / `scheduler` / `router`）**永不进目录**；
  `memory_manager` / `sub_architect` 暂不入目录（前者与 `memory_keeper` 同权、
  后者是版本治理入口，需要时再单独开）。
- **原先担心的副作用已证伪**：`metric_collect` 在关闭开关时**仍可达** —— `operator`
  的工具白名单里就有它，只是不再由 `auditor` 承接。已写成断言
  （`test_role_catalog.py::test_关闭时不含副架构专属动作`）。真正被门控挡住的只有
  副架构角色自身的动作（如 `propose` / `impact_analyze`）。

## 8. 模式切换与回退

- `mode`: `workflow`（现状） | `agentic`（新），由 `/plan` 请求字段决定；**默认 `agentic`**（已定），可显式切回 `workflow` 做 A/B。
- 无密钥 / 模型决策非法 / 循环异常 → `StaticRouteDecider`，行为与今天完全一致，所以默认切到 `agentic` 不会让"无密钥路径"消失。

## 9. 可测性（保住现有绿色基线）

循环是纯函数式的（`decide` / `execute` / `approval` / `clock` / `budget` 全注入）→ 单测用 `ScriptedDecider` 断言每一步、终止、回退、挂起续跑，**不碰网络**。

- 新增 `tests/test_agent_loop.py`：脚本决策器驱动的循环行为。
- 新增 `tests/test_role_catalog.py`：目录与权限矩阵一致性（目录里每个 action 必须在对应角色白名单内）。

## 10. 分阶段落地

| 阶段 | 内容 | 行为变化 |
|---|---|---|
| **P0 ✅ 已完成** | `role_catalog` + 决策契约 + 校验链 + 循环骨架 + `ScriptedDecider` 测试 | **零变化**（未接入路由） |
| **P1 ✅ 已完成** | 决策器（`LLMRouteDecider` + 提示词 + 容错 + 重试 1 次）+ `mode`/`sub_arch` 字段 + `loop_state` 持久化 + 审批挂起与 `/resume` 原样重放 + 前端复用工具卡 | 默认 `agentic` 且**有密钥**才走循环；无密钥自动回退固定工作流 |
| **P2 ✅ 已完成** | 独立「副结构」开关（`#subArchToggle` 三态：关 / 开 / 无密钥禁用）+ `propose` 闭环：提议挂起（`TaskEntry.pending_proposal`）→ `/proposal/approve` 生成脚手架写工作区 / `/proposal/reject` 只记录 → 前端提议卡 | 开关关闭时提议被契约层拒绝；批准**不改仓库既有文件** |
| **P3 ✅ 已完成** | A/B：`workflow` vs `agentic` 跑批脚本 `evaluation/mode_ab.py`（4 任务 × 2 档 × 3 次；输出 `docs/reports/mode-ab-runs.json` + 报告 `docs/reports/mode-ab.md`；走 `GET /tasks/{id}/usage` 取成本）。**实测结论（v2 任务集）：完成率 agentic 100% vs workflow 83%、成本约 6.5× → 倾向 `agentic`**（代价是更贵更慢；关键差异是 agentic 错了能自我纠正，workflow 计划冻结、错一步就停） | 只读（不改后端 / 前端；产物落工作区 `_mode_ab_out/`，跑完即整目录删除） |

## 11. 风险与取舍

| 风险 | 处理 |
|---|---|
| 循环烧 token，成本不可控 | 步数 + token 双预算，超限**强制收敛出结论**，不静默失败 |
| 死循环 / 空转 | 连续 N 次无进展即终止；另有两道"重复"闸：同一「动作 + 参数」**重复失败** N 次、或紧接着**重复成功**且无新产物，都不算进展 |
| 审计可解释性 | 每轮落 `thought + reason + result` 到执行结果（前端折叠展示）；门卫审计不变 |
| 模型编造角色 / 工具 | 校验链 + 回退静态映射 |
| 提议扩编被滥用 | 提议 → 批准 → 落盘三道门，新角色仍需协议 + 授权 + 测试 |
| 与现有测试冲突 | P0 要求零行为变化；新路径不进旧用例 |

## 12. 已定决策 / 待定项

**已定（用户确认）**
1. `mode` **默认 `agentic`**，可切 `workflow`。
2. 副结构门控用**新增的独立开关**（不复用 `#selfCheckToggle`）。
3. 新角色**半自动**落盘（提议 → 用户确认 → 生成文件与脚手架）。
4. 主 / 副架构名单**按文档原文**：主架构 12 / 副架构 5 / 权限层 3（详见 §7）。
5. 关闭副结构开关时的可选集 = 主架构执行者 8 + `operator`（共 9，详见 §7）。
6. 预算：**20 步 / 60k tokens / 连续 2 轮无进展即终止**
   （已固化为 `agent_loop.py` 的 `MAX_LOOP_STEPS` / `MAX_LOOP_TOKENS` / `MAX_STAGNANT_ROUNDS`；
   20 步而非 12：agentic 不预分解 DAG，「写代码 → 运行 → 排错」这条主链路得自己走完）。
7. 决策提示词用**详细版**（目录 + 9 条硬规则 + 输出结构 + 进度区，见 §5.1；副结构开启时第 10 条）。
8. JSON 容错**重试 1 次**再回退（见 §5.2）。
9. 提示词历史窗口 = **最近 6 轮**（`DEFAULT_HISTORY_WINDOW`）。
10. `mode` / `sub_arch` **并入现有 `PlanRequest`**（随 `/plan` 存入 `entry.options`，执行阶段读取）—— 与 `use_llm` / `self_check` 同款。
11. 循环上下文**新建 `LoopState` 模型**（挂在 `TaskEntry.loop_state`，并出现在 `TaskResponse`）。
12. 前端**复用现有工具卡**展示每轮（标题=后端下发的中文动作名，详情=`summary`）；
    过程块折叠（方案 B）**已完成**：工具卡与自检收进默认收起的过程块，
    有失败 / 待放行 / 需决定时自动展开；产物入口另起一行留在正文。
13. 提议的「批准」**只写工作区 `proposals/<id>/`**（脚手架 + 并入清单），**绝不改仓库既有文件**：
    `tools/permissions.py`（授权）/ `registry.py`（工具注册）/ 协议文档一律留人工合并 ——
    自动改这三处等于让一次模型提议直接获得执行权限，越过「新角色三道门」。
    因此**新角色在人工并入授权前天然不可派发**（生成物 README 就是并入清单）。
14. 提议**只支持新角色**（`target=role`）：新工具要改 `registry.py` 与权限矩阵，不在自动落盘范围，
    契约校验直接如实拒绝 —— 不给"看起来批准了其实没生效"的假通道。
15. 「一个名字只指一件事」：一致性核对的前端文案由「副结构自检」改为**「执行自检」**
    （线上字段 `self_check` 与代码标识符不变，避免无收益的契约变更）。

**P0 / P1 / P2 交付物（已落地）**

| 文件 | 内容 |
|---|---|
| `agent_builder/api/role_catalog.py` | 能力目录：分层名单、`accepts`、门控过滤、提示词渲染、高风险判定 |
| `agent_builder/api/deciders.py` | `Decision` / `LoopContext` / `RouteDecider` / `validate_decision` / `static_fallback` / `build_decision_prompt` / `parse_decision` / `LLMRouteDecider` / `PrefixedDecider` / `ProposalDraft` / `ScriptedDecider` |
| `agent_builder/api/proposal_scaffold.py` | 提议脚手架：`plan_scaffold`（5 件生成物）/ `write_scaffold`（工作区沙箱 + 整体成功语义）/ 角色模块·测试·协议章节·授权片段·并入清单渲染 |
| `agent_builder/api/agent_loop.py` | 循环骨架：预算、终止条件、`to_step`（`loop-NNN` + `assignee`）、`run_step`（异常转状态）、`to_execution_results`、审批挂起、提议浮出 |
| `agent_builder/api/orchestrator.py` | 抽出 `build_step_executor()` —— 固定工作流与 agentic 共用同一条执行链路（权限/沙箱/审计语义一致） |
| `agent_builder/api/routes.py` | `mode` 分流、`_execute_plan_agentic()`、`LoopState` 组装、`/resume` 原样重放（`PrefixedDecider`） |
| `agent_builder/api/schemas.py` / `store.py` | `PlanRequest.mode`·`sub_arch`、`StepResult.thought`、`LoopState` 系列模型、`TaskEntry.loop_state` |
| `frontend/js/app.js` | 执行卡优先展示 `summary` + 中文动作名（`stepTitle`）；`index.html` `?v=` bump |
| `tests/test_role_catalog.py` | 三角一致性（文档 / 权限 / 目录）、门控、提示词、无可用动作角色 |
| `tests/test_agent_loop.py` | 校验链、静态回退、三类预算终止、空转、审批挂起、提议浮出 |
| `tests/test_deciders.py` | 提示词渲染与窗口、容错归一化、"绝不伪装成 final"、重试与抛错降级 |
| `tests/test_proposal_closure.py` | 提议闭环：挂起带出完整草案 / 重名不挂起 / 批准写工作区且生成物是合法 Python / 拒绝不写文件 / 无待定提议 409 / 沙箱拒越界 |
| `tests/test_agentic_mode.py` | 端到端：多步收敛 / 到点暂停 / 放行重放写盘 / 无密钥回退 / 非法决策回退 / 解析失败不伪装完成 |

**已定（P3 开工前定的两项）**
1. **任务集**：4 个真实需求（含「调研论文 agent」主任务 R1），2 档 × 3 次；产物一律写工作区独占目录 `_mode_ab_out/`。
2. **评价口径**：只用**运行时指标**、不新增 agentic 专有维度 —— **P0 质量**（完成率 / 失败步骤 / 返工）优先于 **P1 成本**（token / 循环轮数）。`scorecards.py` 是**角色静态评分**（契约/权限/实现三方一致），与运行时 A/B 不同源，故未复用。

## 13. 校验命令（改动后必须全绿）

```powershell
$env:PYTHONPATH = ".deps"
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m ruff check .
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m pytest
node --check frontend/js/app.js
```

P2 完成时的基线：`ruff` 干净、`pytest` 1865 passed / 11 skipped
（P0 开始 1773 → P0 结束 1811 → P1 决策器 1839 → P1 接线 1847 → P2 开关 1849 → P2 提议闭环 1865）。
