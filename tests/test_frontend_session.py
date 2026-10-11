"""前端会话/任务交互缺陷回归（静态检查，新 IA）。

守住四处修复不被回退（语义与原版一致，仅随新 IA 调整函数名）：
1. 放行/改计划/恢复/放弃等卡片操作按「卡片所属任务」执行，不读全局 currentTaskId
   （多任务并存后尤其不能串扰）；顶栏状态标签只允许当前任务更新；
2. 发送请求在途时禁用发送按钮（防连点重复建任务）；
3. 切换 / 新建任务 / 切换工作区前先终止后端任务，再清空界面（防孤儿任务）；
4. 会话条目有上限并持久化到 sessionStorage，刷新后可恢复；且多任务会话并存可回看。
"""

from __future__ import annotations

import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (_PROJECT_ROOT / relative).read_text(encoding="utf-8")


class TestCardScopedToTask:
    """卡片操作必须绑定所属任务，而非全局当前任务。"""

    def test_卡片持有任务ID与取用助手(self) -> None:
        source = _read("frontend/js/app.js")
        assert "card.dataset.taskId" in source
        assert "function cardTaskId(card)" in source
        assert "function syncTaskStatus(taskId, status)" in source

    def test_卡片操作走卡片任务ID(self) -> None:
        source = _read("frontend/js/app.js")
        # 新 IA：主入口是「放行并继续」（/resume 携带 approved_tools）；改计划/恢复/放弃保留。
        for call in (
            "api.resumeTask(taskId, tools, { wait: false })",
            "api.rejectTask(taskId)",
            "api.resumeTask(taskId)",
            "api.abortTask(taskId)",
        ):
            assert call in source, call

    def test_卡片处理器不再读全局任务ID(self) -> None:
        source = _read("frontend/js/app.js")
        for name in ("handleApprovePending", "handleReject", "handleResume", "handleAbort"):
            block = _function_body(source, name)
            assert "const taskId = cardTaskId(card);" in block, name
            assert "currentTaskId" not in block, f"{name} 仍依赖全局 currentTaskId"

    def test_状态标签仅当前任务可更新(self) -> None:
        source = _read("frontend/js/app.js")
        for name in ("handleApprovePending", "handleReject", "handleResume", "handleAbort"):
            block = _function_body(source, name)
            assert "updateTaskStatus(" not in block, f"{name} 直接改顶栏状态标签"
            # 允许直接 syncTaskStatus，或走统一的 applyRunResult（内部同样按 task_id 收敛）。
            assert (
                "syncTaskStatus(taskId, task.status)" in block or "applyRunResult(task)" in block
            ), name
        # applyRunResult 必须按任务 ID 收敛，不得无条件改当前状态标签。
        run_block = _function_body(source, "applyRunResult")
        assert "syncTaskStatus(task.task_id, task.status)" in run_block

    def test_提醒卡绑定任务ID(self) -> None:
        source = _read("frontend/js/app.js")
        # 到点暂停的提醒卡与手动中断卡都挂 data.taskId，供 cardTaskId 读取。
        assert "function buildReminderCard(pending, taskId)" in source
        assert "function buildInterruptCard(taskId)" in source
        assert "card.dataset.taskId = taskId || currentTaskId || '';" in source


class TestSendConcurrencyGuard:
    """请求在途时不得再次建任务。"""

    def test_存在在途标志与提前返回(self) -> None:
        source = _read("frontend/js/app.js")
        assert "let isSending = false;" in source
        assert "if (isSending) return;" in source

    def test_在途时禁用发送按钮并在结束恢复(self) -> None:
        source = _read("frontend/js/app.js")
        body = _function_body(source, "handleSend")
        assert "sendBtn.disabled = true" in body
        assert "finally" in body
        assert "sendBtn.disabled = false" in body


