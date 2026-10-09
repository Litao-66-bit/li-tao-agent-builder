# Agent Builder 项目评价与未来方向（2026-10-09）

> 取证方式：**硬指标全部来自本机实测**（命令见附录）；远端状态来自 GitHub REST API（免 token）。
> HANDOVER 里的自述只在标注处引用，不作为独立证据。未独立复现的项已明确标注。

---

## 1. 一句话结论

**这是一个内核成熟度远超同类个人项目、但对外一致性与运行期形态严重落后的 Meta-Agent 原型。**
它最值钱的不是"能生成 Agent"，而是**一套系统级的事实核对链**（结论必须能被后端证据支撑）；
最拖后腿的不是算法，而是**仓库/文档/CI 落后代码约 5 个迭代**、**任务与密钥只在内存**、
以及 **agentic 形态 6.5× 的成本**。

---

## 2. 硬指标（本机实测）

| 指标 | 数值 |
|---|---|
| 后端规模 | `agent_builder/` **94 文件 / 16,223 行** |
| 测试规模 | `tests/` **70 文件 / 14,583 行 / 1,533 个 `def test_`**（pytest 实收 2,077 用例） |
| 子模块 | `api` 18、`roles` 17、`tools` 7 + `tools/impl` **29**、`contracts` 3、`evaluation` 8、`llm` 3 |
| HTTP 端点 | **33** 个（`@router.*`） |
| 最大文件 | `api/routes.py` **1,397 行**（33 端点全在一处）、`tests/test_frontend_workspace_ui.py` 690、`evaluation/scorecards.py` 573 |
| 前端 | `js/app.js` **2,626 行 / 134 KB**、`styles.css` 1,556 行、`index.html` 310 行、`js/api.js` 146 行（无构建、无 npm） |
| 债务计数 | `except Exception` **32** 处、TODO/FIXME/HACK **14** 处 |
| 文档 | 9 份 md（含 5 份评测报告） |
| 远端 | HEAD `33eb2aa`（2026-09-30 14:17），157 文件；**CI 已运行 63 次、全部 success**，走 PR/分支合并 |
| 本地 vs 远端 | **60 个文件内容不同 + ~81 个本地新增未推送 + 33 个远端独有**（本地无对应文件） |
| 基线 | `ruff` 干净；`pytest` **2,077 passed / 11 skipped** |

---

## 3. 评分卡

| 维度 | 评分 | 关键证据 | 判断 |
|---|---|---|---|
| 架构与分层 | ★★★★☆ | `contracts`(状态机) / `api` / `roles` / `tools` 四层清晰；`roles/` 禁跨层 import 且**有测试强制** | 约束被测试钉住，这点比多数项目强；扣分在 `routes.py` 单体化 |
| 安全护栏 | ★★★★☆ | `tools/gatekeeper.py`（权限+沙箱+审计）、高风险**到点暂停**、`require_local_client` 本机标识头、CORS 单源白名单 | 边界设计正确；扣分在"密钥明文进内存"与单机信任模型 |
| 质量工程 | ★★★★★ | 1,533 个测试 / 16k 行源码（≈0.9:1）；`scorecards.py` 三方一致性、`mode_ab.py` 运行时 A/B、role-brief **盲评 + 强制排序 + p 值** | **个人项目里罕见**：能用统计否掉自己的假设 |
| 可解释性与"不编造" | ★★★★★ | 结论事实行（本次实机验证通过）、未产出引用核对、状态分流措辞、每轮 `thought+reason+result` | **真正的护城河候选**：不是提示词工程，是系统级事实核对 |
| 对外一致性 | ★☆☆☆☆ | 远端 README 仍写"默认基于 LangGraph"（代码 **0** 处 import）、项目结构只列 `__init__.py`+`test_smoke.py`、Roadmap 全未勾；远端仍留上一代 `core/|graph/|facts/` | **最该先修**，且最便宜 |
| 运行期形态 | ★★☆☆☆ | 任务与密钥**仅内存**、无持久化/并发控制/多用户/容器化；重启即失忆 | 决定它现在只能是"本机演示" |
| 前端工程 | ★★★☆☆ | 无构建、`app.js` 2,626 行单体；测试是**静态接线断言**（`test_frontend_workspace_ui.py` 690 行正则/字符串） | 约束靠测试守住是加分；但改一行 UI 要读 2,626 行是负债 |
| 成本 | ★★☆☆☆ | 项目自测 A/B（24 次真实运行）：完成率 agentic 100% vs workflow 83%，token 中位数 4,866 vs 754（**≈6.5×**） | 质量换成本是自觉选择，但没做分级路由/压缩 |

