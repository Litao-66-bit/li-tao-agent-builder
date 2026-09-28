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
