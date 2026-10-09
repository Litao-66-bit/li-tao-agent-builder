# 项目交接提示词（Agent Builder）

> **本文件已瘦身**：删掉了已完成的审计整改明细、A/B 完整数据、文件级变更流水账等历史叙述，只保留**结论、当前架构、硬性约束、下一步计划**。历史细节如需要可从 git 或旧版本找回。
>
> **对话优先 + 产物入口已完成**（「你好」不再弹执行计划；「查看产物」只在真产出时出现；agentic 下不渲染计划块），详见「对话优先 + 产物入口」一节。
>
> **三项后续方向已完成**：① 结论区 ② DeepSeek 式完整折叠 ③ 规划阶段去工作流化 —— 详见「结论区 + 完整折叠 + 规划去工作流化」一节。
>
> **P3（A/B 评测）已完成**：24 次真实运行，完成率 **agentic 100% vs workflow 83%**、成本约 **6.5×** → **倾向 `agentic`**（质量优先，代价是更贵更慢）—— 详见「P3：执行形态 A/B」一节 + `docs/reports/mode-ab.md`。
>
> **今日（2026-10-07）完成**：产物面板 2 个 bug + 放开大文件预览、P3 A/B 跑批出结论、旧轮次 3 项补测、`test_run` 本机跑不通 —— 详见「今日完成（2026-10-07）」一节。
>
> **今日（2026-10-08）完成**：四条硬性约束审计 + 修复（交互原生化 / 颜色 token 化）、「⏸ 中断」按钮不可用定位与修复（循环可中断 + 执行期放开按钮）、实测找 bug → **卡 1–卡 12 全部修复并逐一实测**、**「写代码 → 运行」全链路实机验证通过** —— 详见「今日完成（2026-10-08）」一节。
>
> **同日第二轮实测（接上）**：按「先测后修」再修 **卡 13–卡 18** + 新增 **③ 后端「验证证据」事实行**，并**撤回误报的卡 16**。
> 其中 **卡 18（观察窗口 800 字装不下源码结构）** 与 **卡 17（重复的「成功」动作被算作有进展）** 是"反复重读同一个文件 → 预算烧光"的真正根因；
> 修完跑出**首轮真正收敛**（12 步、测试 5 项通过、沙箱实跑成功，连续重复轮次 6 → 0）—— 详见「今日完成（2026-10-08）」下的「续：同日第二轮实测」小节。
> 基线 **2033 → 2077 passed / 11 skipped**。
>
> **未推送提醒**：本会话全部改动**仍未推送 GitHub**（本地无 `.git`，只能走 REST API）。见「未推送状态」一节。

## 新会话启动提示词（直接粘贴）

```text
继续 Agent Builder 项目（c:\Users\李陶\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\work-mode-projects\6abcee34807a00aa83da2398）。

先完整读 docs/HANDOVER.md，重点读「今日完成（2026-10-08）」「当前架构与状态」「P3：执行形态 A/B」三节。
（后端 / 前端当前**未运行**，需按下方命令重启；根目录已无散落样例，实测产物都收在 `_sample_backup/`，可整目录删。）

已完成：输出形态重构（四类机器输出消除 + 过程块折叠）、对话优先（/chat 分流）、产物入口、
agentic 下隐藏计划块、① 结论区 ② DeepSeek 式完整折叠 ③ 规划阶段去工作流化、
P3 执行形态 A/B（workflow vs agentic，脚本 + 报告已出结论）、
产物面板 2 个 bug（刷新重置折叠 / 放行后误判空转）+ 放开大文件预览的 20MB 上限、
旧轮次 3 项补测（收敛但有失败→verifying / 放行后卡片不重复 / chip 与状态进验证）、
test_run 本机跑不通（改用 sys.executable -m pytest）、
四条硬性约束审计 + 修复（交互原生化 div role=button→原生 button / 颜色字面量→token）、
「⏸ 中断」按钮不可用修复（循环每轮检查中断 + 执行期乐观置「执行中」放开按钮）、
结论兜底文案按状态分流（interrupted→「已中断」/ 非收敛停下→「已停下」，不再一律说「执行完成」）、
实测找 bug 并修卡 1（工具原始返回回灌提示词）/ 卡 2（动作参数语义进目录 + file_list 的 path 标「目录」）、
复跑后按序修卡 8（sandbox_run 非零退出码判失败）/ 卡 9（重复失败即判空转）/ 卡 7（sandbox_run 子进程 PATH 补解释器）、
再修掉剩余卡 3（steps 状态回填）/ 卡 4（E_VALIDATION 不自查重试）/ 卡 5（列表也显示「已停下」）/
卡 6（标题带省略号）/ 卡 10（file_write.overwrite 进签名），并**复跑验证**（卡 3/4/5/6/9/10 均确认修好）
+ 新修卡 12（file_list 条目带目录前缀）、卡 11（agentic 无密钥不再静默空转，只在 /run 拦、且限定"没有可回退计划"）、
**「写代码 → 运行」全链路实机验证通过**（`python hello.py` 真出「hello from agent」验卡 7/8；
模型正确读出并覆盖写回 `agents/research_agent.py` 验卡 12/10，生成文件已 ruff --fix 干净）。
同日第二轮「先测后修」：修卡 13（test_run 真失败时带出用例与原因，不再退化成「角色执行未通过」）、
卡 15（结论不得引用本任务没产出的文件/入口，附确定性核对）、
③ 新增后端生成的「验证证据」事实行（结论声称「验证通过」时附「系统核对：真实运行 N 次…」），
卡 17（紧接着重复同一「动作+参数」的成功动作不算进展，空转闸补上"重复成功"这道）、
卡 18（观察回灌窗口 800 → 2000 字并写明原文长度 —— **这才是"反复重读同一个文件"的根因**）、
卡 5（report 只内存汇总 → 摘要如实说未落盘）、卡 6（新增 `POST /tasks/{id}/deliver` + 前端「确认交付」卡）、
卡 7（code_search 改报「命中 N 条」）、卡 14（MAX_LOOP_STEPS 12 → 20）、
并**撤回误报的卡 16**（前端本来就会在载入时与后端核对密钥状态；上次是我的按键发给了非焦点窗口）；
复跑得**首轮真正收敛**（12 步 / 测试 5 项通过 / 沙箱实跑成功，连续重复轮次 6 → 0）。
剩余：**唯一遗留 = 全部改动未推送 GitHub**（本地无 .git，只能走 REST API；已免 token 算出精确清单：
60 个内容不同 + ~81 个本地新增应推送，另有 33 个远端独有文件必须靠 base_tree 保住，
见 `.dsh-scratch/unpushed-report.txt`）。
③ 加宽后的触发条件**已于 2026-10-09 实机验证通过**（真实密钥 + agentic 收敛轮，结论里正确挂上
「（系统核对：真实运行 1 次，成功 1 次）」，且独立复跑该测试得 2 passed）。

硬性约束：roles/ 层禁止 import api/tools/llm；颜色只写 CSS token、交互一律 <button>；
会话持久化只用 sessionStorage；改前端先看 tests/test_frontend_workspace_ui.py 等静态接线测试；
基线 = ruff 干净 + pytest 全绿（当前 2077 passed / 11 skipped）。

运行与校验（项目根）：
- 后端：$env:PYTHONPATH = ".deps"; python -m uvicorn agent_builder.api.app:create_app --factory --reload --port 8000
  ⚠️ 必须带 --reload，否则改了后端不生效（踩过：新路由没加载 → Method Not Allowed）；
  ⚠️ 但**跑批 / 长测别用 --reload**：生成物里一旦出现 .py 就会重载并清掉内存里的密钥。
- 前端：python -m http.server 8080 --directory frontend
- 校验：python -m ruff check . ; python -m pytest ; node --check frontend/js/app.js
- A/B 跑批：python -m agent_builder.evaluation.mode_ab --model deepseek-chat --reps 3
  （需先在前端存密钥；密钥是内存态，后端每次重启都要重存）
- ⚠️ 前端有缓存坑：改了前端资源要 bump index.html 里的 ?v= 版本号（当前 20261104），并在浏览器 Ctrl+Shift+R
```

## 今日完成（2026-10-07）

四项，都有实测 / 测试佐证。基线 **1975 → 1999 passed / 11 skipped**，`ruff` 干净。

1. **产物面板实测 → 修 2 个 bug + 放开大文件预览**
   - #14 文件树 ↻ 刷新后折叠状态被重置 → 模块级 `collapsedDirs`（按工作区相对路径记）；
   - #15 放行后「不公地」过早判空转 → 挂起轮不记预算 + 续跑不继承 `stagnant_rounds`；
   - 大文件预览删掉 20MB 硬上限 → **任意大小只读截断预览**（始终只读前 2MB，GB 级也秒开）。
   - 浏览器实测：折叠 `agent_builder` → ↻ → 仍折叠；造 21.5MB 文件 → 打开为只读截断。详见「第六轮」。
2. **P3 执行形态 A/B（`workflow` vs `agentic`）** —— 新脚本 `evaluation/mode_ab.py` + 报告
   `docs/reports/mode-ab.md`。24 次真实运行：完成率 **agentic 100% vs workflow 83%**、成本约 **6.5×**
   → 质量优先**倾向 `agentic`**。关键差异：**agentic 错了能自我纠正；workflow 计划冻结、错一步就停在 executing**。
   详见「P3：执行形态 A/B」。
3. **旧轮次 3 项遗留补测（真实密钥 + 浏览器端到端，`?v=20261030`）** —— #2 收敛但有失败 → verifying、
   #6 放行后卡片不重复、#7 chip 与状态进验证；**三项全部通过**，该轮遗留清零。详见「验证与遗留」。
4. **`test_run` 本机跑不通** —— 真因是裸 `pytest`（本机 `pytest` 与 `python` **都不在 PATH**），
   改用 `sys.executable -m pytest`；实测 `run_test('tests/test_narrate.py')` → `32 passed`。
   详见「其他待办 #3」。

**未完成**：全部改动**仍未推送 GitHub**（本地无 `.git`，只能走 REST API）。

## 今日完成（2026-10-08）

两项，均有测试佐证。基线 **1999 → 2009 passed / 11 skipped**，`ruff` 干净、`node --check` 通过。

1. **四条硬性约束审计 + 逐条修复**（用户要求核查「是否已违反」）
   - ① `roles/` 跨层 import：**本就没违反**（`test_evaluation_agents.py::TestDependencyClarity::test_无跨层依赖`
     + `scorecards.LAYER_VIOLATIONS` 在强制）。
   - ② 颜色只写 CSS token：JS 本就没内联颜色；但 CSS 有 12 处 `#fff` + 4 处 shadow `rgba()` 未 token 化 →
     新增 `--on-emphasis` / `--toggle-knob` / `--shadow-knob|modal|card|drawer`（**值与原字面量一致，零视觉变化**）
     并全部替换；规则体里只剩 `var(--…)`。
   - ③ 交互一律原生 `<button>`：2 处 `div role="button"` → 原生元素 —— `.plugin-add`（index.html）
     与文件树行（app.js）；删掉随之变成死代码的 `activateOnKey()`；CSS 补 button 排版重置
     （`display / width / text-align / font: inherit / white-space`）。
   - ④ 只用 `sessionStorage`：**本就没违反**（`test_frontend_session.py` 在强制）。
   - **锁回归**（新增 3 条静态断言，原断言未削弱）：`test_frontend_workspace_ui.py::TestDesktopChromeIA.test_非原生按钮的role_button已清零`、
     `::TestCssColorTokens`（规则体不得有颜色字面量 + 6 个新 token 必须被引用）。
   - 注：`docs/prototypes/handover-card/app.js` 里仍有 `localStorage`（历史原型、非产品代码），**按原地不动处理**。