---

## 4. 三个真正强的地方（建议当作立身之本）

1. **系统级事实核对链**：模型可以在结论里写"验证通过"，但后端会用真实执行结果补一行
   `（系统核对：真实运行 N 次，成功 M 次）`；还会核对"引用了本任务没产出过的文件"。
   这不是让模型更聪明，是**不让模型的自述变成事实**——绝大多数 Agent 产品没做这件事。
   （本次实机复验：任务 3 轮收敛，事实行正确附上，独立复跑其测试得 `2 passed`。）
2. **可复现的评测文化**：`scorecards`（文档-权限-目录三方一致）、`mode_ab`（运行时 A/B + 原始 JSON 落盘）、
   role-brief 盲评（两轮强制排序 + p≈0.0001 否掉自己的假设）。**评测资产本身就是产品**。
3. **被测试强制的架构约束**：`roles/` 跨层 import 禁令、"新增 .py 即视为新角色并参与评分"、
   状态机纯转换表 —— 这类约束通常只写在文档里，这里写进了测试。

---

## 5. 五个最该修的问题（按严重度）

### P0-1 仓库状态与代码脱节（对外一致性）
- 远端 `README.md`（3,147 字符）写"**默认基于 LangGraph**"、"项目结构"只有
  `agent_builder/__init__.py` + `tests/test_smoke.py`，Roadmap 五项全未勾选 ——
  而这五项（规划/生成/沙箱验证/Web 控制台）**当前都已实现**。
- 实测 `langgraph` 在 `pyproject.toml` 里声明，但**全仓库 0 处 import**（stale 依赖）。
- 远端仍保留上一代架构：`agent_builder/core/`、`graph/`、`facts/`、`contracts/messages.py`、
  `roles/tool_guardian.py`（后者本地已重构为 `tools/gatekeeper.py` + `roles/gatekeeper.py`，
  见 `evaluation/scorecards.py:503` 的"外部实现"约定）。
- 本地**缺失**：`README.md`、`LICENSE`、`NOTICE`、`SECURITY.md`、`PRIVACY.md`、`requirements.lock`、
  `.github/workflows/ci.yml`、`agent_builder/__main__.py`。
  → 后果：本地`pyproject.toml` 仍写着 `readme = "README.md"`；远端 README 的快速开始
  `python -m agent_builder "..."` 在当前代码里**没有入口**（新架构只有 Web 入口）。
- **风险**：直接推送（不用 `base_tree`）会删掉远端那 33 个文件；用 `base_tree` 则会留下
  **两代架构并存**的仓库。

### P0-2 运行期形态（决定能不能变成产品）
- `InMemoryTaskStore`：任务、密钥、审计全部内存态，**重启即失忆**；无并发控制、无多用户。
- 密钥仅内存（安全上是对的，可用性上意味着"每次重启都要重存"）。

### P1-3 单体化
- `api/routes.py` 1,397 行装下 33 个端点（含 agentic 编排、审批、交付、council、handover、workspace…）。
- `frontend/js/app.js` 2,626 行 + 无构建：改动的回归成本全靠 690 行静态断言守住。

### P1-4 成本
- agentic 形态 token 中位数 ≈4,866（workflow 754）。已有提示词前缀缓存，但**没有**
  分级模型路由、观察结果压缩、完成轮次复用。6.5× 在"质量优先"下可接受，
  但会挡住任何按次计费/规模化的设想。

