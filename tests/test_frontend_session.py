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
            "api.resumeTask(taskId, tools)",
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


class TestStopTaskBeforeSwitch:
    """切换 / 新建任务 / 切换工作区前必须终止后端任务。"""

    def test_终止与清空助手语义(self) -> None:
        source = _read("frontend/js/app.js")
        stop_block = _function_body(source, "stopActiveTask")
        assert "await api.abortTask(taskId)" in stop_block
        clear_block = _function_body(source, "clearChatUI")
        assert "innerHTML = ''" in clear_block

    def test_三个入口先终止再清界面(self) -> None:
        source = _read("frontend/js/app.js")
        for name in ("switchToTask", "startNewTask", "enterProjectWorkspace"):
            block = _function_body(source, name)
            assert "await stopActiveTask()" in block, name
            assert "clearChatUI()" in block, name
            assert block.index("stopActiveTask") < block.index("clearChatUI"), f"{name} 应先终止后端任务再清界面"


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
