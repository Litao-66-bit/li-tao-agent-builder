"""HTTP API 端点测试 —— 用 TestClient，不依赖真实网络。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.security import CLIENT_HEADER_NAME, CLIENT_HEADER_VALUE


@pytest.fixture(autouse=True)
def _reset_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试前重置单例 store，并把**项目注册表隔离到 tmp**。

    卡 41 之后任务收尾会往"当前工作区"写项目记忆 —— 若注册表共用真实文件，
    测试就会污染用户真实的工作区注册表（并互相串味）。
    """
    monkeypatch.setenv("AGENT_BUILDER_PROJECTS_FILE", str(tmp_path / "projects.json"))
    reset_store()
    reset_project_store()

LOCAL_HEADERS = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}


def _client_on(workspace: Path) -> TestClient:
    """工作区切到 ``workspace`` 的测试客户端。

    卡 41 之后**后端**会用工作区项目日志自动注入上文 —— 不隔离工作区就会读到
    别的用例遗留的项目记忆（实测：跨用例串味导致断言读到 "test"/"demo" 等无关需求）。
    """
    client = TestClient(create_app())
    resp = client.post("/projects", json={"path": str(workspace)}, headers=LOCAL_HEADERS)
    assert resp.status_code == 200, resp.text
    return client


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


class TestTaskContextContinuation:
    """卡 36：任务续接 —— 新建任务时可以带上「上文」（上一任务的需求 / 结论 / 产出）。

    实测（2026-10-11）：每次发消息都新建任务、且新任务只有这一句话，于是
    「先做后端 → 再做前端」必然断链（用户原话：它没有记忆，前后端没法连上）。
    """

    def test_上文落库并回显(self, tmp_path: Path) -> None:
        client = _client_on(tmp_path)
        resp = client.post(
            "/tasks",
            json={
                "requirement": "接着做前端",
                "context": "上一轮需求：先做后端；已产出文件：src/agent.py",
                "parent_task_id": "abc12345",
            },
        )

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["parent_task_id"] == "abc12345"
        assert body["context_chars"] > 0
        # 回看也要能拿到（前端据此知道"这是接着谁说的"）
        detail = client.get(f"/tasks/{body['task_id']}").json()
        assert detail["parent_task_id"] == "abc12345"

    def test_空工作区没传上下文时行为不变(self, tmp_path: Path) -> None:
        client = _client_on(tmp_path)

        resp = client.post("/tasks", json={"requirement": "独立需求"})

        assert resp.status_code == 201
        assert resp.json()["parent_task_id"] == ""
        assert resp.json()["context_chars"] == 0, "空工作区没有历史可带"

    def test_上文超长被截断并注明(self, tmp_path: Path) -> None:
        from agent_builder.api.deps import get_store
        from agent_builder.api.routes import MAX_CONTEXT_CHARS

        client = _client_on(tmp_path)
        resp = client.post(
            "/tasks",
            json={"requirement": "x", "context": "上" * (MAX_CONTEXT_CHARS + 500)},
        )

        assert resp.status_code == 201
        entry = get_store().get(resp.json()["task_id"])
        assert entry is not None
        assert entry.context_note.startswith("上")
        assert "已截断" in entry.context_note, "超长必须注明，不能无声丢弃"

    def test_空白上下文归一为空串(self, tmp_path: Path) -> None:
        from agent_builder.api.deps import get_store

        client = _client_on(tmp_path)
        resp = client.post(
            "/tasks",
            json={"requirement": "x", "context": "   ", "parent_task_id": "  "},
        )

        entry = get_store().get(resp.json()["task_id"])
        assert entry is not None
        assert entry.context_note == ""
        assert entry.parent_task_id is None