2. **「⏸ 中断」按钮不可用 —— 定位 + 修复（方案 A）**
   - **根因**：`updateTaskStatus` 里 `interruptBtnEl.disabled = status !== 'executing' || stopped`，而 `executing`
     前端**观察不到** —— `/run`、`/resume` 是**同步阻塞**接口（整段 agentic 循环跑完才返回），执行期间 UI 停在
     `awaiting_confirm`；跑完之后若没收敛则是 `executing + stopped_reason` → `stopped=true` 仍禁用。
     即 `executing && !stopped` **不可达** → 按钮从头到尾是灰的。
   - **附带问题**：即使能点，`/interrupt` 只翻状态机（`handle_interrupt`），**正在跑的循环不读状态、不会真的停**。
   - **修复**：
     ① 后端 `run_agent_loop` 新增 `should_stop` 回调（**每轮开头**检查）与 `STOP_INTERRUPTED`；
        `_execute_plan_agentic` 传 `should_stop=lambda: conductor.task_state.status is TaskStatus.INTERRUPTED`，
        并在该终止原因下**跳过全部收尾转换**（`STOP_PROPOSED` / `STOP_PENDING_APPROVAL` / `all_done→verifying`），
        保住用户的暂停意图；
     ② 前端新增 `markTaskRunning(taskId)`：在 `applyPlanResult` 的 `/run` 之前、`handleApprovePending` 的 `/resume`
        之前乐观置「执行中」→ 中断按钮在执行期间可用；真实终态仍由响应回来后 `applyRunResult` 覆盖。
   - **新增测试**：`tests/test_agent_loop.py::TestLoopInterrupt`（3 例：中断后不再执行后续工具 / 一开始就中断则零轮 /
     不传回调行为不变）、`tests/test_frontend_workspace_ui.py::TestInterruptWiring`（4 例）。
   - **实机端到端已验证**（真实密钥 + `deepseek-v4-flash`）：发一个只读多步任务 → 执行期间 chip 为「执行中」、
     中断按钮 `disabled=false`（**修复前必为 true**，即整段执行期间都灰着）；点「中断」→ 状态「已暂停」+ 中断卡，
     循环**在 3 步后停下**（未跑完请求的 4 项），`/run` 正常返回、**无 409** —— 证明 `all_done`（3 步全成功）时
     **没有**被误推到「验证中」，用户的暂停意图被保住。前端资源版本号已 bump 到 **20261102**。
   - **顺带发现（已修）**：中断后「结论」文案曾写「执行完成：3 步全部成功」，与顶栏「已暂停」矛盾 ——
     这是 `narrate.conclude()` 在「无模型 final 结论」时按执行结果兜底汇总的**既有**行为
     （stagnant / 预算耗尽等非收敛终止同样如此）。已按状态分流：
     `conclude()` 新增 `status` / `stopped` 两个入参（`_to_task_response` 下发真实状态与「是否非收敛停下」），
     `interrupted` → 「已中断：共 N 步，已完成 D 步」；非收敛停下（`stopped=True`）→ 「已停下：N 步均成功，但任务未收尾」；
     收敛 / 老调用方（不传）**措辞不变**；「等待放行」优先级最高不会被盖掉。
     新增 4 条用例：`tests/test_narrate.py::TestConclude`（已中断 / 中断且失败 / 待放行优先 / 非收敛停下）。
3. **实测找 bug → 修卡 1 / 卡 2**（用 `curl` 直连后端跑真实 agentic 全流程）
   - **卡 1（P0）agent 只看得到一句摘要 → 写不出代码**：`build_decision_prompt` 的「已完成」段
     只拼 `{round, role, action, status, summary}`，**从不带工具返回值**；而角色派发路径还会把
     原始输出吃进自己的结果对象（`CodeResult`）→ 模型列完目录仍不知道列到了什么，只能继续猜参数，
     两轮无进展就判空转停下（实测「调研论文 agent」跑到第 3 步 stagnant、零产物、没走到写代码）。
     **修复**：① `orchestrator.executor_fn`（执行链路唯一出口）用 contextvar 留一份原始返回
     （`take_tool_observation()`，**读取即清空**防串轮）；② `routes._execute` 取走塞进新字段
     `StepOutcome.observation`；③ `run_agent_loop` 写进 `LoopRound` 与 `context.history`；
     ④ 提示词按 `MAX_OBSERVATION_CHARS = 800` 裁剪后渲染成「观察结果」段。
     **只进提示词、不进前端**（`to_execution_results` 未动）。顺带：角色路径下摘要为空话的
     「列出文件完成」会用它补成带内容的人话（**只补这一种情况**，其它摘要不动）。
   - **卡 2（P1）参数语义缺失 → 模型反复猜路径**：`role_catalog` 新增 `ACTION_PARAM_NOTES`
     （`file_list` 的 path 是**目录**、`file_read` 是**具体文件**、都不支持通配符…），
     `RoleSpec.to_prompt_line` 把说明**另起一行**渲染进目录；`narrate.describe_inputs` 增加
     `action` 参数 —— `file_list` 的 path 现在显示「目录：」而不是「文件：」。
   - **新增 7 条用例**：`test_agent_loop.py::TestObservationFeedback`（3）、
     `test_deciders.py`（观察回灌 / 超长裁剪）、`test_role_catalog.py`（参数语义进目录）、
     `test_narrate.py`（列目录的 path 标成目录）。
   - **同轮实测发现但当时未修的卡 3/4/5/6/10**：已在第 5 条全部修掉。
4. **复跑实测 → 按卡 8 / 卡 9 / 卡 7 顺序修复**（三个「让 agent 空转烧预算 / 跑不起来」的问题）
   - **卡 8（P0）`sandbox_run` 丢退出码 → 命令失败被报成 `done`**：`run_command` 用
     `subprocess.run(..., check=False)` 后**只返回 stdout+stderr，退出码被丢掉** → 实测 8 次
     「'python' is not recognized」全报成「沙箱执行完成」，`progressed` 恒真、空转检测失效、
     一路烧到预算上限。**修复**：退出码非零 → 抛
     `tool_error("sandbox_run: 命令退出码 N\n<输出>")`（输出原样带出）；`run_step` 的两条失败分支
     新增 `observation=reason` —— **失败也把真实输出回灌给模型**。
   - **卡 9（P1）空转检测被「成功但没进展」的步骤重置**：`progressed = status == "done" or artifacts`
     把"成功但信息量为零"也算进展；实测模型在失败的 `file_read` 之间夹一次成功的 `file_list`
     就把空转计数清零，同一路径连撞 5 次直到预算耗尽。**修复**：`run_agent_loop` 增加
     **重复失败检测** —— 以「动作 + 规范化 inputs」为指纹，同一指纹失败累计到
     `MAX_REPEAT_FAILURES = 2` 即按 `STOP_STAGNANT` 停下（复用既有终止原因，前端「已停下」卡片零改动）。
   - **卡 7（P1）`sandbox_run` 子进程 PATH 里没有 python**：后端用全路径解释器启动，PATH 里没有
     python → 生成物里 `python hello.py` 直接「is not recognized」（模型为此烧了 8 步找解释器）。
     **修复**：`env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")`
     （与 `test_run`「用同一个解释器」同口径）；`docs/tools.md` 已同步。
   - **新增 6 条用例**：`test_tools_sandbox_run.py`（非零退出码判失败并带出输出 / PATH 前置解释器目录）、
     `test_agent_loop.py::TestRepeatFailureStops`（3）、`test_agent_loop.py`（失败也带观察结果）。
   - 复跑里同时确认卡 1 / 卡 2 **确实生效**：模型 thought 直接引用目录内容、`file_list` 的 path 用对、
     摘要从「列出文件完成」变成「列出文件完成（700 字）」。
5. **修掉剩余 5 张卡（卡 3 / 4 / 5 / 6 / 10）**
   - **卡 3（P1）`steps[].status` 永远是 `pending`**：steps 只在执行前落一次（`to_step` 默认
     `pending`），跑完从不回写 —— 而 `execution_results` 已明确写着 done/failed，两边对不上。
     **修复**：新增 `routes._sync_step_status(entry)`（`_STEP_STATUS_FROM_RESULT` 映射；
     「待放行」= 还没执行，保持 `pending`），agentic 与固定工作流两条收尾路径都调用。
   - **卡 4（P2）gatekeeper 审计行重复 3 条**：根因是错误码表把 **E_VALIDATION 标成可重试**
     （`NON_RETRYABLE` 只含 E_PERMISSION / E_USER_CANCEL / E_COST / E_INTERNAL），
     `code_worker` 的自测重试循环于是把**确定性**的参数错误重试 3 次 → 同一 `audit_id` 留下 3 行。
     **修复**：`code_worker` 对 `E_VALIDATION` 也不自查重试（与 `retryable=False` 同路径）。
   - **卡 5（P2）列表说「执行中」、详情说「已停下」**：`TaskSummary` 新增 `stopped_reason`
     （由 `entry.loop_state.stopped_reason` 派生）；前端 `renderTaskList` 在 `status === 'executing'`
     且原因能翻译时显示「已停下」并用 `interrupted` 配色 —— 判定与详情页**同口径**（都过 `LOOP_STOP_LABELS`）。
   - **卡 6（P2）任务标题硬截断**：`store._task_title` 超 60 字改为 `text[:60] + "…"`（原来在词中间断掉）。
   - **卡 10（P2）关键可选参数不进签名**：`role_catalog` 新增 `_EXTRA_SIGNATURE_PARAMS`
     （`file_write → overwrite`），签名变成 `file_write(path*, content*, overwrite)`；
     `ACTION_PARAM_NOTES["file_write"]` 也点明「覆盖已有文件要带 `overwrite=true`」。
   - **新增 5 条用例**：`test_roles_code_worker.py`（参数类校验失败不重试）、
     `test_agentic_mode.py`（steps 状态对齐 + 摘要带 `stopped_reason`）、
     `test_api_tasks.py`（超长标题带省略号）、`test_role_catalog.py`（overwrite 进签名）、
     `test_frontend_workspace_ui.py`（列表项也显示已停下）。
   - 前端资源版本号 bump 到 **20261103**。
6. **修复后复跑实测**（真实密钥，两个场景）
   - **已验证修好**：卡 3（`steps[*].status` 变成 done/failed，不再是清一色 pending）✅；
     卡 4（5 步 = 5 条审计行，不再 ×3）✅；卡 6（标题以「…」结尾）✅；
     卡 9（同一路径再撞一次就停 —— 5 轮结束而不是 10 轮）✅；
     卡 10（模型的决策理由里直接写了「**无需 overwrite**」，说明签名让它知道了这个参数）✅；
     卡 5（`/task-summaries` 带上 `stopped_reason=stagnant`）✅。
   - **卡 12（新，已修）`file_list` 只返回裸文件名**：返回 `[FILE] research_agent.py (1024 bytes)`，
     模型据此去读 `path="research_agent.py"`（被解析成工作区根）→「文件不存在」→ 反复重试。
     **修复**：条目带**被列目录的前缀**（`[FILE] agents/research_agent.py (… bytes)`；列工作区根时无前缀），
     模型可以把返回的名字**原样**喂给 `file_read`。新增 2 条用例。
   - **卡 11（新，已修 · 口径只收在 `/run`）agentic 无密钥时静默空转 + 任务卡死**：
     **后端重启会清空内存里的密钥**，此时 agentic 的 `/run` 落到固定工作流分支，而 agentic 的计划
     是空的（没有预分解 steps）→ `_execute_plan` 的空计划分支直接 `return`：任务停在 `executing`
     且**什么都没执行**，之后每次 `/run` 都 409「非法状态转换：executing + plan_accepted」，
     任务被彻底卡死（本次实测踩到）。
     **修复**：`/run` 在推进状态**之前**判断「agentic + 无可用模型 + **没有可回退的固定计划**
     （`entry.steps` 为空）」→ 直接 409「未配置可用的模型密钥，请先保存 API 密钥后重新执行」；
     任务仍留在 `awaiting_confirm`，存好密钥再 `/run` 就能恢复。
     **口径为什么必须这么窄**：第一版条件只写「agentic + 无模型」，结果 **15 条测试转红** ——
     它们走的是「无密钥**回退固定工作流**」这条**有效路径**（`_seed_plan` 会直接注入 `entry.steps`），
     或者纯状态机测试。补上 `not entry.steps` 后收敛到 **0 条红，且不需要改任何既有测试**：
     有 steps 时照常回退执行，只有"真的没东西可跑"才报错。
     `/approve`、`/resume` 维持原有宽容行为（严格通道语义不变）。新增 1 条用例。
   - 环境坑复现：`/run`、`pytest` 这类阻塞命令会**连带杀掉后端**（终端共用）→ 之后用**非阻塞**
     （`blocking: false` + `wait_ms_before_async`）跑 curl 就稳。