### P2-5 一致性债务（比预想干净得多）
- `except Exception` **31 处：抽查确认全部是"有意降级"**（带 `# noqa: BLE001` 与说明——LLM 失败降级为空说明、
  执行层异常转 `failed` 状态并把真实原因回灌模型、敏感内容解码失败打 `[DECODE_FAILED]` 标记），
  **未发现静默吞掉异常**。TODO/FIXME 14 处；`langchain` 仅 1 处 import 残留。
- `requirements.lock` 不在本地而 CI 依赖它（已核对远端 lock 含 fastapi/uvicorn/pydantic/httpx/pytest/ruff，故 CI 可装）。
- `langgraph` 在 `pyproject.toml` 声明但 **0 处 import**（阶段 0 删除并重生成 lock）。

---

## 6. 未来方向：三条路

| 路线 | 一句话 | 抓手 | 代价 |
|---|---|---|---|
| **A 纵深：把"可信交付"做成产品** | 不比谁的 Agent 更炫，比**谁敢把结论交给用户** | 验证证据链→"可复现证据包"（命令/退出码/用例/产物哈希）；审计与回放 | 需要真实场景验证 |
| **B 横向：本机原型→可部署服务** | 持久化 + 容器化 + 多用户 + 观测 | SQLite/PG、Docker、密钥托管、健康检查 | 工程量最大，稀释"可信"叙事 |
| **C 变现评测：把评分/评测工具链对外化** | 用 `scorecards`+A/B+盲评 输出"Agent 质量报告" | 评测即服务 | 前提是先有 B 的一部分 |

**建议：以 A 为主线**（差异化 + 已有资产最多），**B 只做"最小可部署"**（持久化 + 容器化 + 健康检查），
**C 作为 A 的对外表达**（"我们的交付物带证据包，证据由我们的评测链生成"）。
理由：A 的三个抓手**都已经在代码里**（事实核对/评测链/护栏），是"把已有资产产品化"；
B 从零到一是纯投入；C 现在做会变成没有客户的工具。

---

## 7. 分阶段方案（每阶段都有验收标准）

### 阶段 0 —— 让仓库与代码一致（建议 1–2 天，最高性价比）
- 一次**同步提交**：新增新架构、**删除被取代的上一代模块**（`core/|graph/|facts/|contracts/messages.py|roles/tool_guardian.py` 及对应旧测试）、
  补回 `README.md`/`LICENSE`/`NOTICE`/`SECURITY.md`/`PRIVACY.md`/`requirements.lock`/CI。
- 重写 README：现在的架构图、真实能力、真实 Roadmap、正确的快速开始（Web 入口，不再是 `python -m agent_builder`）。
- 删掉 `pyproject.toml` 里的 `langgraph` 声明（0 import）并重生成 lock。
- CI 增加：`node --check frontend/js/app.js`（前端已有此校验命令）+ 覆盖率报告（先只观测、不设阈值）。
- **验收**：CI 三版本矩阵全绿；README 的每一句都能在代码里指到；`git clone` 后按 README 能起服务。

### 阶段 1 —— 持久化 + 最小部署（1 周）
- 任务/审计落 SQLite（密钥仍**不落盘明文**）；启动时恢复未完成任务；`Dockerfile` + `compose.yaml`；`/healthz`。
- **验收**：重启后任务可回看、可续跑；`docker compose up` 一条命令起来；新增持久化/恢复/迁移回归测试；
  基线仍为 `ruff` 干净 + 全绿。

### 阶段 2 —— 可信交付包（2 周，差异化核心）
- 每个任务产出 `delivery.json`：命令、退出码、stdout 摘要、测试用例清单、产物 `sha256`、模型与提示词版本；
  新增 `GET /tasks/{id}/evidence`；前端"证据"页；对"未验证/验证失败"的交付打**醒目降级标注**。
- **验收**：任一历史任务可**一键复现验证**；事实核对链从"一行说明"升级为"可下载证据包"；
  新增回归测试（证据完整性、哈希一致、缺失即降级）。