class TestSwitchDoesNotStopTask:
    """切换任务 / 新任务 / 切项目**不得**改变后端任务状态（实测 2026-10-11）。

    此前这三处都会先调 ``stopActiveTask()`` → ``POST /tasks/{id}/abort`` → **FAILED**：
    用户**只是点开另一个任务回看**，手上正在跑的任务就被废掉了。
    保护意图（"不留孤儿任务"）改为由这三条承接：① 任务列表能看到所有任务、状态如实显示；
    ② 需要停就用**可恢复的**「中断」；③ 切项目换工作区前必须**确认**。
    """

    def test_三个入口都不再终止任务(self) -> None:
        source = _read("frontend/js/app.js")
        for name in ("switchToTask", "startNewTask"):
            block = _function_body(source, name)
            assert "abortTask" not in block, f"{name} 不该放弃任务"
            assert "stopActiveTask" not in block, f"{name} 不该调用旧的终止助手"
            assert "interruptActiveTask" not in block, f"{name} 不该顺手中断任务"

    def test_中断助手用可恢复的中断而非放弃(self) -> None:
        block = _function_body(_read("frontend/js/app.js"), "interruptActiveTask")

        assert "api.interruptTask(" in block
        assert "api.abortTask(" not in block

    def test_切项目对运行中任务先确认再中断(self) -> None:
        """换工作区会让正在跑的任务写到别处 —— 必须拦住，且不静默 abort。"""
        block = _function_body(_read("frontend/js/app.js"), "enterProjectWorkspace")

        assert "currentTaskRunning" in block
        assert "openConfirmDialog(" in block
        assert "abortTask" not in block
        assert block.index("openConfirmDialog(") < block.index("interruptActiveTask(")

    def test_切换后仍会恢复执行轮询(self) -> None:
        """回到一个仍在后台执行的任务时，进度必须继续刷新（否则等于看不见了）。"""
        block = _function_body(_read("frontend/js/app.js"), "switchToTask")

        assert "startTaskRunPoller(" in block

    def test_新任务文案不再宣称终止当前任务(self) -> None:
        assert "终止当前任务" not in _read("frontend/index.html")

    def test_清空界面助手语义不变(self) -> None:
        block = _function_body(_read("frontend/js/app.js"), "clearChatUI")

        assert "innerHTML = ''" in block


class TestSessionCapAndPersistence:
    """会话条目有上限，刷新后可恢复；多任务会话并存可回看。"""

    def test_存在上限常量与裁剪入口(self) -> None:
        source = _read("frontend/js/app.js")
        assert "const MAX_CHAT_ITEMS = 200;" in source
        assert "function trimChatArea()" in source
        assert "function recordEntry(entry)" in source
        # 追加消息/工具卡片都会写记录并持久化。
        assert "recordEntry({ kind: 'msg', role, text });" in source
        assert "kind: 'tool'" in source

    def test_裁剪提示样式存在(self) -> None:
        assert ".chat-trimmed" in _read("frontend/styles.css")

    def test_会话持久化走sessionStorage(self) -> None:
        source = _read("frontend/js/app.js")
        assert "const SESSION_KEY = 'agent-builder:session';" in source
        assert "sessionStorage.setItem(SESSION_KEY" in source
        assert "sessionStorage.getItem(SESSION_KEY)" in source
        # 不落长期存储（API 密钥与会话均不写 localStorage）。
        assert "localStorage.setItem" not in source
        assert "localStorage.getItem" not in source

    def test_多任务会话并存可回看(self) -> None:
        source = _read("frontend/js/app.js")
        # 每个任务一份会话记录（entries + plan），切换前先快照，供切回时重建。
        assert "let taskSessions = {};" in source
        assert "function snapshotCurrentTask()" in source
        assert "taskSessions[currentTaskId]" in source
        assert "function switchToTask(taskId)" in source
        assert "function renderEntry(entry)" in source

    def test_刷新后恢复会话与任务(self) -> None:
        source = _read("frontend/js/app.js")
        assert "async function restoreSession()" in source
        assert "await restoreSession();" in source
        assert "api.getTask(currentTaskId)" in source
        assert "function reopenTaskEntry(task)" in source

    def test_切换工作区清空会话(self) -> None:
        source = _read("frontend/js/app.js")
        assert "function clearSession()" in source
        assert "clearSession();" in source


def _function_body(source: str, name: str) -> str:
    """粗切函数体：从 `function <name>` 到下一个顶层 `}` 之后。"""
    start = source.index(f"function {name}(")
    match = re.search(r"\n}\n", source[start:])
    end = start + (match.end() if match else len(source) - start)
    return source[start:end]