7. **「写代码 → 运行」全链路实机验证（本轮收口，两个真实任务，非阻塞 curl 直连后端）**
   - **任务 A（`hello.py`，验卡 7/8）**：`/run` → 到点暂停 → `resume` 后 `stopped_reason=final`；
     三步 `file_write`（文件已存在未覆盖 → failed，符合预期）→ `file_read`(done) → `sandbox_run`(done)，
     沙箱真实回显 **`hello from agent`** ⇒ 卡 7（PATH 前置解释器目录）与卡 8（非零退出码判失败）均生效。
   - **任务 B（`research_agent`，验卡 12/10）**：5 轮 —— R1 `file_list(".")` / R2 `file_list("agents")`
     / **R3 `file_read("agents/research_agent.py")` done**（卡 12 生效，不再「文件不存在」反复重试）
     / R4 再读一次 / **R5 `file_write` 显式带 `overwrite: true`**（卡 10 生效）。
   - 产出 `agents/research_agent.py`（agent 自写完整实现：4 个可插拔组件 + 默认实现 + `__main__`），
     `ruff check --fix` → **25 errors fixed / 0 remaining**；`pytest` **2033 passed / 11 skipped** 不受影响。
   - 环境坑复现：`/resume` 这类阻塞 curl 也会把后端一起带走（终端共用）→ 已改用非阻塞。
   - **附带产物（实测样例，非源码，可随时删）**：`hello.py`、`research_agent_report.md`、
     `research_agent_requirements.md`、`verify_fix_check.md`、`conductor_summary.md`。

### 续：同日第二轮实测（卡 13–卡 18 + 「验证事实行」③；先测后修，每张卡都有实测证据）

> 方法：用 computer-use 在真实浏览器里发同一个需求「帮我做一个调研论文的 agent，从制定方案、执行方案、写代码，到运行验证，产出可运行的代码」，
> 逐轮抓 `GET /tasks/{id}` 的 `loop_state.rounds` 定位问题，修完再复跑（run-1 ~ run-7）。

**卡 13（P1）`test_run` 真失败时原因被吞** —— 退化成「角色执行未通过」，模型看不到真实报错。
修复：`roles/test_runner.py` 失败分支带出「失败 N 项 / 错误 N 项 + pytest short summary 的 FAILED 行（≤5 条）+ 集合期报错的 `E` 行」；
`api/orchestrator.py` 的 `_outcome_detail` 改成**只接受非空字符串**（原先 `error` 字段是「错误用例数（int）」，`error > 0` 时原因会退化成字符串 `"1"`）。
**实机**：失败原因变成「测试未通过：失败 2 项…research_paper_agent/test_agent.py::test_agent_plan…」，模型据此两轮修好接口不匹配。

**卡 15（P1）结论引用没产出过的文件** —— run-4 真写出了 `research_agent/{agent,test_agent}.py` + `README.md`，
结论却写「用法：`python -m research_agent.main`」，而 main.py 根本不存在。
修复：`api/deciders.py` 规则 7 补「answer 里的文件名 / 运行命令必须真的在【已产出】里」；
`narrate.py` 加**确定性核对** `_unproduced_refs()` —— 只在**本任务产出过的目录内**比对
（产出目录之外的引用，如「参考了 docs/HANDOVER.md」，不报警），命中则附「（说明：本任务未产出 X；实际产出 Y）」。

**③ 后端生成「验证证据」事实行** —— 结论声称「验证通过」时，附上后端核对出的真实运行次数 / 成败；
一次没跑过就直说「「验证」仅为代码内的自述」（实机产出的 `verify()` 就是 `bool(steps) and bool(code)` 这种自证式判断）。
修复：`narrate.py` 新增 `_verification_fact()` + `_claims_verification()`，`_EXEC_ACTIONS` 与
`evaluation.tool_report.EXEC_TOOLS` 加同步断言防漂移。
⚠️ **触发条件是第二轮才调对的**：首版卡「验证/测试/校验」与「通过/成功」间隔 6 字以内，而真实结论写成
「验证证据：1) pytest 运行 tests/…，5 项全部通过」，中间隔着路径和逗号 → 漏判；已改成**同一句内**出现即算（保留「未通过/不通过」排除）。
**加宽后的版本已于 2026-10-09 实机验证通过**（真实密钥 + 真实后端 + agentic 收敛轮）：
任务 3 轮收敛（`stopped_reason=final`，状态 `verifying`），模型结论写成
「…用 pytest 真实运行结果：collected 2 items，test_add PASSED、test_mul PASSED，2 passed in 0.02s，测试全部通过。」，
后端**正确追加**「（系统核对：真实运行 1 次，成功 1 次）」；独立复跑 agent 写出的
`_sample_backup/verify_fact_check/` 得 **2 passed**（它的自述属实）。
另有确定性打靶 8/8 通过（含「测试未通过 / 测试不通过 / 验证失败」负例不加事实行），
报告见 `.dsh-scratch/verify-fact-report.txt`、实机日志见 `.dsh-scratch/live-verify-3-report.txt`。

**卡 17（P0）重复的「成功」动作被算作有进展 → 空转检测失效** —— 根因 `StepOutcome.progressed = status == "done" or bool(artifacts)`
把「成功」等同于「有进展」。**实测（run-6）**：模型连续 **5 次** `file_read` 同一个 4.5k 的 `agent.py`，
每轮都算成功 → 空转计数被反复清零 → 14 步里 6 步白烧在重读上；它早已诊断出「实现与测试签名不匹配」却没有轮次去改。
修复：`api/agent_loop.py` 新增 `repeated` 判定（紧接着重复同一「动作 + 参数」且无新产物 → 不算进展），
取上一轮指纹时跳过 `pending_approval` 轮。

**卡 18（P0）观察窗口 800 字装不下源码结构 —— 这才是反复重读的根因**
`MAX_OBSERVATION_CHARS = 800`，而实测量出来的数字是：`research_agent/agent.py` 共 **4539** 字，
`class ResearchAgent` 在**第 843 字**、`__init__` 签名在**第 944 字** —— 全在 800 窗口之外，
模型**无论读多少次都看不到那两行**（它的思考写着「需先看清 agent.py 中 ResearchAgent 的真实签名」）。
修复：上限 **800 → 2000**；截断标记从「…（已截断）」改成**写明原文长度**「…（原文 4539 字，此处只显示前 2000 字）」；
`agent_loop` 里的重复实现删除，改为复用 `deciders._clip_observation`（单一实现）。

**复跑验证（run-7）：首轮真正收敛** —— `stopped_reason=final`、状态「验证中」：
1 写实现 → 2 测试失败 → 3 写测试 → 4 测试失败 → 5~7 读文件 ×3（**参数各不相同**）→ **8~9 改实现** →
10 读 → 11 测试**通过** → 12 沙箱实跑。
**连续重复轮次 6 → 0**；它声称「5 项全部通过」，独立跑 `tests/test_research_agent.py` 得 **5 passed**（属实）。

**卡 16 撤回（误报）** —— 曾报「后端重启后前端仍显示『密钥已配置』」。实为误判：前端本来就在页面载入时调
`loadApiKeyStatus()`，`updateStatusKey()` 有明确的 else 分支「⚿ 未配置密钥」；上次截图里的「已保存」
是**页面从未真正重新加载**造成的（`press_key` 发给了非焦点窗口，AX 观察当时已提示焦点在 TRAE）。
**教训（重要）**：用 computer-use 驱动浏览器前**先点窗口内区域夺焦、再按键**。
已实机证伪：全新后端 `configured=false` → 页面正确显示「输入 API 密钥」+「⚿ 未配置密钥」。

**其余**：卡 5（`report` 只在内存汇总不落盘 → 摘要如实写「仅内存汇总，未落盘」）；
卡 6（新增 `POST /tasks/{id}/deliver` + 前端「确认交付」卡，**实机点通**：`/deliver` 200 → chip / 列表 / 状态栏三处变「已交付」）；
卡 7（`code_search` 改报「命中 N 条」，不再报「198.1k 字」）；卡 14（`MAX_LOOP_STEPS` 12 → 20）。

**顺带**：`pyproject.toml` 的 ruff `exclude` 增加 `_sample_backup`（实测产物草稿目录，非项目源码）——
否则每跑一轮都要为了迁就 lint 去改**被测方**写出来的代码。

### 环境注意（本轮新增）

- 本机实测**必须用非阻塞**方式跑命令，否则终端共用的进程会被带走；分离进程（`Start-Process`）也会被回收。
- 后端**没带 `--reload`** 时改代码不生效，需手动重启；而重启会清空内存里的密钥（每次都要重存）。
- **2026-10-09 起在 DeepSeek Harness (DSH) 里干活，两条 harness 相关的坑**：
  ① DSH 沙箱下直接跑 `pytest` 会得 `1728 passed / 349 errors` —— 全是 `tmp_path` 夹具的
  `PermissionError WinError 5`，根因是 Python `os.mkdir(mode=0o700)` 写的显式权限绕过了沙箱继承授权，
  连创建者自己都列不了该目录（`os.mkdir(0o755/0o777)` 与 PowerShell `New-Item` 都正常）；
  用「PowerShell 预建临时根 + 只放宽临时目录 0o700 的 shim」可跑出 **2077 passed / 11 skipped**，
  配方与证据见 `.dsh-scratch/README.md`。
  ② 浏览器自动化已接入（Playwright MCP 驱动本机 Chrome，工具名 `mcp__browser__*`），
  安装/验证/回滚见 `.dsh-scratch/BROWSER-MCP.md`。

## 今日完成（2026-10-09）：让 agent 真正跑通 + 仓库对齐

用户诉求原话：「**必须要，让 Agent，跑出更高的质量**」；起因是原任务「帮我做一个论文调研 agent」反复跑不通。

### 结果对比（同一需求、同一工作区）

| | 失败那轮（`cea128b8`） | 现在 |
|---|---|---|
| 终止 / 状态 | `stagnant` / `failed` | **`final` / `verifying`** |
| 步数 | 8 步（0 产物） | 11 步（预算 20，空转 0） |
| 测试 | 一次没通过 | **9 项全部通过**（独立复核一致） |
| 结论 | 兜底「已停下」 | 模型自己的完整交付说明 + 系统核对事实行 |

### 11 处根因（每处都有实测证据 + 回归测试）

| # | 现象（实测） | 根因 | 修复 |
|---|---|---|---|
| 1 | 模型连续 **4 次**读同一个 6.6k 文件，思考里写「需要看到 `__init__` 的真实签名」，8 步 0 产物空转 | 观察窗口只有 2000 字，**大文件尾部永远读不到** | `file_read` 加 `start_line/end_line`；裁剪时附**全文符号轮廓** |
| 2 | 模型一次要 `start_line=65/end_line=306`（241 行）→ 又被截断 → 又读不全 | 截断提示只说"已截断"，没给可操作信息 | 提示写明「原文 N 字 / 共 M 行 / 看的是前 K 行 / **一次 ≤50 行**」 |
| 3 | R3 因 `ImportError` 失败 → 模型重写测试 → R7 对同一 target 跑出**新症状**，却被判"重复失败"掐死 | 失败指纹只取前 200 字，而 pytest 失败原文**开头永远是「失败 N 项；〈测试名〉」**，真正区分病因的异常在**尾部** | 指纹覆盖**整条**失败原文（并抹掉 pytest 耗时噪声） |
| 4 | 连读 6 轮、思考里根因全对，却**一步不改**；第 9 步又跑同一个 `test_run` | 没有任何机制要求它"必须动手" | **系统纠正**：连续 ≥3 轮只读且验证仍失败 → 提示词里硬性要求给出改动或 `kind=final` |
| 5 | 模型发 `content="PLACEHOLDER"` 的覆盖写，把**已通过验证的实现整个抹掉** | `file_write` 接受任意内容；模型在"重写 16.6k 字符"压力下退化成占位符 | **占位符护栏**：整份内容是占位符 → 拒绝写入（文件一字节不动，标不可重试） |
| 6 | 结论对**已产出**的 `paper_survey_agent/test_agent.py` 报「本任务未产出」 | 产物名在**比对之前**就被截断成 `…/test_agent.py`，逻辑与展示混用同一个值 | 逻辑用完整路径；短化只发生在展示处 |
| 7 | 改一个函数签名只能整份重写 473 行 / 16.6k 字符 → 退化成占位符 → 干脆不写 | **没有局部修改能力** | 新增 **`file_edit`**（精确替换 + 换行等价匹配 + 改后回灌符号轮廓） |
| 8 | `file_edit` 接进权限矩阵与派发表后**仍然**报「任务超出代码范围」 | 角色**实现层**还有第三份清单 `CODE_ACTIONS`/`DOC_ACTIONS` —— **三层都接才算可用** | 三层对齐 + 补"走角色派发"的回归测试（原来只直接调工具，所以漏了） |
| 9 | `file_edit` 在真实文件上 4 次全部「找不到 old_string」，而 `code_search` 明确指出该行存在、文本一模一样 | `file_write` 在 Windows 上把 `\n` 写成 **CRLF**，`file_read` 读回来是 **LF** → 精确匹配永远失败 | `file_edit` 按**换行等价**再匹配（并保持原文件换行）；`file_write` 不再做平台换行翻译 |
| 10 | `code_search` 直接失败（`ripgrep 未安装或不在 PATH`）→ "搜代码定位函数"的核心动作全废；一轮里连撞两个失败 → 空转闸门第 3 步就掐死 | 把**可选外部二进制**当硬依赖 | 找不到 rg → 退到**内置纯 Python 扫描器**（有界、跳过隐藏/依赖目录、同格式输出并注明换了扫描器） |
| 11 | agent 自己写的 `test_cli_end_to_end` 一直红：`PermissionError [Errno 13]`；模型思考里已正确识别"是 tmpdir 权限"，但**它修不掉** | 受限令牌下 `os.mkdir(0o700)` 写的显式权限连创建者都打不开（与 DSH 沙箱同因） | `test_run` 注入**受限环境兼容层**（只放宽临时目录下的 0o700）+ 子进程临时目录放进工作区 |

