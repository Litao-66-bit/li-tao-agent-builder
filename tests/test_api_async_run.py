"""非阻塞执行（``wait=false``）：长任务期间前端不再整屏冻结。

实测（2026-10-10，真实浏览器 + 真实后端）：
- 首次 ``/run``：6s~18s 期间顶栏「执行中」、工具卡 **0** 张、对话区只有 79 字符；
- 点「放行并继续」后的 ``/resume``：50s~72s 共 **22 秒** 字符数一动不动（368）；
- 直到请求返回，13 张工具卡与结论**一次性**冒出来。

根因：``/run`` / ``/resume`` 同步阻塞到整段循环跑完才返回，而循环本身逐轮更新
（``GET /tasks/{id}`` 在请求进行中就能看到 ``loop_state.rounds`` 增长）。
修法：``wait=false`` 立刻返回并把执行放到后台线程，前端轮询着边跑边渲染。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.contracts.schemas import Plan, Step

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUILDER_PROJECTS_FILE", str(tmp_path / "projects.json"))
    reset_project_store()
    reset_store()
    reset_api_key_store()
    c = TestClient(create_app())
    resp = c.post("/projects", json={"path": str(tmp_path)}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 200, resp.text
    yield c
    reset_project_store()
    reset_store()
    reset_api_key_store()


def _task_ready_to_run(client: TestClient) -> str:
    """建任务并注入一个**非高风险**步骤：让它真的进执行，而不是「到点暂停」。

    （高风险步骤会被 /run 拦成挂起，那样就测不到「后台执行」这条路径了。）
    """
    resp = client.post("/tasks", json={"requirement": "非阻塞执行契约"}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]
    plan_resp = client.post(
        f"/tasks/{task_id}/plan",
        json={"mode": "workflow", "use_llm": False},
        headers=_LOCAL_HEADERS,
    )
    assert plan_resp.status_code == 200, plan_resp.text
    entry = get_store().get(task_id)
    assert entry is not None
    steps = {
        "step-001": Step(
            id="step-001",
            action="diff_preview",
            inputs={"old_content": "a", "new_content": "b"},
        )
    }
    order = ["step-001"]
    plan = Plan(
        task_id=task_id,
        order=order,
        parallel_groups=[order],
        confirmed_by_user=False,
    )
    plan.validate_steps(steps)
    entry.steps = steps
    entry.conductor.task_state.plan = plan
    return task_id


def _wait_until_idle(client: TestClient, task_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"/tasks/{task_id}", headers=_LOCAL_HEADERS).json()
        if not body["running"]:
            return body
        time.sleep(0.02)
    raise AssertionError(f"后台执行没有在 {timeout}s 内结束：running={body.get('running')}")


class TestNonBlockingRun:
    def test_wait_false_立刻返回并如实报running(self, client, monkeypatch):
        from agent_builder.api import routes

        task_id = _task_ready_to_run(client)

        release = threading.Event()

        def _slow_execute(entry, tid, approved_tools):
            # 模拟「长任务」：一直不放行，直到测试主动 release。
            assert release.wait(timeout=5), "测试没有放行后台执行"
            entry.execution_results = [
                {
                    "step_id": "step-1",
                    "action": "file_list",
                    "status": "done",
                    "executor": "operator",
                    "summary": "列出文件完成",
                }
            ]

        monkeypatch.setattr(routes, "_execute_plan", _slow_execute)

        t0 = time.monotonic()
        resp = client.post(f"/tasks/{task_id}/run?wait=false", headers=_LOCAL_HEADERS)
        elapsed = time.monotonic() - t0

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["running"] is True, "wait=false 必须如实告诉前端「还在跑」"
        assert body["status"] == "executing"
        assert elapsed < 1.0, f"wait=false 不该被阻塞（实测 {elapsed:.2f}s）"

        # 后台仍在跑：中间态可见（这就是前端轮询能画出进度的依据）
        mid = client.get(f"/tasks/{task_id}", headers=_LOCAL_HEADERS).json()
        assert mid["running"] is True
        assert mid["execution_results"] == []

        release.set()
        final = _wait_until_idle(client, task_id)
        assert final["running"] is False
        assert [item["step_id"] for item in final["execution_results"]] == ["step-1"]

    def test_wait_true_仍然是同步阻塞的旧行为(self, client, monkeypatch):
        """默认（不传 wait）语义不能变 —— 既有调用方与测试都依赖它。"""
        from agent_builder.api import routes

        task_id = _task_ready_to_run(client)
        seen: list[str] = []

        def _execute(entry, tid, approved_tools):
            seen.append(tid)
            entry.execution_results = []

        monkeypatch.setattr(routes, "_execute_plan", _execute)

        body = client.post(f"/tasks/{task_id}/run", headers=_LOCAL_HEADERS).json()

        assert seen == [task_id], "同步路径必须在请求内跑完"
        assert body["running"] is False

    def test_同一任务不能并发起两次后台执行(self, client, monkeypatch):
        from agent_builder.api import routes

        task_id = _task_ready_to_run(client)
        release = threading.Event()

        def _slow_execute(entry, tid, approved_tools):
            release.wait(timeout=5)

        monkeypatch.setattr(routes, "_execute_plan", _slow_execute)

        first = client.post(f"/tasks/{task_id}/run?wait=false", headers=_LOCAL_HEADERS)
        assert first.status_code == 200

        second = client.post(f"/tasks/{task_id}/run?wait=false", headers=_LOCAL_HEADERS)
        assert second.status_code == 409
        assert "正在执行中" in str(second.json()["detail"])

        release.set()
        _wait_until_idle(client, task_id)

    def test_后台线程结束会清掉running(self, client, monkeypatch):
        from agent_builder.api import routes

        task_id = _task_ready_to_run(client)

        def _boom(entry, tid, approved_tools):
            raise RuntimeError("模拟后台执行炸掉")

        monkeypatch.setattr(routes, "_execute_plan", _boom)

        client.post(f"/tasks/{task_id}/run?wait=false", headers=_LOCAL_HEADERS)

        # 关键：异常也必须把 running 复位，否则任务永远卡在「执行中」且再也不能重跑。
        final = _wait_until_idle(client, task_id)
        assert final["running"] is False
