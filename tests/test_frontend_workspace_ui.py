"""新信息架构（IA）前端接线静态检查。

覆盖本轮三条用户痛点的 UI 接线，防止回归：
1. **看得到准确的计划**：计划区块确实把 /plan 的 steps 结构化渲染
   （步骤 id / action / 输入摘要 / 依赖 / 高风险标记 / 执行状态 / 查看产物），
   而不是只渲染步骤数量；
2. **多任务并存可回看**：左栏任务列表调用 ``GET /task-summaries``，展示标题 / 状态 /
   是否待放行，当前任务高亮；
3. **默认直接跑 + 到点暂停**：/plan 后自动 /run；到点暂停由后端 ``pending_approval``
   触发提醒卡，放行动作调用 ``resumeTask`` 并携带 ``approved_tools``；
4. **看得到产物**：工具卡 / 计划步骤都有「查看产物」入口，代码块经 escapeHtml 走
   ``<pre><code>``；同时守住「颜色只写 CSS token」「交互一律 <button>」「不引不存在的 id」。
5. **/plan 返回 0 步不再死路**：「暂无可用计划」卡片原样展示后端 pending_questions，
   给出「重新规划 / 放弃任务 / 改计划」入口，按钮走卡片任务作用域；静态资源带 ?v= 防缓存。
6. **任务可删除**：左栏每项带原生删除按钮 → ``api.deleteTask`` → ``DELETE /tasks/{id}``；
   删除按钮与主按钮是 ``.task-row`` 下的平级兄弟（避免 button 嵌套 button）。
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (_PROJECT_ROOT / relative).read_text(encoding="utf-8")


class TestPlanBlockRendersSteps:
    """计划区块渲染 steps 明细（而非只渲染数量）。"""

    def test_计划区块把steps结构化渲染(self) -> None:
        source = _read("frontend/js/app.js")
        assert "function renderPlanBlock(plan)" in source
        # 真正遍历 steps / order 渲染每一行。
        assert "const steps = plan.steps || {};" in source
        assert "const order = hasOrder ? plan.order : Object.keys(steps);" in source
        assert "const highRisk = new Set(plan.high_risk_actions || []);" in source

    def test_每行渲染关键字段(self) -> None:
        source = _read("frontend/js/app.js")
        for marker in (
            "data-step-id=",            # 步骤 id
            "escapeHtml(actionLabel)",  # 展示中文动作名（回退英文 action）
            "step.depends_on",          # 依赖
            "summarizeInputs(step)",    # 输入摘要（人话，非 key=value）
            "plan-step-flag",           # 高风险标记
            "plan-step",                # 行样式类
        ):
            assert marker in source, marker
        # 高风险标记文案 + 状态来自 execution_results。
        assert "高风险" in source
        assert "const results = {};" in source
        assert "plan.execution_results" in source

    def test_计划区块由planState驱动且收进过程块(self) -> None:
        """计划折进「过程」块（DeepSeek 式折叠），但仍然可折叠、仍由 planState 驱动。"""
        source = _read("frontend/js/app.js")
        assert "let planState = null;" in source
        assert "renderPlanBlock(planState)" in source
        body = _function_body(source, "renderPlanBlock")
        assert "process-body" in body  # 渲染进过程块的 body
        assert "mountProcessNode(block)" in body  # 由过程块统一收纳
        assert "document.createElement('details')" in body  # 计划自身仍可折叠
        html = _read("frontend/index.html")
        assert 'id="planSlot"' not in html  # 独立槽位已撤销：不再有"正文之外的计划区"

    def test_不在appjs内联下发颜色(self) -> None:
        source = _read("frontend/js/app.js")
        assert "style.color" not in source
        assert "style.background" not in source
        assert "style.backgroundColor" not in source


class TestReopenShowsPlanSteps:
    """回看完整性：步骤明细取自后端 `steps`（此前 plan 只有 order，回看只剩 step_id 占位）。"""

    def test_回看时用后端steps填充planState(self) -> None:
        source = _read("frontend/js/app.js")
        # 后端 GET /tasks/{id} 的 steps 明细被真正接进 planState。
        assert "steps: task.steps || {}," in source
        # 旧的空占位（只剩 order → 看不到 action / 输入）必须已消除。
        assert "steps: {}," not in source
        # order 缺失时仍由 renderPlanBlock 回退到 Object.keys(steps)，兜底不变。
        assert "const order = hasOrder ? plan.order : Object.keys(steps);" in source

    def test_执行阶段也用后端steps回填planState(self) -> None:
        """回归：agentic 下 ``applyPlanResult`` 拿到的是**空 steps**（后端不再预分解 DAG），
        若执行阶段不回填，``stepTitle()`` / ``actionTitle()`` 只能回退英文 action ——
        工具卡标题与「产物」行会显示 ``file_list`` 这种机器输出（第 ② 类回归）。
        """
        source = _read("frontend/js/app.js")
        merge = _function_body(source, "mergePlanStateFromTask")
        assert "planState.steps = task.steps;" in merge
        # 执行完成（/run 等）与回看（GET /tasks/{id}）两条路径共用同一个收口函数。
        assert "mergePlanStateFromTask(task);" in _function_body(source, "applyRunResult")
        assert "mergePlanStateFromTask(task);" in _function_body(source, "applyTaskToView")


class TestTaskListWiring:
    """任务列表走 GET /task-summaries。"""

    def test_api层暴露任务摘要(self) -> None:
        source = _read("frontend/js/api.js")
        assert "listTaskSummaries: () => api.request('GET', '/task-summaries')" in source

    def test_app调用摘要端点并渲染(self) -> None:
        source = _read("frontend/js/app.js")
        assert "await api.listTaskSummaries()" in source
        assert "function renderTaskList(summaries)" in source
        # 展示标题 / 状态 / 是否待放行，当前任务高亮。
        assert "summary.title" in source
        assert "statusLabel(summary.status)" in source
        assert "summary.has_pending_approval" in source
        assert "is-current" in source

    def test_页面持有任务列表与新任务入口(self) -> None:
        html = _read("frontend/index.html")
        assert 'id="taskList"' in html
        assert 'id="newTaskBtn"' in html

    def test_列表项也显示已停下(self) -> None:
        """卡 5：非收敛停下时列表项不能还写「执行中」—— 与详情页的「已停下」矛盾。"""
        body = _function_body(_read("frontend/js/app.js"), "renderTaskList")
        assert "summary.stopped_reason" in body
        assert "LOOP_STOP_LABELS[summary.stopped_reason" in body
        assert "'已停下'" in body
        # 默认路径不变：没有停下原因时仍走原有状态文案。
        assert "statusLabel(summary.status)" in body


class TestTaskDeleteWiring:
    """任务列表每项可删：原生删除按钮 → api.deleteTask → DELETE /tasks/{id}。

    特别守住「HTML 不允许 button 嵌套 button」：删除按钮与主按钮是 .task-row 下的
    平级兄弟节点，而不是塞进 .task-item 内部。
    """

    def test_api层暴露删除任务(self) -> None:
        source = _read("frontend/js/api.js")
        assert "deleteTask: (id) => api.request('DELETE', `/tasks/${id}`)" in source

    def test_app调用删除接口并有处理器(self) -> None:
        source = _read("frontend/js/app.js")
        assert "api.deleteTask(taskId)" in source
        assert "async function handleDeleteTask(taskId)" in source
        # 失败提示 + 删除本地会话快照（避免刷新后被恢复）。
        assert "❌ 删除任务失败：" in source
        assert "delete taskSessions[taskId];" in source

    def test_删除按钮为原生button且带任务标识(self) -> None:
        source = _read("frontend/js/app.js")
        assert "del.dataset.deleteTask = summary.task_id;" in source
        assert "del.type = 'button';" in source
        # 点击不触发切换（stopPropagation）。
        assert "event.stopPropagation();" in source

    def test_删除按钮与主按钮是平级兄弟而非嵌套(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "renderTaskList")
        # 行容器承载两个平级 button。
        assert "row.className = 'task-row';" in body
        assert "row.append(btn, del);" in body
        # 删除按钮不再被塞进主按钮内部（button 嵌套 button）。
        assert "btn.append(title, meta);" in body
        segment = body.split("btn.append(title, meta);")[1].split("row.append(btn, del);")[0]
        assert "btn.append(del" not in segment
        assert "btn.appendChild(del" not in segment

    def test_删除当前任务复用重置路径(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "handleDeleteTask")
        # 删除当前任务 → 复用既有「＋ 新任务」重置路径，再刷新列表。
        assert "await startNewTask();" in body
        assert "await loadTaskSummaries();" in body

    def test_样式用语义token且不写死颜色(self) -> None:
        css = _read("frontend/styles.css")
        assert ".task-row" in css
        assert ".task-del" in css
        assert "var(--danger)" in css and "var(--danger-bg)" in css

    def test_删除前先弹确认且未确认不删(self) -> None:
        """破坏性操作必须先确认：确认调用出现在 deleteTask 之前，未确认直接返回。"""
        source = _read("frontend/js/app.js")
        body = _function_body(source, "handleDeleteTask")
        assert "await openConfirmDialog({" in body
        assert "if (!confirmed) return;" in body
        assert body.index("openConfirmDialog") < body.index("api.deleteTask")

    def test_确认弹窗复用既有对话框组件与token(self) -> None:
        html = _read("frontend/index.html")
        # 复用 .project-dialog 组件（与「新建文件夹」等弹窗同源）。
        assert 'class="project-dialog" id="confirmDialog"' in html
        assert 'id="confirmOk"' in html and 'id="confirmCancel"' in html
        assert 'class="key-btn key-btn-destructive"' in html
        assert 'role="dialog"' in html and 'aria-modal="true"' in html

    def test_破坏性默认聚焦取消并支持esc与遮罩关闭(self) -> None:
        source = _read("frontend/js/app.js")
        # 默认焦点给「取消」而不是删除：破坏性操作的默认动作不应是删除。
        assert "confirmCancelEl?.focus();" in source
        assert "confirmDialogEl.hidden = false;" in source
        # Esc 与点遮罩（data-close）都解析为取消，关闭时归还焦点。
        assert "!confirmDialogEl.hidden" in source and "settleConfirmDialog(false)" in source
        assert "confirmDialogEl?.querySelectorAll('[data-close]')" in source
        assert "if (trigger && typeof trigger.focus === 'function') trigger.focus();" in source

    def test_确认按钮用危险色token而非品牌渐变(self) -> None:
        css = _read("frontend/styles.css")
        assert ".key-btn.key-btn-destructive" in css
        assert "background: var(--danger);" in css
        # 对话框正文用可换行的普通字体类（.project-dialog-hint 是等宽+单行省略，不适用）。
        assert ".confirm-message" in css


class TestRunPauseAndResumeWiring:
    """默认直接跑 + 到点暂停：/run 自动调用，放行携带 approved_tools。"""

    def test_api层暴露run与带授权的resume(self) -> None:
        source = _read("frontend/js/api.js")
        assert "runTask: (id, approvedTools = null)" in source
        assert "resumeTask: (id, approvedTools = null)" in source
        # 授权字段名必须是后端契约的 approved_tools。
        assert "approved_tools: approvedTools" in source

    def test_app默认直接跑并渲染提醒卡(self) -> None:
        source = _read("frontend/js/app.js")
        # /plan 之后自动 /run（默认直接跑）；0 步分支不跑，直接给出可执行入口。
        plan_body = _function_body(source, "applyPlanResult")
        assert "await api.runTask(taskId)" in plan_body
        assert "applyRunResult(ran)" in plan_body
        # 首次发送即走同一「应用分解结果」函数（不复制粘贴）。
        assert "await applyPlanResult(task.task_id, result)" in source
        # 到点暂停 → 提醒卡渲染进 actionSlot，列出待放行工具/步骤。
        assert "function renderActionSlot(task)" in source
        assert "task.pending_approval" in source
        assert "function buildReminderCard(pending, taskId)" in source
        assert "pending.tools" in source
        assert "pending.steps" in source

    def test_放行调用resume并携带授权(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "handleApprovePending")
        assert "const taskId = cardTaskId(card);" in body
        assert "JSON.parse(card.dataset.approvedTools" in body
        assert "await api.resumeTask(taskId, tools)" in body

    def test_改计划与手动中断仍保留(self) -> None:
        source = _read("frontend/js/app.js")
        assert "function handleReject(card)" in source
        assert "api.rejectTask(taskId)" in source
        assert "function handleInterrupt()" in source
        assert "api.interruptTask(currentTaskId, 'user_stop')" in source


class TestInterruptWiring:
    """回归：「⏸ 中断」按钮必须在**执行期间**可用。

    ``/run``、``/resume`` 是同步阻塞接口（整段循环跑完才返回），前端若不先乐观置
    「执行中」，按钮（仅在 ``executing`` 且未停下时可点）在整段执行期间都是灰的。
    """

    def test_存在乐观置执行中的助手(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "markTaskRunning")
        assert "updateTaskStatus('executing')" in body
        # 置位前清掉「等待放行」「已停下」两个派生标记，否则 chip 会压住「执行中」。
        assert "currentPendingApproval = null;" in body
        assert "currentStopReason = '';" in body

    def test_run之前先置执行中(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "applyPlanResult")
        assert "markTaskRunning(taskId);" in body
        assert body.index("markTaskRunning(taskId)") < body.index("api.runTask(taskId)")

    def test_resume之前先置执行中(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "handleApprovePending")
        assert "markTaskRunning(taskId);" in body
        assert body.index("markTaskRunning(taskId)") < body.index("api.resumeTask(taskId, tools)")

    def test_中断按钮仍只在执行阶段可用(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "updateTaskStatus")
        assert "interruptBtnEl.disabled = status !== 'executing' || stopped;" in body


class TestArtifactAccess:
    """产物入口：从计划步骤 / 工具卡直接打开对应产物文件。"""

    def test_存在打开产物函数(self) -> None:
        source = _read("frontend/js/app.js")
        assert "function openArtifact(path, trigger)" in source
        assert "openWorkspaceFile({ path, size: null }, trigger)" in source

    def test_计划步骤与工具卡都有产物入口(self) -> None:
        source = _read("frontend/js/app.js")
        assert "data-artifact-path=" in source
        assert "查看产物" in source
        # 计划区块与工具卡两处都绑定点击打开。
        assert source.count("openArtifact(") >= 3

    def test_代码块走precode且经escapeHtml(self) -> None:
        source = _read("frontend/js/app.js")
        assert "<pre><code>${escapeHtml(code)}</code></pre>" in source
        assert "function extractCodeBlock(text)" in source

    def test_右栏为产物面板复用文件树与查看器(self) -> None:
        html = _read("frontend/index.html")
        assert 'id="fileTree"' in html
        assert 'id="fileViewer"' in html
        assert "产物" in html


class TestDesktopChromeIA:
    """标题栏 / 菜单栏 / 状态栏 IA 存在，顶栏用单一状态标签取代六阶段固定流程。"""

    def test_页面含三类chrome(self) -> None:
        html = _read("frontend/index.html")
        assert 'class="titlebar"' in html
        assert 'class="menubar"' in html
        assert 'class="statusbar"' in html
        for menu in ('data-menu="file"', 'data-menu="edit"', 'data-menu="view"',
                     'data-menu="task"', 'data-menu="help"'):
            assert menu in html, menu

    def test_状态栏四项(self) -> None:
        html = _read("frontend/index.html")
        for element_id in ("statusBackend", "statusTask", "statusModel", "statusKey"):
            assert f'id="{element_id}"' in html, element_id

    def test_顶栏为单一状态标签(self) -> None:
        html = _read("frontend/index.html")
        assert 'class="status-chip"' in html
        assert 'id="taskStatusChip"' in html
        assert 'id="taskStatusText"' in html
        source = _read("frontend/js/app.js")
        assert "function updateTaskStatus(status)" in source

    def test_六阶段流程已不存在(self) -> None:
        # 固定六阶段（需求/计划/确认/执行/验证/汇报）的 UI 与逻辑必须彻底移除。
        html = _read("frontend/index.html")
        source = _read("frontend/js/app.js")
        assert "stage-bar" not in html
        assert "STAGES" not in source
        assert "STATUS_TO_STAGE" not in source
        # 取而代之的是单一状态标签：结构、脚本常量、样式三处齐备。
        assert "taskStatusChip" in html
        assert "STATUS_CHIP_STATE" in source
        assert "status-chip" in _read("frontend/styles.css")

    def test_菜单可展开且带快捷键标注(self) -> None:
        source = _read("frontend/js/app.js")
        assert "function openMenu(name, rootBtn)" in source
        assert "function runMenuAction(action)" in source
        assert "accel:" in source

    def test_交互一律原生button(self) -> None:
        # 菜单项、任务项、状态栏项、产物入口都必须是 <button>。
        source = _read("frontend/js/app.js")
        assert "btn.type = 'button';" in source
        assert '<button type="button" class="plan-artifact-btn"' in source
        html = _read("frontend/index.html")
        assert '<button type="button" class="status-item"' in html

    def test_非原生按钮的role_button已清零(self) -> None:
        """约束：交互一律原生 <button> —— div/span + role=button 的 ARIA 变通不得存在。

        文件树行与「＋ 添加插件」此前是 ``div role=button`` + keydown 兜底，现已换回原生元素。
        """
        source = _read("frontend/js/app.js")
        html = _read("frontend/index.html")
        assert "setAttribute('role', 'button')" not in source
        assert 'role="button"' not in html
        # 文件树行与「添加插件」都已是原生 <button>（键盘/读屏语义由原生元素自带）。
        assert "document.createElement('button')" in _function_body(source, "renderFileTree")
        assert '<button type="button" class="plugin-add"' in html


class TestNoDanglingElementIds:
    """app.js 里 getElementById 引用的 id 必须都能在 index.html 找到。"""

    def test_引用的id都存在(self) -> None:
        source = _read("frontend/js/app.js")
        html = _read("frontend/index.html")
        ids = sorted(set(re.findall(r"getElementById\('([^']+)'\)", source)))
        assert ids, "未解析到任何 getElementById"
        missing = [i for i in ids if f'id="{i}"' not in html]
        assert not missing, f"app.js 引用了 index.html 不存在的 id: {missing}"


class TestNoPlanCardWiring:
    """/plan 返回 0 步时渲染「暂无可用计划」卡片，而非走进死路。"""

    def test_0步分支不再硬编码归因也不建议中断(self) -> None:
        source = _read("frontend/js/app.js")
        # 不再替后端猜测原因（真实原因在 pending_questions 里）。
        assert "未启用 LLM 拆分" not in source
        # 0 步分支不得再引导用户点击此时必然禁用的「⏸ 中断」。
        body = _function_body(source, "applyPlanResult")
        assert "stepCount === 0" in body
        assert "⏸ 中断" not in body

    def test_存在暂无可用计划卡与三个操作入口(self) -> None:
        source = _read("frontend/js/app.js")
        assert "function renderNoPlanCard(taskId, questions, extra)" in source
        assert "function appendNoPlanCard(taskId, questions, extra)" in source
        # 标题 + 原样展示后端 pending_questions（不猜测/不改写）。
        assert "暂无可用计划" in source
        assert "pendingQuestionText(item)" in source
        assert "appendNoPlanCard(taskId, questions" in source
        # 三个原生 <button>：重新规划 / 改计划 / 放弃任务。
        for cls in ("btn-replan", "btn-reject", "btn-danger"):
            assert cls in source, cls
        assert "重新规划" in source
        assert "放弃任务" in source
        # 卡片纳入会话条目重建（刷新后可回看），与 DOM 裁剪机制一致。
        assert "entry.kind === 'noplan'" in source
        assert "kind: 'noplan'," in source

    def test_卡片按钮走卡片任务作用域(self) -> None:
        source = _read("frontend/js/app.js")
        card_body = _function_body(source, "renderNoPlanCard")
        # 卡片挂 data.taskId 供 cardTaskId 读取，按钮绑定均传卡片本身。
        assert "card.dataset.taskId = taskId" in card_body
        for wiring in ("handleReplan(card)", "handleReject(card)", "handleAbort(card)"):
            assert wiring in card_body, wiring
        # 「重新规划」复用底部栏规划选项，且只读卡片任务 ID，不读全局 currentTaskId。
        replan_body = _function_body(source, "handleReplan")
        assert "const taskId = cardTaskId(card);" in replan_body
        assert "currentTaskId" not in replan_body
        assert "readPlanOptions()" in replan_body
        assert "api.planTask(taskId, useLLM, planOptions)" in replan_body
        # 结果复用与首次发送一致的后续逻辑（同一函数，不再复制粘贴）。
        assert "await applyPlanResult(taskId, result);" in replan_body

    def test_静态资源引用带同一版本号(self) -> None:
        html = _read("frontend/index.html")
        for ref in ('href="styles.css?v=', 'src="js/api.js?v=', 'src="js/app.js?v='):
            assert ref in html, ref
        versions = set(re.findall(r'\?v=([0-9A-Za-z._-]+)', html))
        assert len(versions) == 1, f"三处静态资源版本号必须一致：{versions}"
        # 注释提示后续改资源要 bump 版本号（静态服务器不发 Cache-Control）。
        assert "版本号" in html


class TestPauseCardsOfferReject:
    """暂停（到点暂停 / 手动中断）后也能「改计划」——卡片入口与后端状态机已同时放行。"""

    def test_两张暂停卡都带改计划入口(self) -> None:
        source = _read("frontend/js/app.js")
        for name in ("buildReminderCard", "buildInterruptCard"):
            body = _function_body(source, name)
            assert "btn-reject" in body, name       # 原生 <button>（不新增 div+onclick）
            assert "handleReject(card)" in body, name  # 走卡片任务作用域


class TestExecutionCardShowsHumanText:
    """执行结果卡默认展示人话（summary / 中文动作名），不露原始 JSON 与英文 action。

    agentic 每轮决策与固定工作流共用这张卡，所以两边一起守住。
    """

    def test_卡片优先展示人话摘要(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "renderExecutionResults")
        # 详情一律经 toolCardDetail 收口：优先 summary，失败走 humanizeError，
        # 绝不把原始 error / result 裸倒给用户。
        assert "toolCardDetail(item)" in body
        # 标题用后端下发的中文动作名；缺失才回退英文 action。
        assert "stepTitle(item.step_id, item.action)" in body
        assert "function stepTitle(stepId, fallback)" in source


class TestProposalCard:
    """副结构提议卡的接线：入口、两个决定按钮、以及"不越权"的文案必须写着。"""

    def test_提议卡在动作槽里且有优先级(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "renderActionSlot")
        # 提议卡优先于中断卡：此刻真正等的是"要不要扩编"的决定
        assert "task.pending_proposal" in body
        assert "buildProposalCard(task.pending_proposal, task.task_id)" in body
        assert body.index("buildProposalCard") < body.index("buildInterruptCard")

    def test_两个决定按钮都绑到同一个处理器(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "buildProposalCard")
        assert "handleProposalDecision(card, true)" in body
        assert "handleProposalDecision(card, false)" in body
        assert "btn-run" in body and "btn-reject" in body  # 复用既有按钮样式

    def test_卡片写明不改仓库既有文件(self) -> None:
        """批准 = 只写工作区脚手架；这条必须出现在用户看得到的地方，不能只写在文档里。"""
        body = _function_body(_read("frontend/js/app.js"), "buildProposalCard")
        assert "不改仓库既有文件" in body
        assert "tools/permissions.py" in body  # 告知"并入授权后才生效"

    def test_api层有两个端点(self) -> None:
        source = _read("frontend/js/api.js")
        assert "approveProposal: (id) => api.request('POST', `/tasks/${id}/proposal/approve`)" in source
        assert "rejectProposal: (id) => api.request('POST', `/tasks/${id}/proposal/reject`)" in source


class TestProcessBlock:
    """过程块：默认收起的过程收纳（工具卡 + 过程说明），三类情况必须自动展开。"""

    def test_容器自身是一条chat_item(self) -> None:
        """裁剪只认 `:scope > .chat-item`：容器算一条，内部节点必须去掉该类。"""
        source = _read("frontend/js/app.js")
        ensure = _function_body(source, "ensureProcessBlock")
        assert "block.className = 'process-block chat-item'" in ensure
        mount = _function_body(source, "mountProcessNode")
        assert "node.classList.remove('chat-item')" in mount
        # 裁剪不变式本身没被改动
        assert "log.querySelectorAll(':scope > .chat-item')" in _function_body(source, "trimChatArea")

    def test_默认收起(self) -> None:
        """创建时不带 open；只有 refreshProcessBlock 的例外分支才会 open。"""
        source = _read("frontend/js/app.js")
        assert "open = true" not in _function_body(source, "ensureProcessBlock")
        assert "block.open = true" in _function_body(source, "refreshProcessBlock")

    def test_三类情况必须自动展开(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "shouldExpandProcess")
        assert "processHasFailure()" in body  # 有失败
        assert "task.pending_approval" in body  # 有待放行
        assert "task.pending_proposal" in body  # 有需要你决定（扩编）
        assert "awaiting_confirm" in body and "interrupted" in body

    def test_手动收起后不被反复弹开(self) -> None:
        """自动展开要"记一次账"，否则每轮刷新都会跟用户的手动收起打架。"""
        body = _function_body(_read("frontend/js/app.js"), "refreshProcessBlock")
        assert "dataset.autoOpened" in body

    def test_工具卡与自检都进过程块(self) -> None:
        source = _read("frontend/js/app.js")
        assert "mountProcessNode(card)" in _function_body(source, "renderToolCard")
        selfcheck = _function_body(source, "renderSelfCheck")
        assert "appendProcessNote(" in selfcheck
        assert "appendMessage(" not in selfcheck  # 自检不再占对话正文
        assert "{ failed: true }" in selfcheck  # 自检发现问题 → 触发自动展开

    def test_摘要是一行人话(self) -> None:
        """摘要行对齐 DeepSeek 网页版：「已深度思考（用时 N 秒）」+ 可核对的步数。"""
        body = _function_body(_read("frontend/js/app.js"), "refreshProcessBlock")
        assert "已深度思考" in body
        assert "用时 ${elapsed}" in body
        assert "已完成 ${" in body
        assert "失败 ${failed} 次" in body
        # 用时（后端暂无单步耗时 → 只给总时长）；追加节点触发的「无上下文刷新」也要能算出来，
        # 否则后到的追加会把带用时的摘要覆盖掉。
        assert "const ctx = task || lastProcessTask;" in body
        assert "taskElapsedText(ctx)" in body
        # 快照必须在**提前 return 之前**记下：过程块由第一批工具卡才建起来，那次调用会提前返回。
        assert body.index("if (task) lastProcessTask = task;") < body.index(
            "if (!block || !block.isConnected) return;"
        )
        # 「待审批」既不算完成也不算失败，否则"到点暂停"时摘要会虚报步数。
        assert "待放行 ${waiting} 步" in body

    def test_决策理由收进过程块(self) -> None:
        """思考（agentic 每轮的 thought）以前哪都不显示，现在随工具卡进过程块。"""
        source = _read("frontend/js/app.js")
        card = _function_body(source, "renderToolCard")
        assert "tool-card-thought" in card
        assert "escapeHtml(thought)" in card
        # 执行结果卡把 thought 传下去；刷新重建时也从记录里恢复。
        assert "thought: item.thought," in _function_body(source, "renderExecutionResults")
        assert "thought: entry.thought," in _function_body(source, "renderEntry")
        assert "thought: extra && extra.thought ? extra.thought : null," in _function_body(
            source, "appendToolCard"
        )

    def test_恢复会话时过程说明不丢(self) -> None:
        """刷新页面要从记录里重建：新增的记录类型必须有对应的渲染分支。"""
        source = _read("frontend/js/app.js")
        assert "entry.kind === 'process-note'" in _function_body(source, "renderEntry")
        assert "kind: 'process-note'" in _function_body(source, "appendProcessNote")

    def test_产物入口留在正文(self) -> None:
        """过程默认收起，但"产出了什么、去哪看"必须一眼可见（规格里的默认保留项）。"""
        source = _read("frontend/js/app.js")
        row = _function_body(source, "renderArtifactRow")
        assert "mountChatNode(row)" in row  # 挂在正文，不进过程块
        assert "artifact-row chat-item" in row
        assert "查看产物" in row
        assert "appendArtifactRow(extra.path, title, stepId)" in _function_body(source, "appendToolCard")
        assert "entry.kind === 'artifact'" in _function_body(source, "renderEntry")

    def test_过程块样式只用token(self) -> None:
        css = _read("frontend/styles.css")
        assert ".process-block {" in css
        assert ".process-summary::before" in css
        block = css[css.index(".process-block {") : css.index(".process-block {") + 320]
        assert "var(--border-strong)" in block
        assert "#" not in block  # 不写死颜色


class TestReopenShowsExecutionResults:
    """回归：点开任务列表里的任务时，执行过程必须从**后端**重建。

    实测（「帮我做一个调研agent」）：不是本机跑过的任务点开只有一句结论，
    后端明明有 4 条执行结果 —— 4 张卡、失败原因、产物入口全看不到。
    """

    def test_回看时按后端结果渲染执行过程(self) -> None:
        source = _read("frontend/js/app.js")
        assert "renderExecutionResults(task);" in _function_body(source, "applyTaskToView")

    def test_本地记录与后端结果共用同一个键(self) -> None:
        """键不一致 → 两份来源各渲染一套卡片（回看时工具卡翻倍）。"""
        source = _read("frontend/js/app.js")
        assert "function stepIdFromTitle(title)" in source
        assert "entry.stepId || stepIdFromTitle(entry.title)" in _function_body(source, "renderEntry")
        assert "stepId: item.step_id," in _function_body(source, "renderExecutionResults")


class TestStopCard:
    """回归：agentic **非收敛停下**（预算耗尽 / 空转 / 决策不可解析）后，
    用户要看到原因与出口 —— 此前只看到顶栏「执行中」，既没解释也没按钮。
    """

    def test_四种停下原因有中文文案(self) -> None:
        source = _read("frontend/js/app.js")
        assert "const LOOP_STOP_LABELS = {" in source
        for key in ("budget_steps", "budget_tokens", "stagnant", "invalid_decision"):
            assert key in source, key
        assert "连续两轮没有新进展" in source
        assert "function loopStopReason(task)" in source

    def test_停下卡只在仍挂在执行中时出现(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "renderActionSlot")
        # 严格门控：failed / delivered / verifying 各有终态文案，不能被「已停下」盖掉
        # （实测：用户主动「放弃」后顶栏曾被误写成「已停下」）。
        assert "task.status === 'executing' ? loopStopReason(task) : ''" in body
        assert "slot.appendChild(buildStopCard(task));" in body
        # 优先级：待放行 > 等扩编决定 > 已暂停 > 已停下。
        assert body.index("buildReminderCard") < body.index("buildStopCard(task)")
        assert body.index("buildInterruptCard") < body.index("buildStopCard(task)")

    def test_停下卡给出原因与出口(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "buildStopCard")
        assert "执行已停下" in body
        assert "需求尚未完成" in body
        assert "escapeHtml(reason)" in body  # 原因经转义
        assert "handleAbort(card)" in body  # 唯一可用操作：放弃任务
        assert "btn-danger" in body  # 原生 <button>

    def test_顶栏不再谎报执行中(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "updateTaskStatus")
        assert "const stopped = !waiting && Boolean(currentStopReason);" in body
        assert "stopped ? '已停下'" in body
        assert "status !== 'executing' || stopped" in body  # 已停下时禁用「中断」
        assert "currentStopReason = '';" in _function_body(source, "resetActiveView")
        assert "currentStopReason = '';" in _function_body(source, "switchToTask")

    def test_停下卡样式只用token(self) -> None:
        css = _read("frontend/styles.css")
        assert ".stop-card {" in css
        block = css[css.index(".stop-card {") : css.index(".stop-card {") + 260]
        assert "var(--warn)" in block
        assert "#" not in block  # 不写死颜色


class TestDeliverCard:
    """回归：收敛后停在 ``verifying`` 的任务此前**没有任何出口** —— 既不自动交付、
    UI 也没按钮 → 永远停在顶栏「验证中」。现在给出「✓ 确认交付」。
    """

    def test_验证中渲染交付卡(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "renderActionSlot")
        assert "task.status === 'verifying'" in body
        assert "slot.appendChild(buildDeliverCard(task));" in body
        # 优先级：待放行 / 等扩编 / 已暂停 / 已停下 都排在「验证中」之前。
        assert body.index("buildStopCard(task)") < body.index("buildDeliverCard(task)")

    def test_交付卡给出唯一出口(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "buildDeliverCard")
        assert "确认交付" in body
        assert "handleDeliver(card)" in body
        assert "btn-resume" in body  # 原生 <button>

    def test_交付走后端端点并回写状态(self) -> None:
        api = _read("frontend/js/api.js")
        assert "deliverTask: (id) => api.request('POST', `/tasks/${id}/deliver`)" in api

        body = _function_body(_read("frontend/js/app.js"), "handleDeliver")
        assert "api.deliverTask(taskId)" in body
        assert "syncTaskStatus(taskId, task.status)" in body


class TestNoDuplicateStepCards:
    """回归：放行后 ``/resume`` 会回传**全部**执行结果 —— 前端必须按步骤号去重。

    否则同一个 step 会同时留一张「待审批」和一张「已完成」的卡，过程块摘要的步数也跟着虚增。
    （浏览器实测发现：放行后卡片从 3 张变 6 张。）
    """

    def test_工具卡按步骤号去重(self) -> None:
        source = _read("frontend/js/app.js")
        drop = _function_body(source, "dropPreviousStepNodes")
        assert "data-step-id=" in drop
        # 选择器必须限定节点类型：工具卡 / 产物行 / 计划行都用同一个步骤号。
        assert "selector}[data-step-id=" in drop
        assert "dropPreviousStepNodes(stepId, '.tool-card');" in _function_body(source, "appendToolCard")
        assert "dropPreviousStepNodes(stepId, '.artifact-row');" in _function_body(
            source, "appendArtifactRow"
        )
        entry = _function_body(source, "renderEntry")
        # 历史记录没有 stepId（旧版本写入的）→ 从标题回推步骤号；键必须与后端 step_id 一致。
        assert "const stepKey = entry.stepId || stepIdFromTitle(entry.title);" in entry
        assert "const stepKey = entry.stepId || stepIdFromTitle(entry.title) || entry.path || '';" in entry
        assert "dropPreviousStepNodes(stepKey, '.tool-card');" in entry
        assert "dropPreviousStepNodes(stepKey, '.artifact-row');" in entry
        assert "split(' · ')" in _function_body(source, "stepIdFromTitle")
        # 步骤号由执行结果带进来，卡片与产物行共用它。
        assert "stepId: item.step_id," in _function_body(source, "renderExecutionResults")
        assert "row.dataset.stepId = String(stepId);" in _function_body(source, "renderArtifactRow")

    def test_会话记录按步骤号去重(self) -> None:
        """DOM 去重了，transcript 也要去重 —— 否则刷新后又按旧记录重建出重复卡片。"""
        body = _function_body(_read("frontend/js/app.js"), "recordEntry")
        assert "item.kind === entry.kind && item.stepId === entry.stepId" in body


class TestChipNotStaleAfterResume:
    """回归：放行后顶栏不能停在「等待放行」（实测 chip 与真实状态不符）。"""

    def test_重算当前状态标签(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "renderActionSlot")
        assert "if (currentStatus) updateTaskStatus(currentStatus);" in body


class TestNoMachineOutput:
    """四类机器输出不得回到默认视图：①原始 JSON ②英文 action ③key=value ④异常原文。"""

    def test_输入摘要不再是key_value(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "summarizeInputs")
        assert "description" in body  # 优先后端下发的人读描述
        assert "${key}=" not in body and "key}=" not in body  # ③
        assert "文件：" in body
        assert "参数：" in body

    def test_计划块不露英文action名(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "renderPlanBlock")
        assert "step.title" in body
        assert "actionLabel" in body
        assert "escapeHtml(step.action)}${risky" not in body  # 旧写法（直接渲染英文 action）
        # 高风险提示也从 action 反查中文名
        assert "titleOfAction(a)" in body

    def test_提醒卡待放行清单用人话(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "buildReminderCard")
        assert "actionTitle(t)" in body  # 待放行工具
        assert "stepTitle(s.step_id, s.action)" in body  # 待放行步骤
        assert "escapeHtml(s.action)" not in body

    def test_工具卡不倒原始error与result(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "toolCardDetail")
        assert "item.summary" in body  # 人话优先
        assert "humanizeError(item.error)" in body  # ④ 走收敛
        assert "item.error ||" not in body and "item.result ||" not in body  # 不再直出
        assert "typeof result === 'string'" in body  # ① 非字符串结果不展示
        humanize = _function_body(_read("frontend/js/app.js"), "humanizeError")
        assert "Error:" in humanize and "Exception:" in humanize
        assert "E_[A-Z0-9_]+" in humanize  # 错误码前缀（E_VALIDATION: …）也要剥掉

    def test_执行卡走收口函数(self) -> None:
        source = _read("frontend/js/app.js")
        assert "toolCardDetail(item)" in source
        assert "item.summary || item.error" not in source

    def test_交接卡机器未决用人话(self) -> None:
        """交接卡（卡片 + 复制文本）里的机器未决同样不得露英文 action。"""
        source = _read("frontend/js/app.js")
        assert source.count("item.title || item.action") >= 2  # 卡片 + 复制文本
        assert "escapeHtml(item.action)}</span>" not in source  # 旧写法
        assert "item.step_id)} ${item.action}" not in source  # 旧写法（直接拼英文 action）


class TestApiErrorText:
    """后端结构化 detail（对象 / 422 数组）不得渲染成 "[object Object]"。"""

    def test_不再直接拼detail(self) -> None:
        source = _read("frontend/js/api.js")
        assert "apiErrorText(data, resp.status)" in source
        assert "data.message || data.detail ||" not in source  # 旧写法（对象 → [object Object]）

    def test_覆盖三种detail形态(self) -> None:
        body = _function_body(_read("frontend/js/api.js"), "apiErrorText")
        assert "Array.isArray(raw)" in body  # FastAPI 422：数组
        assert "item.msg" in body  # 422 校验项
        assert "raw.message" in body  # exc.to_dict()：对象
        assert "请求失败（HTTP ${status}）" in body  # 兜底不露原始 status code 语境之外的机器串


class TestReplanFromAwaitingConfirm:
    """「重新规划」必须先退回 planning。

    卡片出现时任务在 awaiting_confirm，而 ``/plan`` 只接受 PLANNING，
    直接调用必然 409「非法状态转换: awaiting_confirm + plan_ready」。
    """

    def test_卡片记录可退计划标记(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "renderNoPlanCard")
        assert "card.dataset.canReject = extra && extra.canReject ? '1' : ''" in body

    def test_重新规划先退计划再分解(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "handleReplan")
        assert "if (card.dataset.canReject === '1') await api.rejectTask(taskId);" in body
        # 退计划必须早于重新分解
        assert body.index("api.rejectTask(taskId)") < body.index("api.planTask(taskId")


class TestChatFirst:
    """发送按钮先走 /chat：闲聊不该变成一份执行计划。"""

    def test_api层暴露对话入口(self) -> None:
        source = _read("frontend/js/api.js")
        assert "chat: (message, history = [], options = {}) =>" in source
        assert "'/chat'" in source

    def test_发送先分流再建任务(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "handleSend")
        assert "api.chat(requirement, recentChatHistory()" in body
        assert "verdict.kind !== 'task'" in body
        assert "appendMessage('agent', verdict.reply)" in body
        # 分流必须早于建任务 —— 否则又回到「无条件弹计划」
        assert body.index("api.chat(") < body.index("api.createTask(")

    def test_上下文只取纯文本消息(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "recentChatHistory")
        assert "entry.kind === 'msg'" in body
        assert "role: entry.role === 'user' ? 'user' : 'assistant'" in body


class TestArtifactEntry:
    """「查看产物」只在真正产出文件时出现，且不再拿 inputs.path 冒充。"""

    def test_计划块产物按钮来自artifacts(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "renderPlanBlock")
        assert "stepArtifactPath(res)" in body
        assert "step.inputs || {}).path" not in body  # 旧写法：目录也会带按钮

    def test_工具卡用同一个判定(self) -> None:
        source = _read("frontend/js/app.js")
        assert "stepArtifactPath(item)" in _function_body(source, "renderExecutionResults")
        # 旧启发式必须彻底消失，否则只读步骤又会长出「查看产物」
        assert "stepInputPath" not in source

    def test_产物判定只看artifacts(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "stepArtifactPath")
        assert "item.artifacts" in body
        assert "inputs" not in body

    def test_打开失败给人话与去处(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "openWorkspaceFile")
        assert "打开失败：" in body
        assert "产物" in body


class TestAgenticHidesPlanBlock:
    """agentic 下没有固定 DAG 可展示：计划块只在 workflow 模式渲染。"""

    def test_计划块按模式短路(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "renderPlanBlock")
        assert "EXECUTION_MODE !== 'workflow'" in body
        # 非 workflow / 无计划 → 把已折进过程块的那份撤掉，别留下上一份计划。
        assert "existing.remove()" in body
        # 有计划 → 就地替换（保持位置），否则每次刷新都会多出一份。
        assert "existing.replaceWith(block)" in body

    def test_模式显式下发且与后端默认一致(self) -> None:
        source = _read("frontend/js/app.js")
        assert "const EXECUTION_MODE = 'agentic'" in source
        assert "mode: EXECUTION_MODE" in _function_body(source, "readPlanOptions")

    def test_不再引导用户去找计划块(self) -> None:
        source = _read("frontend/js/app.js")
        assert "（见计划区块）" not in source
        # 文案按模式分流：agentic 只说「收到，开始执行。」
        body = _function_body(source, "applyPlanResult")
        assert "const mode = result.mode || EXECUTION_MODE;" in body  # 以后端回显为准
        assert "'收到，开始执行。'" in body
        assert "计划已生成，共 ${stepCount} 个步骤" in body
        # agentic 的「0 步」不再被当成「暂无可用计划」；
        # 只有后端明确给了未决问题（真走不下去）才落到卡片上。
        assert "stepCount === 0 && (mode === 'workflow' || questions.length)" in body


class TestConclusionBlock:
    """结论区：跑完给一句人话结论（后端 `task.conclusion`），留在正文便于一眼看到。"""

    def test_结论块留在正文不折进过程块(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "renderConclusion")
        assert "task.conclusion" in body
        assert "mountChatNode(conclusionBlockEl)" in body  # 正文，而非过程块
        assert "mountProcessNode(" not in body
        assert "'conclusion-block chat-item'" in body
        # 一个任务只保留一条：默认先撤掉旧的，避免每轮刷新叠出一堆结论。
        assert "conclusionBlockEl.remove()" in body

    def test_执行完与回看时都渲染结论(self) -> None:
        source = _read("frontend/js/app.js")
        assert "renderConclusion(task);" in _function_body(source, "applyRunResult")
        # 结论不入 transcript（由后端状态派生）→ 刷新 / 切换任务时必须能重建。
        assert "renderConclusion(task);" in _function_body(source, "applyTaskToView")

    def test_有结论时不重复播报执行进度(self) -> None:
        source = _read("frontend/js/app.js")
        assert "if (task && task.conclusion) return;" in _function_body(source, "renderRunSummary")
        # 结论块随对话区一起清空：引用要同步重置，避免指向脱离文档的旧块。
        assert "conclusionBlockEl = null;" in _function_body(source, "clearChatUI")
        assert "conclusionBlockEl = null;" in _function_body(source, "resetActiveView")

    def test_结论块样式只用token(self) -> None:
        css = _read("frontend/styles.css")
        assert ".conclusion-block {" in css
        block = css[css.index(".conclusion-block {") : css.index(".conclusion-block {") + 260]
        assert "var(--success-text)" in block
        assert "#" not in block  # 不写死颜色

    def test_结论保留换行(self) -> None:
        """结论是给用户看的成品（可含短列表 / 换行）→ 不能被压成一长条。"""
        css = _read("frontend/styles.css")
        start = css.index(".conclusion-body {")
        assert "white-space: pre-wrap" in css[start : start + 300]


class TestMarkdownRendering:
    """助手输出按 Markdown 子集渲染（对齐 DeepSeek 网页版的正文形态）。

    背景（实测）：结论与助手消息此前都是 `textContent`（生文本）—— 模型写的
    `- 列表` / `**加粗**` / 围栏代码块全部原样显示，这是与 DeepSeek 网页版差距最大的一处。
    """

    def test_渲染函数先转义再拼白名单标签(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "renderMarkdown")
        assert "escapeHtml(" in body
        assert "md-code" in body
        assert "md-copy" in body

    def test_结论用_markdown_渲染而非纯文本(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "renderConclusion")
        assert "renderMarkdown(text)" in body
        assert "innerHTML" in body
        assert "textContent = text" not in body  # 这条正是原先的生文本写法

    def test_助手消息渲染_markdown_用户消息保持纯文本(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "renderMessage")
        assert "renderMarkdown(text)" in body
        assert "role === 'user'" in body
        assert "textContent = text" in body  # 用户那条仍按纯文本，不做格式化

    def test_代码块复制有委托处理(self) -> None:
        source = _read("frontend/js/app.js")
        assert ".md-copy" in source
        assert "copyToClipboard(code.textContent)" in source

    def test_样式覆盖_md_正文与代码卡(self) -> None:
        css = _read("frontend/styles.css")
        for selector in (".md p {", ".md table {", ".md-code {", ".md-copy {"):
            assert selector in css, f"styles.css 缺少 {selector}"
        # Markdown 正文不能沿用 pre-wrap（标签间换行会被渲染成多余空行）
        assert ".md {" in css
        start = css.index(".md {")
        assert "white-space: normal" in css[start : start + 200]

    def test_代码块与表格样式只用token(self) -> None:
        css = _read("frontend/styles.css")
        for anchor in (".md-code {", ".md-copy {"):
            block = css[css.index(anchor) : css.index(anchor) + 260]
            assert "#" not in block, f"{anchor} 写死了颜色"


class TestMarkdownRenderingBehavior:
    """用 node **真跑**渲染器（没有 node 的环境自动跳过）。

    为什么值得单独一组：本文件其余前端测试都是「读源码做字符串断言」——能保证"写了这段代码"，
    保证不了"渲染结果对"。实测正是这组抓到一处真 bug：先 escapeHtml 会把 `>` 变成 `&gt;`，
    于是引用块的 `> ` 标记再也匹配不上，引用被当普通段落渲染了。
    """

    def test_转义之后引用_代码块_XSS_都正确(self, tmp_path) -> None:
        import json
        import shutil
        import subprocess

        import pytest

        node = shutil.which("node")
        if not node:
            pytest.skip("本环境没有 node，跳过渲染行为验证")

        source = _read("frontend/js/app.js")

        def extract(name: str) -> str:
            start = source.index(f"function {name}(")
            brace = source.index("{", start)
            depth = 0
            for idx in range(brace, len(source)):
                if source[idx] == "{":
                    depth += 1
                elif source[idx] == "}":
                    depth -= 1
                    if depth == 0:
                        return source[start : idx + 1]
            raise AssertionError(f"函数不配对: {name}")

        sample = (
            "# 标题\n\n"
            "**加粗** 与 `行内码`\n\n"
            "- 项一\n- 项二\n\n"
            "```python\nprint(1)\n```\n\n"
            "> 引用一行\n\n"
            "<script>alert(1)</script>\n"
        )
        js = "\n".join(extract(n) for n in ("escapeHtml", "mdInline", "renderMarkdown"))
        js += f"\nconst sample = {json.dumps(sample)};\nconsole.log(renderMarkdown(sample));\n"
        script = tmp_path / "md-check.js"
        script.write_text(js, encoding="utf-8")

        proc = subprocess.run(
            [node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False
        )
        out = proc.stdout or ""
        assert out, proc.stderr  # node 起不来/语法错误时给出原因

        assert "<h1>标题</h1>" in out
        assert "<strong>加粗</strong>" in out
        assert "<blockquote>" in out  # ← 先转义后仍要认得 `&gt;` 标记
        assert "md-code" in out and "md-copy" in out
        # XSS：样本里的 <script> 只能以实体形式出现
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


class TestFileTreeKeepsCollapsedState:
    """回归：↻ 刷新重建文件树时，折叠状态不再被重置（实测折叠的目录又展开了）。"""

    def test_折叠状态按路径记住并复用(self) -> None:
        source = _read("frontend/js/app.js")
        assert "const collapsedDirs = new Set();" in source
        body = _function_body(source, "renderFileTree")
        assert "collapsedDirs.has(node.path)" in body      # 重建时读回
        assert "collapsedDirs.add(node.path)" in body      # 折叠时记下
        assert "collapsedDirs.delete(node.path)" in body   # 展开时移除


class TestLargeFilePreviewWiring:
    """大文件只读截断预览：内容被截断 → 禁止编辑、不给「保存」，并说明原因。"""

    def test_截断内容禁止编辑且提示(self) -> None:
        body = _function_body(_read("frontend/js/app.js"), "openWorkspaceFile")
        assert "file.truncated" in body
        assert "fileViewerBody.readOnly = true" in body
        assert "过大，仅只读预览" in body


class TestCssColorTokens:
    """约束：颜色只写 CSS token —— 颜色字面量只允许出现在 :root 的 token 定义里。"""

    def test_规则体里没有颜色字面量(self) -> None:
        css = _read("frontend/styles.css")
        # 去掉注释与两段 token 定义（浅色 :root + 暗色 :root），规则体里不应再有颜色字面量。
        body = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        body = re.sub(r":root\s*\{[^}]*\}", "", body)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "规则体里仍有十六进制颜色字面量"
        assert not re.search(r"\brgba?\(", body), "规则体里仍有 rgb/rgba 颜色字面量"

    def test_中性前景与投影已token化并引用(self) -> None:
        css = _read("frontend/styles.css")
        for token in ("--on-emphasis", "--toggle-knob", "--shadow-knob",
                      "--shadow-modal", "--shadow-card", "--shadow-drawer"):
            assert f"{token}:" in css, token          # 在 :root 里定义
            assert f"var({token})" in css, token      # 被规则引用


def _function_body(source: str, name: str) -> str:
    """粗切函数体：从 `function <name>` 到下一个顶层 `}` 之后。"""
    start = source.index(f"function {name}(")
    match = re.search(r"\n}\n", source[start:])
    end = start + (match.end() if match else len(source) - start)
    return source[start:end]