### 护栏清单（**改这一层之前请逐条对照**）

- **能力要接三层**：权限矩阵（`tools/permissions.py`）、派发表（`api/orchestrator.py: ACTION_ROLE_MAP`）、角色范围（`roles/*.py: CODE_ACTIONS`/`DOC_ACTIONS`）。漏任何一层 → 工具"存在但对模型不可见"或"可见但被角色拒绝"。
- **确定性优先于提示词**：模型会犹豫、会退化。凡是"必须发生"的事（不许写占位符、必须动手、不许重复同一验证）都做成**确定性护栏**，提示词只做补充。
- **失败指纹必须覆盖整条原文**：截断到开头会把不同病因判成同一症状，引发误杀（#3 就是这么来的）。
- **"症状变了"就是进展**：重复失败闸门**与**空转计数器必须同一口径（新症状 → 清零），否则"改一处 → 跑一次 → 再改"的正常循环会被掐死。
- **"会写盘的动作"清单有多处**：`api/artifacts.py: _FILE_PRODUCING_ACTIONS`、`roles/code_worker.py: FILE_PRODUCING_ACTIONS`、`narrate._WRITE_ACTIONS` —— 新增写盘动作时都要同步，否则产物不计入【已产出】、摘要还会漏。
- **跨平台换行**：写盘不做平台翻译；读取/匹配要容忍 LF↔CRLF。**测试样本也要按平台给**（实测：`C:/Windows/evil.py` 在 POSIX 上并未越界 → CI 失败）。
- **可选外部依赖必须有兜底**：`rg`（`code_search`）、临时目录权限（`test_run`）都在这一条上翻过车。
- **人话文案有一致性测试**：新增动作要补中文名（`narrate.ACTION_TITLES`）与人话化分支，否则摘要会漏出英文机器输出。
- **产物要落 `outputs/`**：工作区常常就是本项目仓库根 —— **新**交付物统一放 `<工作区>/outputs/`（**改已有文件仍写回原路径**），
  并且 `pyproject.toml` 的 `[tool.ruff] exclude` 与 `testpaths = ["tests"]` 已把产物排除出 lint / 测试范围
  （实测动机：agent 写出的 `paper_agent.py` 会让本地 `ruff check .` 直接失败）。详见 `docs/agentic-loop-design.md` §13。

### 验证方法（**这一段最值得复用**）

1. **镜像 worktree 复现 CI（决定性）**：`git worktree add --detach .dsh-scratch/ciN HEAD` → 拿到与 CI **完全相同的树**（含只在远端的文件），再用与 CI 同版本的 ruff/pytest 跑 `ruff check .` + `pytest`。
   - 反面教训：直接在**工作区**跑测试会得到 6 failed + 1 error 的**假象**（`agent_builder/contracts/messages.py`、`NOTICE`、`PRIVACY.md`、`SECURITY.md` 等只在远端的文件缺失），很容易把人带偏。
2. **CI 日志没权限时用注解取证**：拿不到 `actions:read` 时，临时经 `pyproject` 的 `pytest11` 入口挂一个只上报的插件，把失败写成 `::error` → GitHub 注解**公开可读**，可直接定位失败用例。
   - 坑：`pytest_runtest_logreport` 里的 `print` **会被 pytest 捕获吞掉**，只有 `pytest_terminal_summary` 的 print 会进 CI 日志。
3. **只读核对远端**：`git ls-remote` 看真实 HEAD（本地缓存的 `origin/main` 可能是旧的）；GitHub API `/commits/<sha>/check-runs` 看结论、`/check-runs/<id>/annotations` 看明细。

### 操作陷阱（都亲自踩过）

- **绝不对已推送的提交 `--amend`**（会被判 `non-fast-forward` 拒收）→ 在已推送提交**之上追加**新提交。
- **不要用 PowerShell 做源码文本往返**：PS 5.1 的 `Get-Content`/`WriteAllLines` 默认非 UTF-8，会把中文字符串读坏（实测把 `test_contracts.py` 写成 `unterminated string literal`）→ 一律用 Python `utf-8` + `newline=""` 读写，并 `ast.parse` 自检。
- **RUF100**：写无用的 `# noqa` 会让 `ruff check .` 失败；而 lint 失败会让 pytest 被 **skip**（看起来像"测试没跑"）。

## 基础运行环境

- **操作系统**：Windows + PowerShell
- **项目根目录**：`c:\Users\李陶\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\work-mode-projects\6abcee34807a00aa83da2398`
- **Python**：`"$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe"`（3.14），ruff 0.16.9
- **依赖装法**：沙箱禁止写解释器 `site-packages` 的 `Scripts\*.exe`，故用 `pip install --target .deps` + 启动时 `PYTHONPATH=.deps`；`pyproject.toml` 的 ruff 已 `exclude = [".deps", "_sample_backup"]`（后者是实测产物草稿目录，非项目源码）
- **后端**：FastAPI + uvicorn，端口 **8000**；CORS 白名单**仅** `http://127.0.0.1:8080`（换端口会被 CORS 拦，本会话验证时踩过）
- **前端**：纯静态 HTML/CSS/JS（无构建、无 npm），静态服务器 **8080**
- **GitHub**：`Litao-66-bit/li-tao-agent-builder`，branch `main`。
  **2026-10-09 起本地已有 `.git`**（在项目根 `git init` 建的，父提交直接站在远端 HEAD 上，因此推送是快进、远端独有文件自动保留）：
  - **HTTPS 必须带 `-c http.sslBackend=openssl`**（默认 schannel 报 `SEC_E_NO_CREDENTIALS`）；SSH 在 DSH 沙箱里**跑不起来**（`sh.exe: couldn't create signal pipe`）；推送网络很抖（约 1/3 成功）→ 必须带重试。
  - **fine-grained PAT**：`Administration: Read and write` ≠ 能推代码，要的是 **`Contents: Read and write`**；"Public repositories" 模式天生只读。
  - 只加白名单文件、**绝不 `git add -A`**（会把远端独有文件记成删除）；agent 产物（`paper_agent.py` 等）与 `.deps/`、`_sample_backup/`、`.dsh-scratch/`、`.trae/` 都排除在外。
- **语言**：代码注释 / commit message / 文档一律中文

## 启动与校验命令

```powershell
# 后端（项目根目录）—— 务必带 --reload
$env:PYTHONPATH = ".deps"
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m uvicorn agent_builder.api.app:create_app --factory --reload --port 8000

# 前端（项目根目录）
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m http.server 8080 --directory frontend

# 校验
$env:PYTHONPATH = ".deps"
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m ruff check .
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m pytest
node --check frontend/js/app.js
```

**当前基线：`ruff` 干净、`pytest` 2077 passed / 11 skipped、`node --check` 通过。**

## 前端缓存注意事项（本会话反复踩，务必知道）

`python -m http.server` **不发 `Cache-Control`**，浏览器按启发式缓存，会出现「新 HTML + 旧 JS」→ 旧 JS 去操作已被删除的 DOM（如旧的 `#stageBar`）→ `TypeError`，页面看起来「坏了」。

- `frontend/index.html` 的三处静态资源已带版本号：`styles.css?v=` / `js/api.js?v=` / `js/app.js?v=`（**当前 20261104**）
- **改任何前端资源后必须 bump 这个版本号**（三处保持一致），否则用户浏览器会继续用旧缓存
- 用户侧：`Ctrl+Shift+R` 一次；或 F12 → Network 勾 **Disable cache**（之后就不用再强刷）
- 后端改动**必须重启或带 `--reload`**：本会话因为没带 `--reload`，新加的 `DELETE /tasks/{id}` 没加载，前端报 `Method Not Allowed`
- ⚠️ 后端任务列表与 API 密钥**都只存内存**，重启即清空 → 重启后需要重新填密钥

## 当前架构与状态

### 后端（`agent_builder/`）

**任务状态机**（`contracts/state_machine.py`，纯转换表、无副作用、仅 14 处引用）：
`received → planning → awaiting_confirm → executing → verifying → reworking → delivering → delivered`；异常分支 `interrupted` / `failed`；`INTERRUPT` 仅 `executing→interrupted`，`RESUME` 仅 `interrupted→executing`，`ABORT` 任意态可终止。

**关键端点**：

| 端点 | 语义 |
|---|---|
| `POST /tasks` | 建任务 → planning；响应含 `title` / `pending_approval` |
| `GET /tasks` | **仅返回 id 字符串列表**（既有契约，勿改） |
| `GET /task-summaries` | 任务摘要列表（`title`/`status`/`updated_at`/`has_pending_approval`），供前端多任务列表 |
| `GET /tasks/{id}` | 详情（含 `plan` / `execution_results` / `high_risk_actions` / `title` / `pending_approval`） |
| `DELETE /tasks/{id}` | 删除任务（内存条目；404=不存在） |
| `POST /tasks/{id}/plan` | **agentic + 有密钥：不再分解 DAG**（空 steps，转 awaiting_confirm，回显 `mode`）；否则走分解 → awaiting_confirm，返回 `steps` / `order` / `parallel_groups` / `pending_questions` / `high_risk_actions` / `council` |
| **`POST /tasks/{id}/run`** | **柔性主入口**：默认直接跑（也接受 `planning` 直跑）；仅当存在**未放行**高风险步骤时「到点暂停」→ `interrupted` + `pending_approval{tools,steps}`（不执行、不写文件） |
| `POST /tasks/{id}/approve` | **保留的严格前置确认通道**（省略 `approved_tools` = 不授权任何高风险工具） |
| **`POST /tasks/{id}/resume`** | 带 `approved_tools` 放行后**累积**到任务级授权集合再续跑；仍有未放行则再次挂起；**用户手动中断**的恢复只翻转状态、不重放计划 |
| `POST /tasks/{id}/reject` / `interrupt` / `abort` | 改计划 / 中断 / 放弃 |
| `GET /tasks/{id}/usage` | 任务级 token 计量（A/B 用） |
| `GET/POST /tasks/{id}/handover[/ack]` | 交接提示（规则式，不调 LLM） |
| `POST /tasks/{id}/council` | 评审会 |
| `/workspace/files`、`/workspace/file`、`/settings/api-key*`、`/projects*` | 产物树 / 文件读写 / 密钥 / 项目工作区 |

**高风险审批门（方案 A，已接线）**：`file_write` / `file_delete` / `git_commit` / `rollback` 属高风险，一律 `Approval(required=True)`，只有被显式放行才带 `granted_by` 通过门卫；未放行**不再判失败**，而是被 `/run` 拦成「到点暂停」。

### 前端（`frontend/`，本会话已重做 IA）

**信息架构**：标题栏（窗口控制为纯视觉装饰）→ 菜单栏（文件/编辑/视图/任务/帮助，含快捷键）→ 工具栏（**单一状态标签** `#taskStatusChip`，取代原六阶段条）→ 三栏（左：**任务列表** ｜ 中：**工作区** ｜ 右：**产物面板**）→ 底部设置/输入 → 状态栏。