class TestAbortAndInterruptClearPendingApproval:
    """卡 37 收尾：放弃 / 中断之后不该再挂着「待放行」（实测 2026-10-11：用户真实任务是
    ``failed`` + ``has_pending_approval=True``，列表上挂着一个永远点不了的标记）。"""

    def test_放弃后不再有待放行(self) -> None:
        from agent_builder.api.deps import get_store

        client = TestClient(create_app())
        resp = client.post("/tasks", json={"requirement": "r"})
        task_id = resp.json()["task_id"]
        entry = get_store().get(task_id)
        assert entry is not None
        entry.pending_approval = {"tools": ["file_write"], "steps": ["loop-002"]}

        body = client.post(f"/tasks/{task_id}/abort").json()

        assert body["status"] == "failed"
        assert body["pending_approval"] is None
        assert get_store().get(task_id).pending_approval is None

    def test_中断后不再有待放行(self) -> None:
        from agent_builder.api.deps import get_store

        client = TestClient(create_app())
        resp = client.post("/tasks", json={"requirement": "r"})
        task_id = resp.json()["task_id"]
        client.post(f"/tasks/{task_id}/plan", json={"use_llm": False})
        entry = get_store().get(task_id)
        assert entry is not None
        entry.conductor.handle_plan_accepted()  # AWAITING_CONFIRM → EXECUTING
        entry.pending_approval = {"tools": ["file_write"], "steps": ["loop-002"]}

        body = client.post(
            f"/tasks/{task_id}/interrupt", json={"reason": "user_stop"}
        ).json()

        assert body["pending_approval"] is None


class TestBackendOwnedMemory:
    """卡 41：**后端**要为"记忆"负责 —— 工作区级项目日志自动写入、新任务自动注入。

    实测（2026-10-11，用户第二次反馈）：前端拼的上文在刷新 / 切项目 / 后端重启后即失效；
    真正的记忆必须落在**工作区**里、由后端维护。
    """

    def test_任务收尾写进工作区项目日志(self, tmp_path: Path) -> None:
        from agent_builder.api.project_log import read_project_log

        client = _client_on(tmp_path)
        task_id = client.post("/tasks", json={"requirement": "先做个后端"}).json()["task_id"]

        client.post(f"/tasks/{task_id}/abort")

        rows = read_project_log(tmp_path)
        assert [row.task_id for row in rows] == [task_id]
        assert rows[0].requirement == "先做个后端"

    def test_新任务自动带上项目历史(self, tmp_path: Path) -> None:
        from agent_builder.api.project_log import ProjectLogEntry, record_project_log

        client = _client_on(tmp_path)
        record_project_log(
            tmp_path,
            ProjectLogEntry(
                task_id="t-old",
                requirement="先做个后端",
                status="delivered",
                conclusion="后端已完成",
                artifacts=["outputs/api.py"],
            ),
        )

        body = client.post("/tasks", json={"requirement": "接着做前端"}).json()

        assert body["context_chars"] > 0, "后端必须自动带上本项目的历史"
        entry = get_store().get(body["task_id"])
        assert entry is not None
        assert "先做个后端" in entry.context_note
        assert "outputs/api.py" in entry.context_note

    def test_空工作区时前端传的上文兜底(self, tmp_path: Path) -> None:
        client = _client_on(tmp_path)

        body = client.post(
            "/tasks", json={"requirement": "A", "context": "前端自己拼的上文"}
        ).json()

        entry = get_store().get(body["task_id"])
        assert entry is not None
        assert entry.context_note == "前端自己拼的上文"

    def test_有历史时后端记忆优先于前端(self, tmp_path: Path) -> None:
        from agent_builder.api.project_log import ProjectLogEntry, record_project_log

        client = _client_on(tmp_path)
        record_project_log(
            tmp_path,
            ProjectLogEntry(
                task_id="t-old",
                requirement="历史需求",
                status="delivered",
                conclusion="历史结论",
            ),
        )

        body = client.post(
            "/tasks", json={"requirement": "B", "context": "前端自己拼的上文"}
        ).json()

        entry = get_store().get(body["task_id"])
        assert entry is not None
        assert "历史需求" in entry.context_note
        assert "前端自己拼的上文" not in entry.context_note

    def test_记忆按工作区隔离(self, tmp_path: Path) -> None:
        from agent_builder.api.project_log import ProjectLogEntry, record_project_log

        ws_a = tmp_path / "ws-a"
        ws_a.mkdir()
        client_a = _client_on(ws_a)
        record_project_log(
            ws_a,
            ProjectLogEntry(
                task_id="t-a", requirement="A 工作区的活", status="delivered", conclusion="A 完成"
            ),
        )
        assert client_a.post("/tasks", json={"requirement": "A2"}).json()["context_chars"] > 0

        ws_b = tmp_path / "ws-b"
        ws_b.mkdir()
        client_b = _client_on(ws_b)

        assert client_b.post("/tasks", json={"requirement": "B"}).json()["context_chars"] == 0
