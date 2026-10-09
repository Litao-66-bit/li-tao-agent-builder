"""HTTP API 端点测试 —— 用 TestClient，不依赖真实网络。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import reset_store


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    """每个测试前重置单例 store。"""
    reset_store()


class TestHealth:
    def test_health(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestCreateTask:
    def test_create_task_default_id(self) -> None:
        client = TestClient(create_app())
        resp = client.post("/tasks", json={"requirement": "写一个 hello world"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "planning"
        assert data["task_id"]
        assert data["current_stage"] == "planning"

    def test_create_task_custom_id(self) -> None:
        client = TestClient(create_app())
        resp = client.post("/tasks", json={"requirement": "test", "task_id": "my-task"})
        assert resp.status_code == 201
        assert resp.json()["task_id"] == "my-task"

    def test_create_task_empty_requirement(self) -> None:
        client = TestClient(create_app())
        resp = client.post("/tasks", json={"requirement": ""})
        assert resp.status_code == 400

    def test_create_duplicate_task(self) -> None:
        client = TestClient(create_app())
        client.post("/tasks", json={"requirement": "test", "task_id": "dup"})
        resp = client.post("/tasks", json={"requirement": "test", "task_id": "dup"})
        assert resp.status_code == 409


class TestGetTask:
    def test_get_task(self) -> None:
        client = TestClient(create_app())
        create = client.post("/tasks", json={"requirement": "test"})
        tid = create.json()["task_id"]
        resp = client.get(f"/tasks/{tid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "planning"

    def test_get_task_not_found(self) -> None:
        client = TestClient(create_app())
        resp = client.get("/tasks/nonexistent")
        assert resp.status_code == 404

    def test_list_tasks(self) -> None:
        client = TestClient(create_app())
        client.post("/tasks", json={"requirement": "a", "task_id": "t1"})
        client.post("/tasks", json={"requirement": "b", "task_id": "t2"})
        resp = client.get("/tasks")
        assert resp.status_code == 200
        assert set(resp.json()) == {"t1", "t2"}


class TestPlanFlow:
    def test_plan_without_llm(self) -> None:
        client = TestClient(create_app())
        create = client.post("/tasks", json={"requirement": "test"})
        tid = create.json()["task_id"]
        resp = client.post(f"/tasks/{tid}/plan", json={"use_llm": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "awaiting_confirm"
        assert data["pending_questions"]  # 无 LLM → 待确认

    def test_plan_then_approve(self) -> None:
        client = TestClient(create_app())
        create = client.post("/tasks", json={"requirement": "test"})
        tid = create.json()["task_id"]
        client.post(f"/tasks/{tid}/plan", json={"use_llm": False})
        resp = client.post(f"/tasks/{tid}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"

    def test_plan_then_reject(self) -> None:
        client = TestClient(create_app())
        create = client.post("/tasks", json={"requirement": "test"})
        tid = create.json()["task_id"]
        client.post(f"/tasks/{tid}/plan", json={"use_llm": False})
        resp = client.post(f"/tasks/{tid}/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "planning"

    def test_approve_without_plan_conflict(self) -> None:
        client = TestClient(create_app())
        create = client.post("/tasks", json={"requirement": "test"})
        tid = create.json()["task_id"]
        # PLANNING 状态直接 approve → 非法转换 → 409
        resp = client.post(f"/tasks/{tid}/approve")
        assert resp.status_code == 409


class TestInterruptResume:
    def test_interrupt_resume(self) -> None:
        client = TestClient(create_app())
        create = client.post("/tasks", json={"requirement": "test"})
        tid = create.json()["task_id"]
        client.post(f"/tasks/{tid}/plan", json={"use_llm": False})
        client.post(f"/tasks/{tid}/approve")
        # interrupt
        resp = client.post(f"/tasks/{tid}/interrupt", json={"reason": "test"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "interrupted"
        # resume
        resp = client.post(f"/tasks/{tid}/resume")
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"


class TestAbort:
    def test_abort(self) -> None:
        client = TestClient(create_app())
        create = client.post("/tasks", json={"requirement": "test"})
        tid = create.json()["task_id"]
        resp = client.post(f"/tasks/{tid}/abort")
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"


class TestDeleteTask:
    """DELETE /tasks/{id}：删除条目（不触发执行副作用；不释放项目状态）。"""

    def test_delete_existing_task(self) -> None:
        client = TestClient(create_app())
        create = client.post("/tasks", json={"requirement": "test", "task_id": "del-me"})
        assert create.status_code == 201
        resp = client.delete("/tasks/del-me")
        assert resp.status_code == 200
        assert resp.json() == {"task_id": "del-me", "deleted": True}

    def test_deleted_task_gone_from_get_and_summaries(self) -> None:
        client = TestClient(create_app())
        client.post("/tasks", json={"requirement": "a", "task_id": "keep"})
        client.post("/tasks", json={"requirement": "b", "task_id": "drop"})
        assert client.delete("/tasks/drop").status_code == 200
        # 详情 404（与 GET /tasks/{id} 一致）。
        assert client.get("/tasks/drop").status_code == 404
        # 摘要列表不再包含它；另一个任务仍在。
        summaries = client.get("/task-summaries").json()
        ids = {s["task_id"] for s in summaries}
        assert "drop" not in ids
        assert "keep" in ids
        # GET /tasks（ID 列表契约）同样不再包含。
        assert "drop" not in client.get("/tasks").json()

    def test_超长需求标题带省略号(self) -> None:
        """卡 6：此前是硬截断，列表里显示成「…请把实现代码写进工」（在词中间断掉）。"""
        client = TestClient(create_app())
        client.post(
            "/tasks",
            json={"requirement": "帮我做一个调研论文的 agent：" + "细节" * 40, "task_id": "long-title"},
        )

        title = client.get("/task-summaries").json()[0]["title"]

        assert title.endswith("…")
        assert len(title) == 61  # 60 字上限 + 省略号

    def test_delete_missing_task_returns_404(self) -> None:
        client = TestClient(create_app())
        resp = client.delete("/tasks/nonexistent")
        assert resp.status_code == 404