- **任务列表**：`GET /task-summaries`，显示标题/状态/「待放行」；点击切换回看；**每项带 ✕ 删除**
- **工作区**：对话流 + **过程块**（`已深度思考（用时 N 秒）`，收纳计划区块 / 工具卡 / 思考 / 自检；默认收起）+ **结论块** + 提醒卡（放行/改计划/放弃）+「暂无可用计划」卡
  - 计划区块（仅 `workflow` 模式）：按 `order` 渲染 steps：id / action / 输入摘要 / 依赖 / 高风险标记 / 执行状态 /「查看产物」
  - 产物入口行：工具卡折进过程块后，「查看产物」在正文另留一条（刻意冗余，不是重复渲染）
- **产物面板**：文件树 + 查看器；工具卡都有「查看产物」入口；含 ``` 的结果用 `<pre><code>`（一律 `escapeHtml`）
- **通用确认弹窗**：`openConfirmDialog({title, message, confirmText}) → Promise<boolean>`，复用 `.project-dialog`；默认焦点给「取消」，Esc / 点遮罩 = 取消，关闭归还焦点。删除任务已接它
- **LLM 拆分默认开启**（`#llmToggle` 带 `checked`）

**语义要点**：`/plan` 后**自动 `/run`**（不再要求先点确认）。**0 步**分两种：agentic 下 0 步是**正常的**（后端不再预分解 DAG）→ 直接执行；只有后端**明确给了 `pending_questions`**（真走不下去）才渲染「暂无可用计划」卡（原样展示 + 重新规划/改计划/放弃入口），**不再**出现「未启用 LLM 拆分」这种替后端瞎猜的归因。

### 已归档的结论（历史，只需知道结论）

- **前端可访问性/主题化/响应式整改**（P1×2 / P2×5 / P3×6）+ 复审 11 项：**全部已修复**（对比度走 token、`<button>` 化、焦点接管、触摸目标 ≥24px、暗色主题、窄屏抽屉…）。教训：`display:none` / `opacity:0` 会把原生控件移出无障碍树。
- **角色简报（role brief）**：三档 `off`/`core`/`full` 已实现，**结论 = 不采纳 `core`/`full`，维持 `off`**（四项 P0 全测完：编造率人工复核、返工率无收益、人工盲评两轮强制排序 `off` 1.07 vs `core` 2.43、p≈0.0001）。细节见 `docs/reports/role-brief-ab.md`。
- **交接提示（handover）**：已实施。门控 = 轮次 >5 或 token ≥12000 或字符 ≥24000；每会话限 3 次；`ack` 记 seen/ignored；卡片不写入 transcript。
- **评审会（council）**：已实施，`chair 汇总 + 用户拍板`。
- **项目（工作区）选择**：已实施，`data/projects.json`（已 gitignore）。
- **前端审计用的旧六阶段条**：**已彻底删除**，换成单一状态标签（用户明确要求：不要固定流程）。

## 未推送状态

- **当前状态（2026-10-09 晚更新）**：`main` 已推进到 **`fd0f948`**，该提交的 CI **全绿**（3.10 / 3.11 / 3.12 三个 job 全 success）。
  另有**本地已验证、待推送**的提交（本轮文档/lock 对齐 + 本文件）：
  `e1ee7a9`（README 按事实更新 + requirements.lock 与 pyproject 对齐）、
  `2ad2e7d`（NOTICE 去掉 langgraph 声明 + `tests/test_p2.py` 的 lock 一致性断言 + 清理失效注释）。
  - **待推送原因**：PAT 在最后一步已失效（GitHub 返回 `remote: Invalid username or token`）。
  - **补推方式（二选一）**：① 换新 PAT（`Contents: Read and write`）后 `git push`（记得 `-c http.sslBackend=openssl` + 多层重试）；
    ② 在 clone `C:\Users\李陶\li-tao-agent-builder`（SSH 通道、**不需要 token**）里跑
    `git fetch "<本项目根目录>" main` 然后 `git push origin FETCH_HEAD:main` —— 是**快进推送**。
- 下面这几段是 **2026-10-09 早些时候（推送之前）的历史盘点**，保留作为"当时如何逐文件核对差异"的方法记录：
- 本会话全部改动**未推送**（当时本地无 `.git`）。想确认「到底哪些没推」，最可靠的做法是拉取远端 `main` 的 tree 与本地文件逐一比对。
- **2026-10-09 已按上述办法精确盘点**（免 token：远端 `main` 的 tree 与本地逐文件比 git blob 哈希，
  脚本 `.dsh-scratch/diff_remote.py`，四类完整清单 `.dsh-scratch/unpushed-report.txt`）：
  远端 HEAD `33eb2aa`（最后推送 2026-09-30 14:17）、157 个文件；**内容不同 60 个**、
  **本地新增应推送 ~81 个**（另有 21 个 `_sample_backup/` 实测产物与 `data/projects.json` **不该推**；
  两个名字带 `key` 的盲测报告已扫描，**无真实密钥**）、仅行尾差异 0 个。
- ⚠️ **远端有 33 个本地不存在的文件**（`README.md`、`LICENSE`、`NOTICE`、`PRIVACY.md`、`SECURITY.md`、
  `.github/workflows/ci.yml`、`agent_builder/core|graph|facts/**`、`docs/contracts/**`、`tests/test_smoke.py` 等，
  已逐个抽验）→ 推送**必须**用 `base_tree=远端 HEAD tree`，否则会把这 33 个文件从远端删掉。
- `.gitignore` 2026-10-09 补了两条：`_sample_backup/`、`.deps/`（原先只忽略了 `data/projects.json`、
  `.dsh-scratch/`）—— 已用真实临时 git 仓库 `git check-ignore` 验证三条忽略全部生效。
- 推送方式（用户手动）：REST API —— `git credential fill` 取 token → blob → tree(base_tree=远端 HEAD tree) → commit → PATCH ref。
- 更早就未推送的还有：**密钥管理全链路**、**密钥安全加固（CORS/本机标识头/指纹/verify 探针）**、「副结构自检」开关。
- 2026-10-08 新增未推送文件：`frontend/index.html`、`frontend/js/app.js`、`frontend/styles.css`（约束修复 + 中断修复）、
  `agent_builder/api/agent_loop.py`、`agent_builder/api/routes.py`、`agent_builder/api/deciders.py`、
  `agent_builder/api/orchestrator.py`、`agent_builder/api/role_catalog.py`、`agent_builder/narrate.py`、
  `agent_builder/tools/impl/sandbox_run.py`（卡 7 / 卡 8）、
  `agent_builder/tools/impl/file_write.py` 未动但相关：`agent_builder/api/store.py`（卡 6）、
  `agent_builder/api/schemas.py`（卡 5）、`agent_builder/roles/code_worker.py`（卡 4）、
  `agent_builder/tools/impl/file_list.py`（卡 12）、
  `tests/test_agent_loop.py`、`tests/test_deciders.py`、`tests/test_role_catalog.py`、
  `tests/test_tools_sandbox_run.py`、`tests/test_tools_file_list.py`、`tests/test_roles_code_worker.py`、
  `tests/test_agentic_mode.py`、
  `tests/test_api_tasks.py`、`tests/test_frontend_workspace_ui.py`、`tests/test_narrate.py`、
  `docs/tools.md`、`docs/HANDOVER.md`。
- 实测新增（非源码，可保留或忽略）：`agents/research_agent.py`（agent 自写并已 ruff 干净）、
  `hello.py`、`research_agent_report.md`、`research_agent_requirements.md`、
  `verify_fix_check.md`、`conductor_summary.md`。

## 输出形态重构（自然语言化 + 思考折叠）—— ✅ 已完成

### 1. 用户诉求（原话）

> 「希望的执行计划，是一套输出文本，而不是代码，并且我希望，这些思考过程压缩掉成像是 Deepseek 网页版的那种输出形式」

### 2. 现状证据（四类「机器输出」，均已截图确认）

| # | 现象 | 出处 |
|---|---|---|
| ① | 工具卡直接显示**原始 JSON dict**：`{"step_id": "step-008", "status": "done", "files_changed": ["research_agent_report.md"], "change_desc": "…"}` | `orchestrator._to_str()` 把结构化结果 JSON 序列化进 `StepResult.result`，前端 `renderToolCard` 原样显示 |
| ② | 计划区块显示**英文 action 名**：`step-003  web_fetch` / `file_write` / `test_run` | 计划区块直接渲染 `step.action` |
| ③ | 输入参数是 **`key=value` 机器格式**：`输入 url=https://…` / `test_path=tests/ pattern=test_research_agent.py` | 计划区块渲染 `step.inputs` 原始键值 |
| ④ | 失败信息是**异常原文**：`RuntimeError: AgentError: web_fetch: HTTP 错误: 404 Not Found`、`[stderr] python is not recognized…` | 后端错误字符串直接透传 |

并且：**所有中间过程（工具卡 / 步骤明细 / 自检）平铺在对话流里**，没有折叠。

### 3. 改造方案

**A. 输出文本化（后端为主）—— ✅ 已完成**

1. 在 `agent_builder/` 增一层「人类可读摘要」，与现有 `_outcome_detail()` 的兜底思路一致但产出**自然语言**：
   - `StepResult` 增加 `summary: str` —— 一句话人话，如「已抓取示例站点并保存约 1.2k 字摘要」「已创建 research_agent_report.md，含 2 个待补充章节」
   - 原始 `result` / `error` **保留**（供「查看原始数据」与审计），但**不再作为默认展示**
   - 按 action / 角色实现摘要器；失败也要人话：把 `E_*` 错误码 + 原始异常映射成「网络超时，重试 2 次未成功」
2. 计划文本化：
   - 后端给每个 step 产出 `title` / `description`（自然语言），而不是只给 `action` + `inputs`
   - 前端用**中文动作名**（`file_write` → 「写入文件」），输入参数人性化（`path=…` → 「文件：research_agent_report.md」），原始键值只进「查看原始数据」
3. 保留「查看产物」入口（折叠后仍要可达）

**实现落点**：`agent_builder/narrate.py`（`action_title` / `describe_step` / `describe_inputs` / `summarize_result` / `humanize_error` 纯函数）；`StepResult.summary`（`api/schemas.py`）、`Step.title` / `Step.description`（`contracts/schemas.py`）；`orchestrator.run_plan()` 生成 summary；**`agent_loop.to_step()` 复用同一个 `describe_step()`**（agentic 与固定工作流不再各写一套人话）；`handover.collect_open_items()` 的机器未决项也补 `title`（中文动作名）+ 人话 `detail`。前端在 `app.js` 的 `summarizeInputs` / `actionTitle` / `humanizeError` / `toolCardDetail` 四处收口，交接卡的机器未决行与复制文本同样走 `title || action`。静态断言见 `tests/test_frontend_workspace_ui.py::TestNoMachineOutput`、`tests/test_agent_loop.py::TestToStep` 与 `tests/test_api_handover.py::TestCollectOpenItems`。

**B. 思考折叠（前端为主，形态对齐 DeepSeek 网页版）—— ✅ 已完成**

1. 新增「过程块」组件：一行摘要 + 可展开内容
   - 摘要行示例：`已完成 6 步 · 失败 2 次 · 用时 12 秒`（可点击展开）
   - 折叠内容按时间序收纳：工具卡、步骤明细、副结构自检
2. **默认收起**；对话区默认只留：用户需求、**结论性回答**、需要用户决策的卡片、产物入口
3. **必须自动展开的例外**（不能让用户错过）：有失败、有「待放行」、有「需要你决定」
4. 计时：用任务 `created_at` / `updated_at` 算总耗时；单步耗时后端暂无数据 → 先只显示「步数 + 失败数」
5. 视觉沿用现有 token 与 `<details>` 折叠习惯，不引入新色

**实现落点**：`app.js` 的 `ensureProcessBlock` / `mountProcessNode` / `refreshProcessBlock` / `shouldExpandProcess` / `taskElapsedText`；`styles.css` 的 `.process-block` 系列；静态断言见 `tests/test_frontend_workspace_ui.py::TestProcessBlock`。

**两处与上面原文的偏差（有意为之，需知悉）**

- **步骤明细没有折进去**：计划区块在 `#planSlot`，它本来就是「结构化区块、不入对话流、不参与裁剪」，且是确认计划的主交互面 —— 再默认收起等于在核心流程上多加一次点击。真要折，说一声。
- **产物入口另起一行**：工具卡折进去后，卡上的「查看产物」按钮会被一起藏起来，所以正文额外补一条 `.artifact-row`（规格里"产物入口要留在默认视图"）。等于同一产物有两个入口（卡内 + 正文），这是刻意的冗余，不是重复渲染。
- **过程块自身是 `.chat-item`**：`trimChatArea` 只认 `:scope > .chat-item`，容器算一条、内部节点去掉该类 → 裁剪不变式不受影响（有断言钉住）。
- **自动展开记账一次**（`data-auto-opened`）：例外情况自动展开，但用户手动收起后不会被下一轮刷新反复弹开。