### 阶段 3 —— 成本与规模（2–4 周）
- 分级模型路由（轻任务小模型、关键决策大模型）+ 观察结果压缩 + 预算可视化；
  A/B 扩到 10 任务 × 3 次并固化为**回归门槛**。
- **验收**：完成率 ≥95% 的前提下 token 中位数下降 ≥40%（用 `evaluation/mode_ab.py` 复测）。

### 阶段 4 —— 评测工具链对外化（可选，视 A 的落地情况）

---

## 8. 度量（唯一真相，建议写进 README）

| 指标 | 现状 | 目标 |
|---|---|---|
| 完成率（到达 verifying） | agentic 100%（24 次自测） | 保持 ≥95% |
| token 中位数/任务 | 4,866 | 阶段 3 降 ≥40% |
| 首轮收敛率 | 已有单次样本（12 步收敛） | 建立统计口径 |
| 交付可复现率 | 无（新指标） | 阶段 2 起 ≥90% |
| CI 通过率 | 63/63 success | 保持 100% |

---

## 9. 不做清单（防止发散）
- 不做模板市场 / 多租户 / 插件生态（在 A 验证前）。
- 不引入第二套 Agent 框架；把"框架可插拔"从叙事里删掉（现状是自研状态机 + 角色 + 循环）。
- 不为"看起来更聪明"牺牲事实核对 —— 那是本项目的立身之本。

## 10. 风险
- **单人维护 16k 行 + 1,533 测试**：变更成本会持续上升，阶段 0/1 的本质是**降熵**。
- **成本若降不下来**，路线 A 的商业前提会被削弱（客户会问"每次交付多少钱"）。
- **两代架构并存的仓库**会让外部贡献者直接迷路，必须在阶段 0 清掉。

---

## 11. 代码级补充审计（AST 静态分析，2026-10-09）

**最长的 12 个函数**（复杂度热点，建议与 `routes.py` 拆分一并处理）：

| 行数 | 位置 | 函数 |
|---|---|---|
| 173 | `api/agent_loop.py:197` | `run_agent_loop()` |
| 169 | `api/routes.py:1136` | `_execute_plan_agentic()` |
| 138 | `evaluation/security_audit.py:194` | `dynamic_findings()` |
| 122 | `api/routes.py:732` | `plan_task()` |
| 119 | `api/deciders.py:399` | `build_decision_prompt()` |
| 110 | `tools/impl/web_search.py:28` | `search_web()` |
| 110 | `api/proposal_scaffold.py:43` | `render_role_module()` |
| 99 | `tools/impl/plan_validate.py:20` | `validate_plan()` |
| 96 | `roles/gatekeeper.py:69` | `execute()` |
| 95 | `tools/impl/sandbox_run.py:42` | `run_command()` |

**重复实现**：把函数体归一化（去 docstring/空白）后逐字节比对，**≥20 行的完全相同实现一个都没有**
—— 不存在"抄一遍"式的技术债，这点比多数同规模项目好。

**测试盲区**：`agent_builder/` 下仅 **4 个**模块在 `tests/` 里找不到任何引用，且**全部是一次性分析脚本**
（`evaluation/role_brief_ab_blind.py`、`role_brief_ab_blind_rank.py`、`role_brief_ab_exec.py`、`security_audit.py`）
—— **产品代码零盲区**。

**异常处理**：31 处 `except Exception` 全部为有意降级（见 P2-5），未发现静默吞异常。

完整清单：`.dsh-scratch/code-audit-report.txt`

---

## 附录：本次取证的命令与产物

- 未推送精确清单（远端 tree vs 本地逐文件 blob 哈希）：`.dsh-scratch/unpushed-report.txt`（脚本 `diff_remote.py`）
- ③ 实机验证：`.dsh-scratch/live-verify-3-report.txt` / `live-verify-3.json`；确定性打靶 `.dsh-scratch/verify-fact-report.txt`
- 远端文件对照：`.dsh-scratch/remote-README.md`、`remote-.github_workflows_ci.yml`、`remote-requirements.lock`、`remote-tree.json`
- 基线：`ruff check .` → All checks passed；`pytest` → 2,077 passed / 11 skipped