**C. 验收标准**

- 上表四类机器输出**全部消除**（①原始 JSON ②英文 action ③`key=value` ④异常原文）✅
- 默认视图能一眼看懂「做了什么、成没成、要我做什么」✅
- `ruff` 干净 + `pytest` 全绿 + `node --check` 通过；静态接线测试同步更新（不得削弱）✅
- 用真实任务在浏览器里跑一遍并截图确认 ✅（真实密钥下跑通 9 轮 agentic 循环）

**D. 实跑补齐（真实端到端跑通后追加，均已修）**

| 问题 | 类别 | 处置 |
|---|---|---|
| `重新规划失败：[object Object]` | ⑤ 机器输出（后端 `detail=exc.to_dict()` 是**对象**，FastAPI 422 是**数组**，`new Error(detail)` 直接退化） | `frontend/js/api.js` 新增 `apiErrorText(data, status)` 统一收口三种形态；`renderExecutionResults` 等处的 `err.message` 因此恒为人话 |
| `读取文件失败：E_VALIDATION: file_read: 路径不是文件: tests` | ④ 残留（大写错误码前缀 `E_*:` 两种正则都匹配不到） | `narrate.py` 新增 `_ERROR_CODE_RE`；前端 `humanizeError` 同步 |
| `console.log('规划选项:', readPlanOptions())` | 调试残留 | 已删（改绑 `updateStatusModel`） |
| `↻ 重新规划` 在 `awaiting_confirm` 必然 409「非法状态转换」 | 功能 bug | 前端先 `/reject` 退回 planning 再 `/plan`（卡片以 `data-can-reject` 标记该状态），**不动状态机契约** |
| 只读动作被写成「已写入 tests：…」 | 人话文案错配 | `narrate._result_sentence()` 按动作分流：只有 `_WRITE_ACTIONS`（file_write / git_commit / rollback）才说「已写入」；只读类优先用结果自带的人话描述，缺失回退「中文动作名：目标」 |

> **注意**：`StepResult.summary` 是**执行时**算好并落库的，所以历史任务的卡片文案不会追改；上述修复只对**新跑的任务**生效。

**E. 建议做法（当初的建议，已被上面的实际做法取代）**

先用 **rapid-prototype-craft** 出一个「对话输出形态」原型（折叠过程块 + 文本化步骤 + 结论区），**给用户确认视觉**后再接真实后端 —— 避免直接改产品前端反复返工。

## 对话优先 + 产物入口（本轮已完成，用户实测后提出）

> **用户原话**：「这些没有我想要的那种折叠，而且无法查看产物……我认为后端并没有让大模型掌控，依然存在着工作流，连一些基础的对话都做不到，比如说『你好』，只会按工作流弹出对话卡。」

### 定位到的根因（都有代码证据）

| # | 现象 | 根因 |
|---|---|---|
| 1 | 「你好」也弹任务卡 | `handleSend` **无条件** `createTask` → `planTask`；后端**没有对话端点**，只有任务端点 |
| 2 | 「依然是工作流」 | `/plan` 永远走 `Decomposer` 产出固定 DAG，前端把它当**主界面**渲染；agentic 只作用于**执行**阶段 |
| 3 | 没有 DeepSeek 式折叠 | 计划块在 `#planSlot`（对话流之外）且 `<details open>` 默认展开；过程块只收工具卡与自检 |
| 4 | 看不到「结论」 | agentic 以 `KIND_FINAL` 结束时，`decision.thought` **被丢弃**（`LoopOutcome` 无 `final_answer`） |
| 5 | 「查看产物」报 `路径不是文件: .` | `renderPlanBlock` / `stepInputPath` 只要 `step.inputs.path` 存在就渲染按钮；`execution_results` **没有 `artifacts` 字段** |

### 本轮改了什么

**① 对话优先（P0）**
- 新增 [`POST /chat`](agent_builder/api/chat.py)：一次 LLM 调用做意图分流，返回 `{kind: "chat"|"task", reply, reason}`。
  - `kind=chat` → 直接回话，**不建任务、不出计划**；
  - `kind=task` → 前端才进入任务链路。
  - **拿不准偏向 chat**；**无密钥不降级成工作流**，而是如实回一句「还没有配置 API 密钥……」，模型乱返时也不擅自转任务。
- 前端 `handleSend` 改为「先 `/chat` 再决定」；上下文由 `recentChatHistory()` 从 `transcript` 里取最近 8 条纯文本消息。

**② 产物入口（P3）**
- 新增 [`agent_builder/api/artifacts.py`](agent_builder/api/artifacts.py) 的 `artifacts_of()` —— **唯一**判定「这一步产出了哪些文件」的地方。只读动作（列出文件/读取文件）的 `inputs.path` **不算产物**。
  - 单独成模块的原因：`deciders` 反向 import 了 `orchestrator`，`orchestrator` 再 import `agent_loop` 会**成环**。
- `StepResult` 增 `artifacts`；`to_execution_results()` 与 `orchestrator` 的固定工作流一并填充。
- 前端 `stepArtifactPath(item)` 取代旧的 `stepInputPath(stepId)`：**只有真产出才渲染「查看产物」**；打不开时提示「如果这是一个文件夹，请在右侧『产物』面板里展开查看」。

**③ agentic 下不再渲染计划块**
- `EXECUTION_MODE = 'agentic'`（与后端 `PlanRequest.mode` 默认值对齐，并**显式下发** `mode`，避免两边默认值漂移）。
- `renderPlanBlock()` 在非 `workflow` 模式直接短路清空 —— 预分解出的 DAG 并不是真正跑的东西，把它当主界面就会一直「像工作流」。
- 配套文案按模式分流：agentic 下只说「收到，开始执行。」，不再报「共 N 个步骤（见计划区块）」。

### 验收
- 发「你好」→ 只回一句中文提示，**不建任务**、`#planSlot` 为空（已在浏览器实测）。
- 「查看产物」只在 `artifacts` 非空时出现（`tests/test_frontend_workspace_ui.py::TestArtifactEntry` + `tests/test_agent_loop.py::TestArtifacts`）。

### 尚未做
- 无（原先列的三项见下一节，均已完成）。

## 结论区 + 完整折叠 + 规划去工作流化（✅ 已完成）

> 本节三项对应此前「用户已确认的后续方向」。改动集中在前端 `app.js` / `styles.css` /
> `index.html` 与后端 `narrate.py` / `agent_loop.py` / `routes.py` / `schemas.py`。

### ① 结论区：跑完给一句人话结论

- **后端**：`LoopOutcome` 增 `final_answer` —— 收敛那轮（`kind=final`）的 `thought` 不再被丢弃；
  落进 `LoopState.final_answer`；`narrate.conclude()` 是结论的唯一来源（优先模型结论，
  没有则按执行结果如实汇总；**都没有就返回空串，不编造**）；`TaskResponse.conclusion`
  在 `_to_task_response` 里统一算好，所以 `/run` 响应与 `GET /tasks/{id}` 回看口径一致。
- **前端**：`renderConclusion(task)` 渲染 `.conclusion-block`（**留在正文**，不折进过程块）；
  一个任务只保留一条（替换而非追加），由后端状态派生 → **不入 transcript**，
  刷新 / 切任务由 `applyTaskToView` 重建。有结论时 `renderRunSummary` 不再重复播报进度。

### ② DeepSeek 式完整折叠

- 过程块摘要行改成 **`已深度思考（用时 N 秒）· 已完成 N 步[ · 失败 N 次]`**。
- **计划块折进过程块**：`#planSlot` 独立槽位已**删除**；`renderPlanBlock` 改把
  `<details class="plan-block">` 挂进 `.process-body`（就地替换，避免刷新叠出多份）。
  仍只在 `workflow` 模式渲染（agentic 没有固定 DAG）。
- **思考折进过程块**：每轮决策理由（`StepResult.thought`）以前哪都不显示，
  现在随工具卡渲染成 `.tool-card-thought`；`appendToolCard` / `renderEntry` 同步记录与恢复。
- 自动展开的三类例外不变（有失败 / 有待放行 / 有需要你决定）。
- **副作用**：菜单「改计划 / 放弃」原先借用 `#planSlot` 当卡片取任务 ID，
  改用新增的轻量作用域壳 `taskScopeEl(currentTaskId)`。

### ③ 规划阶段去工作流化（agentic 不再先出 DAG）

- `POST /tasks/{id}/plan`：`mode=agentic` **且存在可用密钥**时**不再调用 Decomposer**，
  只置一个空 `Plan` 占位 + 转 `awaiting_confirm`；响应回显 `mode`（`DecomposeResponse.mode`），
  `steps` 为空是**正常结果**。**无密钥时仍走原分解路径** —— 因为 agentic 会回退固定工作流，
  那时预分解是必须的（否则没步骤可跑）。
- `POST /tasks/{id}/run`：现在也接受 `planning` 直跑（内部补一次 `plan_ready`，
  **不改状态机契约**），即「带着原始需求直接进循环」。
- 前端 `applyPlanResult`：按响应里的 `mode` 分流；agentic 的「0 步」不再被当成
  「暂无可用计划」。例外：后端**明确给了 `pending_questions`**（真走不下去）时仍出卡片，
  避免"看着跑起来了其实什么都没发生"。

### 浏览器实测发现并修复的 7 个 bug

> 真实密钥 + 端到端实测（agentic 循环）暴露出来的问题，全部已修 + 补回归测试。

| # | 现象（实测证据） | 根因 | 修复 |
|---|---|---|---|
| 1 | 只读动作也长出「查看产物」，点开必报「路径不是文件: tests」（后端 `artifacts=["tests"]`） | `CodeWorker._extract_files_changed()` 对**任何** action 都回填 `inputs.path` → `artifacts_of()` 信以为真。**单测只覆盖纯工具路径（result 是 str），漏了角色派发路径（result 是 `asdict(CodeResult)`）** | `code_worker.FILE_PRODUCING_ACTIONS`（只有 `file_write` 才算变更）；补 2 条回归测试（只读不算 / 写动作照旧算） |
| 2 | 模型说 `stopped_reason=final`（自己收尾了），任务却永远停在 `executing`（顶栏一直「执行中」） | `_execute_plan_agentic` 只在**所有**结果都 done 时才转 verifying | `all_done or stopped_reason == STOP_FINAL` → VERIFYING；补「非收敛终止仍留在执行中」反向测试 |
| 3 | agentic（默认模式）下工具卡与「产物」行标题退回英文 `file_list` / `file_read` | `applyPlanResult` 在 agentic 下把 `planState.steps` 置空，`applyRunResult` 从不拿 `task.steps` 回填 → `stepTitle()/actionTitle()` 全部回退英文 | 新增 `mergePlanStateFromTask(task)`，执行完成与回看两条路径共用 |
| 4 | 过程块摘要只有「已深度思考」，没有「（用时 N 秒）」 | 最后一次 `refreshProcessBlock()` 由 `mountProcessNode` 触发、不带 task → 覆盖掉带用时的版本 | 记住 `lastProcessTask`（**必须在提前 return 之前记**：过程块由第一批工具卡才建）；「待审批」也不再混入「已完成」步数 |
| 5 | 实测 `created_at` 与 `updated_at` 只差 10 微秒（= 同一次创建）；任务列表排序恒等于创建顺序 | `TaskState.updated_at` 只在创建时由 `default_factory` 赋值，**全仓库无写入点** | `Conductor._transition` 每次转换刷新 `updated_at`（失败转换不污染） |
| 6 | **实测新发现**：放行后工具卡从 3 张变 6 张、摘要步数虚增（同一 `loop-003` 同时有「待审批」与「已完成」两张卡） | `/resume` 会回传**全部** `execution_results`，而前端 `renderExecutionResults` 只追加不去重（DOM 与 transcript 都会积累） | `data-step-id` + `dropPreviousStepNodes(stepId, selector)`（必须限定 `.tool-card` / `.artifact-row`，两者共用同一个步骤号）；`recordEntry` 同 kind 同 id 去重；重建路径对无 `stepId` 的历史记录用标题兜底 |
| 7 | **实测新发现**：放行成功后顶栏仍停在「等待放行」，与真实状态（验证中）不符 | chip 文案由 `currentPendingApproval` 派生，而 `renderActionSlot` 清掉它之后只刷新了状态栏、没刷新 chip | `renderActionSlot` 末尾按 `currentStatus` 重算一次 chip |

**未修（同类，但本前端不可达）**：`renderSelfCheck` 的过程说明在 `/resume` 后会重复追加（自检只在固定工作流路径产出，而本前端固定 `EXECUTION_MODE='agentic'`）。

### 第二轮实测：「帮我做一个调研agent」暴露的问题

实测数据：`status=executing`、`stopped_reason=stagnant`、4 步里 3 步失败、无 `final_answer`、无产物。
（`file_list` 缺 path → `E_PERMISSION`；`memory_read ~/.li-tao-agent` → 越界被拦；`web_search` → 网络超时。）

| # | 现象（实测证据） | 根因 | 修复（已实测） |
|---|---|---|---|
| 8（原 A） | 点开任务列表里的任务，**执行过程完全不可见**：`CARDS=0`、只剩一句「执行了 4 步：成功 1 步、失败 3 步」（后端明明有 4 条执行结果） | `applyTaskToView` 从不渲染 `execution_results`，工具卡只来自本地 transcript → **不是本机跑过的任务点开等于什么都没有** | `applyTaskToView` 补 `renderExecutionResults(task)`；新增 `stepIdFromTitle()` 让「本地记录」与「后端结果」用**同一个键**（否则两份来源各渲染一套卡片）→ 实测 `CARDS=4`，含决策理由与人话失败原因 |
| 9（原 B） | 非收敛停下后：chip 显示「执行中」、无任何卡片、无出口，且「中断」按钮可点（点下去把一个已停下的任务改成「已暂停」） | `loop_state.stopped_reason` 前端完全不渲染 | 新增 `LOOP_STOP_LABELS` + `loopStopReason()` + 「⚠ 执行已停下」卡（原因 / 失败步数 / 出口=放弃）；chip 改「已停下」并禁用「中断」。**严格门控 `status === 'executing'`** —— 否则用户主动「放弃」后顶栏会被误写成「已停下」（实测踩到） |
| 10 | 「已深度思考（**用时 0 秒**）」 | 非收敛停下**没有状态转换** → `updated_at` 停在「进入执行」时刻 | 新增 `Conductor.touch()`（只刷新 `updated_at`），在 `_execute_plan` 两条分支执行结束后调用 |

### 第三轮：工具层修复（C 错误分类 + D 沙箱基准，均为实测动机）

**C：缺参数不再报「权限不足」**

- `gatekeeper` 三个缺参分支（文件 `path`/`repo_path`、web `url`、`test_run` `target`）由
  `permission_error` 改 `validation_error` —— 与 `registry` 的「缺少必填参数」同口径；
  **真正的越界仍是 `E_PERMISSION`**；拒绝依旧写审计（`allowed=False`），安全边界不变。
- 动机（实测）：模型看到 `E_PERMISSION: 文件工具缺少 path/repo_path 参数` 会当成"没权限"
  → 换个动作，而不是补上参数重试（那轮就是 loop-001 失败 → loop-004 换 web_search）。
- 同步改写 8 处既有断言（2 处缺参 + 6 处空值），「被拦下 + 留档」的保护意图未削弱。

**D：相对路径按「当前工作区」解析，不再按进程 cwd**

- 新增 `tools/gatekeeper.resolve_in_workspace(path, base=None)` + `current_workspace_dir`
  contextvar：门卫用自己的 `workspace_dir` 校验，`registry.execute` 把同一基准注入上下文，
  工具实现（拿不到门卫实例）据此解析 —— 未注入时退回 cwd，保持「直接调实现」的既有行为。
- 落地：门卫三处（沙箱 path / optional path / test_run target）+ 5 个文件类实现
  （file_read / file_write / file_list / file_delete / data_query）。
- 顺带修 `git_ops.validate_repo_path`：此前拿**硬编码的 Linux `WORKSPACE_DIR`** 比对，
  在本机永远判越界（`git_commit` / `git_log` / `rollback` 直接不可用）。
- 新增 2 条回归测试：cwd ≠ 工作区时 `path="a.txt"` 仍读工作区那一份；**反证** cwd 下的同名文件读不到。
- 为什么以前看不出来：默认工作区恰好等于 cwd（`file_write` 的返回值仍是调用方给的写法，摘要不变）。

### 第四轮：存储边界 + 结论区（**「提示词泄漏」是误判**）

**先纠正一个误判**：此前把「模型读 `~/.li-tao-agent`」归因为**提示词泄漏宿主路径** —— 不是。
`catalog_prompt` 只给角色名 + 动作名，提示词里没有任何路径。真因是**存储后端默认位置写死在用户主目录**：
`memory_store.DEFAULT_MEMORY_DIR = Path.home()/".li-tao-agent"`（`audit_store` 同），
而 `memory_read/write/forget`、`audit_log`、`metric_collect` **没有 path 参数、也不在门卫的沙箱校验名单**里
—— 等于绕过边界读写工作区外，本机还直接 `WinError 5 拒绝访问`（且读之前会在用户主目录 `mkdir`）。

| # | 问题 | 修复 |
|---|---|---|
| 11 | 默认记忆 / 审计位置在工作区外（工具无 path 参数 → 沙箱管不到） | 改为 `<当前工作区>/.agent-memory/memory.json` 与 `<当前工作区>/.agent-audit/audit.json`：复用 `current_workspace_dir` 上下文（无上下文退回 cwd），隐藏目录不会被产物树展示；`.gitignore`、`docs/tools.md` 同步 |
| 12 | 结论区端给用户的是模型的**内心独白**（"可以收尾"） | `Decision.answer` 独立字段（与 `thought` 分开）+ 提示词硬规则「kind=final **必须给 answer**（写给用户看的结论），不要写"可以收尾"这类内部独白」+ 解析接受 `final_answer` 别名；循环取 `answer`，缺失才回退 `thought`（宁可给理由，不编造结论）；前端 `.conclusion-body` 加 `pre-wrap`（结论可含换行/短列表） |

### 第五轮：工具参数结构进提示词（最高频失败源）

**问题（实测）**：需求「把项目名写进长期记忆」连着两步失败，**不是权限问题，是模型只能猜参数**：

```
loop-001  memory_write  E_VALIDATION: 缺少必填参数: ['key']（允许的参数: ['content','key','scope','sensitive']）
loop-002  memory_write  E_VALIDATION: scope 非法 'long_term'，可选: ['long','short']
```

根因：`catalog_prompt` 只给「角色名 + 动作名」，**不给动作的参数结构** → 每轮 `inputs` 全靠猜
（之前 `file_list` 缺 `path` 同源）。代价是白跑一轮 + 计入空转（连着两次就停下）。

**修复**：

- 新增 `role_catalog.action_signature(action)`：从工具 spec 取「必填 + 带枚举」的参数压成
  `memory_write(key*, content*, scope=short|long)`；可选且无枚举的（如 `limit`）不铺开。
- `RoleSpec.to_prompt_line()` 用它渲染动作；`catalog_prompt` 追加一行写法说明
  「参数写法：动作(必填参数\*, 带枚举的参数=值1|值2)；请**按签名给 inputs**，不要猜参数名」
  —— 目录里一个签名都没有时不加（不出现无意义注释）。
- 角色内行为（`summarize` / `propose`，非注册工具）没有 schema → 原样返回动作名，不编造。
- 签名进的是提示词**最前面的稳定段**（吃 DeepSeek 前缀缓存）：一次成本换掉"每轮猜参数"的失败。
  `docs/agentic-loop-design.md` 的提示词样例已同步。

至此前五轮列出的问题全部处理完；后续新增能力按需再开。

### 第六轮：产物面板实测（折叠持久化 + 放行空转 + 大文件预览）

**产物面板实测**（浏览器端到端）确认：目录折叠/展开、文件点击打开、编辑保存、未保存二次确认、
焦点归还、键盘可达（Enter/Esc）、产物行入口、隐藏目录不进树、↻ 刷新不重复节点 —— 全部正常。
顺带挖出两个 bug 并修复：

| # | 问题 | 修复 |
|---|---|---|
| 14 | ↻ 刷新重建文件树后，折叠状态被重置（折叠的目录又展开了） | `app.js` 新增模块级 `collapsedDirs`（Set，按工作区相对路径记）；`renderFileTree` 建节点时读回、折叠/展开时增删 |
| 15 | 放行后「不公地」过早判空转：明明只失败一次就 `stagnant` 停下 | ① `run_agent_loop` 里 pending_approval 那轮**不记预算**（判断挪到 `budget.record` 之前）—— 审批门拦下的是"还没执行"，既不算一步也不算空转；② `_execute_plan_agentic` 续跑时**不继承 `stagnant_rounds`**（用户放行 = 一次新的干预，给一轮新机会；步数/token 仍累计，兜住无限往复） |

**大文件预览**：此前 `> MAX_PREVIEW_BYTES（20MB）` 直接 413，用户永远打不开大文件。
现删掉这个硬上限，**任意大小都只读截断预览**（始终只读前 2MB，GB 级也秒开，不会把整份塞进响应体）；
`formatSize` 补 GB 档，前端 `20.5MB · 过大，仅只读预览` 提示可正常触发。

**验证**：`ruff` 干净、`pytest` **1975 passed / 11 skipped**、`node --check` 通过。
新增 5 条测试：`test_agent_loop.py::TestLoopApprovalGate.test_挂起轮不计入预算与空转`、
`test_agentic_mode.py::TestAgenticApprovalGate.test_放行后不继承空转计数`、
`test_api_projects.py::TestLargeFilePreview`、
`test_frontend_workspace_ui.py::TestFileTreeKeepsCollapsedState` / `::TestLargeFilePreviewWiring`。
浏览器实测：折叠 `agent_builder` → ↻ → 仍 `▸`；造 21.5MB 的 `_preview_check.txt` → 打开为只读截断（内容回传 2MB）。

### 验证与遗留

- `ruff` 干净、`pytest` **1955 passed / 11 skipped**、`node --check` 通过；
  静态接线测试同步改写（`tests/test_frontend_workspace_ui.py` 新增 `TestConclusionBlock`、
  `TestProcessBlock.test_决策理由收进过程块`，改写计划块 / 摘要行 / 0 步分支三处断言，意图未削弱）；
  端到端新增 `tests/test_agentic_mode.py::TestAgenticNoDag` / `::TestConclusion`、
  `tests/test_agent_loop.py` 的 `final_answer` 断言、`tests/test_narrate.py::TestConclude`。
- **已做浏览器实测**（真实密钥，agentic 循环）：结论区 / 过程块「已深度思考（用时 N 秒）」/
  中文动作名标题 / 只读动作不出「查看产物」/ 顶部 chip 都按预期工作；上表 7 个 bug 就是这么挖出来的。
- **补测已完成（真实密钥 + 浏览器端到端，`?v=20261030`）** —— 一次性覆盖此前只靠单测 / 静态断言的 3 项。
  需求：「先读取 `_no_such_dir_abc/note.md`（不存在），再把读取失败的原因写入 `_live_check.md`」
  → 第 1 步读失败（`失败 1 次`）、第 2 步写入「到点暂停 · 待放行」→ 点「放行并继续」后：
  ① **#2 收敛但有失败 → verifying**：模型给出结论卡「已完成：读取 `_no_such_dir_abc/note.md` 失败，
     并把失败原因写入 `_live_check.md`」，状态栏转「任务：验证中」——**走的不是 all_done 分支**（有 1 个失败步骤）；
  ② **#6 放行后卡片不重复**：`.tool-card` 数前后都是 2，`loop-002` 仍是**同一张卡**（仅从「待审批」变
     「已写入 `_live_check.md`」），只多出 1 行产物（`data-step-id` 节点 3 = loop-001×1 + loop-002×2）；
  ③ **#7 chip / 状态进验证阶段**：顶部 chip 变「验证中」、状态栏「任务：验证中」。
  （验证用的 `_live_check.md` 已删除。）

### 本机环境坑（本轮踩到，务必知道）

- **后台服务与命令共用同一个终端**：用 `blocking:false` 起的后端 / 前端，会被随后任何一条
  阻塞式命令（pytest / node / Invoke-WebRequest…）**连带杀掉** → 现象是浏览器 `ERR_CONNECTION_REFUSED`。
  对策：**先跑完所有校验命令，最后再起服务**；服务起来后只用浏览器（`browser_navigate` /
  `browser_evaluate`）验证，别再敲终端。`Start-Process` 起的进程同样会被回收。
- **后端重启 = 内存密钥清空**：`--reload` 每次重载都会重启应用进程，密钥（仅驻内存）随之丢失，
  浏览器里必须**重新保存一次密钥**。所以「改后端 → 重载 → 接着实测」这条链路每轮都要重填密钥，
  尽量把后端改动一次改完再测。
- 后端任务同样只在内存：重启后旧任务全部 404，前端会提示「之前的任务已不存在（后端可能已重启）」。
- `browser_evaluate` 的脚本里**不要写 `\s` 这类转义**：模板字符串会把 `\s` 变成 `s`，
  于是 `/\\s+/g` 实际变成 `/s+/g`，把文本里所有字母 s 都替换成空格（本轮被这个坑过一次，
  差点误报成前端渲染 bug）。用 `.split(String.fromCharCode(10)).join(' / ')` 处理换行更稳。

## P3：执行形态 A/B（workflow vs agentic）—— ✅ 已完成（24 次真实运行）

**已定口径**（用户拍板）：**4 任务 × 2 档（`workflow` / `agentic`）× 3 次**；只用**运行时指标**；
报告优先级 **P0 质量 → P1 成本**。

- **任务集**（`mode_ab.TASKS`）：**R1「调研论文 agent」为主任务**，另配 R2 只读 / R3 写文件 / R4 搜索
  三个对照；所有需求都把产物写进工作区独占目录 `_mode_ab_out/`（跑完整目录删除）。
- **脚本**：`agent_builder/evaluation/mode_ab.py`（只读、仅 stdlib，照搬 `role_brief_ab_exec.py` 的 HTTP 骨架）。
  两档**执行入口不同**：`workflow` 走 `POST /approve`（计划里已有 `high_risk_actions`，一次性授权）；
  `agentic` 走 `POST /run`，高风险「到点暂停」→ 按 `pending_approval.tools` 逐次 `POST /resume`
  （上限 `--max-resume`，默认 6）。
- **指标**（`extract_metrics` 纯函数）：完成率（到达 `verifying`）、失败步骤数、返工数（`retries` 求和）、
  token、agentic 循环轮数、终止原因；`agentic` 档若 `loop_state` 为空 → 标 `agentic_fell_back`
  （无密钥被回退成固定工作流，该 run 不计入 agentic）。
- **产物**：`docs/reports/mode-ab-runs.json`（原始）+ **`docs/reports/mode-ab.md`（结论报告）**；
  新增 `tests/test_evaluation_mode_ab.py`（23 例，纯函数 / 纯文件，不联网）。
- **结果（v2 任务集，`deepseek-chat` 温度 0.3）**：完成率 **agentic 100%（12/12） vs workflow 83%（10/12）**；
  失败步骤 1 vs 2；返工 0 vs 2（agentic 单步不重派）；token 中位数 **4866 vs 754（~6.5×）**；
  agentic 循环中位数 3（区间 1–12）。**按质量优先判定 → 倾向 `agentic`（代价是成本贵 6.5×、轮次长）**。
- **真正的形态差异**：失败集中在 R2，两档各错，但结局不同 —— workflow 的分解器**臆造 `sandbox_run` 步骤**
  （缺 `command` → `E_VALIDATION`），**计划是冻结的、错一步就停在 `executing`**；agentic 误用 `data_query`
  读 `.md`，**下一轮自我纠正**（改回 `file_read`）仍收敛。即 agentic 不是不犯错，而是**错了能改**。
- **任务集修订（v1 → v2，重要）**：v1 的 R2/R4 要求「先列目录再处理」，逼模型在看不到目录内容时猜文件名
  （`file_read: 路径不是文件: _mode_ab_out/notes/`、`notes/<file1>.md`、`file_list: .../notes/*`），
  **两档都踩**、噪声盖过形态差异 → **v1 的「workflow 优」不可采信**。v2 把文件名全部写死：
  完成率 50% / 25% → 83% / 100%，失败步骤 7 / 29 → 2 / 1。v1 原始数据留 `mode-ab-runs.before-taskfix.json`。

## 下一步计划（进行中）：后端 agentic 化 —— 由模型掌控选 agent

> **完整方案见 [docs/agentic-loop-design.md](agentic-loop-design.md)**（护栏、能力目录、决策契约、提示词模板、JSON 容错、循环骨架、副结构门控、提议闭环、分阶段与风险）。
> **P0 / P1 / P2 已完成** —— `mode` 默认 `agentic`（模型逐轮选 agent，无密钥自动回退固定工作流）；「副结构」开关 + 提议闭环可用。**P3 已完成**（24 次真实运行：完成率 workflow 50% vs agentic 25%、成本约 1/10 → 倾向 `workflow`）。
> **补充（本轮）**：规划阶段已**去工作流化** —— agentic 下有密钥时 `/plan` 不再跑 Decomposer 出 DAG（详见「结论区 + 完整折叠 + 规划去工作流化」一节）。

- **目标**：执行阶段不再走「静态 `ACTION_ROLE_MAP` + 固定五阶段」，改由模型每轮决策「下一个由谁做、做什么」，拿到结果后继续决策或收敛。
- **已定**：① `mode` **默认 `agentic`**；② 副结构用**独立开关**；③ 新角色**半自动**落盘；④ 主/副名单**按文档原文**；⑤ 预算 **12 步 / 60k tokens / 连续 2 轮无进展**；⑥ 提示词**详细版**；⑦ 解析失败**重试 1 次**；⑧ 历史窗口**最近 6 轮**；⑨ `mode`/`sub_arch` 并入 `PlanRequest`；⑩ 循环上下文用 `LoopState` 挂 `TaskEntry`；⑪ 前端**复用现有工具卡**；⑫ 批准**只写工作区 `proposals/<id>/`**、不改仓库既有文件；⑬ 提议**只支持新角色**（新工具如实拒绝）；⑭ 一致性核对前端文案改叫**「执行自检」**。
- **护栏不变**：`ToolGatekeeper`（权限+沙箱+审计）、高风险审批门（到点暂停）、roles 层跨层约束、新角色三道门。
- **新增文件**：`agent_builder/api/{role_catalog,deciders,agent_loop,proposal_scaffold}.py`；测试 `tests/{test_role_catalog,test_agent_loop,test_deciders,test_agentic_mode,test_proposal_closure}.py`。
- **关键实现点**：① `orchestrator.build_step_executor()` 是**固定工作流与 agentic 共用的唯一执行链路**（改执行语义必须两边一起看）；② agentic 的「到点暂停」在**进入工具链之前**判定（门卫对未授权是 `E_PERMISSION` 失败，不是挂起）；③ `/resume` 用 `PrefixedDecider` **原样重放**挂起那一步并撤掉它的旧记录行；④ **提议的待落盘内容在提议时刻就算好**（`entry.pending_scaffold`），批准写的就是用户预览过的那份；⑤ 生成物落在 `proposals/<id>/`，**新角色在人工并入 `tools/permissions.py` 授权前天然不可派发**。
- **踩到的坑**：① `ACTION_ROLE_MAP` 里混着**非工具动作**（`summarize`/`propose`/`impact_analyze` 等是**角色内行为**）→ 只对**已注册工具**查权限矩阵；② `metric_collect` 关闭副结构时**仍可达**（`operator` 白名单含它）；③ 默认 `agentic` 让 3 个既有用例语义变了（已显式传 `mode=workflow`）；④ 模型给 `risk="critical"` 会让 `ChangeProposal` 强校验抛 500 → 在**解析层**把风险档位收敛到合法集合；⑤ 前端新增端点要用装饰器上的 `dependencies=[Depends(require_local_client)]`（不是自定义 `_require_local_client`）。
- **P3 已定**：任务集 = 4 个真实需求（含 R1「调研论文 agent」）、2 档 × 3 次；评价口径 = 只用运行时指标（P0 质量 → P1 成本）。详见「P3：执行形态 A/B」一节。

## 其他待办（次要，按需）

1. ~~回看不完整~~ **已修**：`TaskResponse` 已带 `steps` 明细（`_to_task_response` 逐条 `model_dump`），前端 `applyTaskToView` 用它填充 `planState`。
2. ~~「改计划」可达性~~ **已修**：状态机已定义 `interrupted + plan_rejected → planning`，两张暂停卡都带「改计划」入口。
3. ~~windows 环境依赖~~ **已修**：`test_run` 原来用裸 `pytest`，而本机依赖是
   `pip install --target .deps` 装的 —— `pytest` 与 `python` **都不在 PATH**（实测两遍
   `shutil.which` 都是 `None`），裸命令直接 `WinError 2`。改用 `sys.executable -m pytest`
   （用**正在跑本应用的**那个解释器，pytest 是它的依赖）：不用改 PATH，也不会挑到另一个
   Python 环境。`docs/tools.md` 已同步；新增回归测试
   `tests/test_tools_test_run.py::TestTestRunFunctional::test_用当前解释器跑pytest`（钉住 argv）。
   实测 `run_test('tests/test_narrate.py')` → `32 passed`。
   - **同类未修（按需再开）**：`sandbox_run` 走 `shell=True, env=os.environ`，子进程里同样没有
     `python`／`pip` —— 若生成物要执行 `python xxx.py`，本机仍会「python is not recognized」。
     要修就在 `env["PATH"]` 前补 `Path(sys.executable).parent`。

## 硬性约束（改代码前务必知道）

- **角色层（`agent_builder/roles/*.py`）禁止 import `agent_builder.api` / `agent_builder.tools` / `agent_builder.llm`**：`tests/test_evaluation_agents.py` 会判跨层依赖并扣分；`roles/` 下**新增任何 .py 都会被当成新角色**参与评分（需要 `docs/execution-protocols.md` 章节 + `permissions.py` 授权 + `tests/test_roles_<name>.py` 含 Functional/Edge 用例类且 ≥10 例）。派发逻辑因此放在 `api/orchestrator.py`。
- **前端有静态接线测试**：`tests/test_frontend_workspace_ui.py`、`tests/test_frontend_session.py`、`tests/test_api_plan_options.py::TestFrontendWiring`、`tests/test_api_key_ttl_rotation.py::TestFrontendWiring` 会正则/字符串断言 `app.js` / `index.html` / `styles.css` 的关键内容，并检查 `app.js` **顶层无重复 `const/let/var` 声明**。**改前端前先看这几个测试**；IA 变了要**同步改写断言但不得削弱保护意图**。
- **颜色只写在 CSS**：只引 `:root` 语义 token（浅色默认 + `prefers-color-scheme` 覆盖），**JS 不得内联下发颜色**。
- **交互一律原生 `<button>`**（不要 div+onclick）。注意 **HTML 不允许 button 嵌套 button**（任务列表项用 `.task-row` 平级承载主按钮与删除按钮）。
- **会话持久化只用 `sessionStorage`**（键 `agent-builder:session`），**严禁 `localStorage`**（有测试断言）。
- **工作区边界**：目录须绝对路径、存在、是目录、非驱动器根、非系统保护目录；文件访问只接受工作区**相对路径**（`_safe_workspace_path` 防穿越）；`/projects/*` 与 `/workspace/*` 挂 `require_local_client`（需 `X-Agent-Builder-Client: web` 头）。
- **易混淆命名**：`roles/gatekeeper.py`（看门人角色）vs `tools/gatekeeper.py`（工具门卫，不参与角色评分）。
- **`make_llm_executor`** 是死代码（仅测试/文档引用），**保留不动**。

## 本机浏览器验证的经验（重要）

- `file://` 被浏览器策略禁用 → 要在浏览器里验证前端，必须起 HTTP 静态服务
- `browser_navigate` 对**同一 URL 不真正重载**（会复用缓存/不重导航）→ 加 `?cb=<随机数>` 强制真导航；换端口也算新源（但会被 CORS 拦）
- `browser_press_key` 在本机**不生效**（实测方向键/滑块无效）→ 键盘行为只能靠结构证据判断
- **无视口模拟工具** → 验证暗色/窄屏时可用 CSSOM 临时把媒体规则改成 `all`
- `browser_take_screenshot` 常不可用（offscreen/throttled）→ 改用计算样式 + AX 快照取证
- `browser_evaluate` 的脚本**不要用 IIFE 包裹**，也不要依赖 `Array.from(...).map(...)` 的返回（本机实测常返回 `undefined`）→ 用字符串拼接 + 直接属性访问更稳
- 排查「按钮点了没反应」优先用 `document.elementFromPoint(x, y)` 看**最上层元素是不是目标按钮**（本机踩过浮层遮挡的坑）
