"""卡 44：工作区级「已放行能力」记忆（落盘 + 跨任务 + 跨重启）。

覆盖两块：
1. ``agent_builder/api/approval_store.py`` 自身的读写与容错（纯文件，不起服务）；
2. 端到端：同一工作区的**第二个任务**不再重复问同一类工具 —— 这就是卡 44
   验收标准 4「放行记录在工作区里持久化：重启后端 + 新任务，不再重复问同一类工具」。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.approval_store import (
    approval_path,
    grant_workspace_approvals,
    read_workspace_approvals,
    revoke_workspace_approvals,
)
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.contracts.schemas import Plan, Step

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离客户端：项目注册表落临时文件，工作区切到 tmp_path。"""
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


def _delete_task(client: TestClient, tmp_path, name: str) -> tuple[str, object]:
    """建一个「删文件」任务（不可逆动作 → 需放行），返回 (task_id, 目标路径)。"""
    target = tmp_path / name
    target.write_text("keep me", encoding="utf-8")
    tid = client.post(
        "/tasks", json={"requirement": "删文件"}, headers=_LOCAL_HEADERS
    ).json()["task_id"]
    client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
    entry = get_store().get(tid)
    assert entry is not None
    steps = {"step-001": Step(id="step-001", action="file_delete", inputs={"path": str(target)})}
    plan = Plan(
        task_id=tid,
        order=["step-001"],
        parallel_groups=[["step-001"]],
        confirmed_by_user=False,
    )
    plan.validate_steps(steps)
    entry.steps = steps
    entry.conductor.task_state.plan = plan
    return tid, target


def _grant_delete_and_pause(client: TestClient, tid: str) -> dict:
    """走到「已放行 file_delete 但任务仍挂起（还差 git_commit）」这个状态。

    为什么要绕这一下：放行**只在任务因高风险挂起时**才被接受，所以想验证"放行被落盘"
    就必须让任务仍处于挂起态 —— 先放行 git_commit，让 file_delete 成为下一个待放行项。
    """
    client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
    body = client.post(
        f"/tasks/{tid}/resume",
        json={"approved_tools": ["git_commit"]},
        headers=_LOCAL_HEADERS,
    ).json()
    assert body["status"] == "interrupted"
    return body


class TestApprovalStoreFile:
    def test_未写过时是空集(self, tmp_path):
        assert read_workspace_approvals(tmp_path) == set()

    def test_写入后可读回且按工作区隔离(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        granted = grant_workspace_approvals(tmp_path, {"file_delete", "git_commit"})
        assert granted == {"file_delete", "git_commit"}
        assert read_workspace_approvals(tmp_path) == {"file_delete", "git_commit"}
        assert read_workspace_approvals(other) == set(), "授权按工作区隔离，不得串味"

    def test_累积而不是覆盖(self, tmp_path):
        grant_workspace_approvals(tmp_path, {"file_delete"})
        grant_workspace_approvals(tmp_path, {"rollback"})
        assert read_workspace_approvals(tmp_path) == {"file_delete", "rollback"}

    def test_落点是工作区内的隐藏目录(self, tmp_path):
        grant_workspace_approvals(tmp_path, {"file_delete"})
        path = approval_path(tmp_path)
        assert path.exists()
        assert path.parent.name == ".agent-memory", "与项目日志同目录（已 gitignore）"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload == {"approved_tools": ["file_delete"]}

    def test_读坏的文件不致命(self, tmp_path):
        path = approval_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        for body in ("{不是 JSON", "[]", "null", '{"approved_tools": "file_delete"}'):
            path.write_text(body, encoding="utf-8")
            assert read_workspace_approvals(tmp_path) == set(), body

    def test_非字符串条目被过滤(self, tmp_path):
        path = approval_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"approved_tools": ["file_delete", 7, None, "", "  ", "x" * 200]}),
            encoding="utf-8",
        )
        assert read_workspace_approvals(tmp_path) == {"file_delete"}

    def test_撤销后为空(self, tmp_path):
        grant_workspace_approvals(tmp_path, {"file_delete"})
        revoke_workspace_approvals(tmp_path)
        assert read_workspace_approvals(tmp_path) == set()
        revoke_workspace_approvals(tmp_path)  # 幂等：文件不存在也不抛


class TestWorkspaceApprovalsEndToEnd:
    def test_放行后同一工作区的新任务不再重复问(self, client: TestClient, tmp_path):
        """卡 44 验收 4 的核心：跨任务记住放行，不再逐任务重批。"""
        tid, victim = _delete_task(client, tmp_path, "victim.txt")
        paused = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert paused["pending_approval"]["tools"] == ["file_delete"]

        resumed = client.post(
            f"/tasks/{tid}/resume",
            json={"approved_tools": ["file_delete"]},
            headers=_LOCAL_HEADERS,
        ).json()
        assert resumed["status"] == "verifying"
        assert not victim.exists()
        assert read_workspace_approvals(tmp_path) == {"file_delete"}

        # 第二个任务（同工作区）：同一类工具**不再挂起**，直接执行。
        tid2, victim2 = _delete_task(client, tmp_path, "victim2.txt")
        again = client.post(f"/tasks/{tid2}/run", headers=_LOCAL_HEADERS).json()
        assert again["pending_approval"] is None, "已放行的能力不该逐任务重批"
        assert again["status"] == "verifying"
        assert not victim2.exists()

    def test_新任务创建时即带上工作区已放行能力(self, client: TestClient, tmp_path):
        grant_workspace_approvals(tmp_path, {"file_delete"})
        tid, _victim = _delete_task(client, tmp_path, "victim3.txt")
        run = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert run["pending_approval"] is None

    def test_改计划会连工作区级授权一起撤销(self, client: TestClient, tmp_path):
        """否则新计划一跑就把旧授权读回来，"清零"只是内存里做样子。"""
        tid, _victim = _delete_task(client, tmp_path, "victim4.txt")
        _grant_delete_and_pause(client, tid)
        assert read_workspace_approvals(tmp_path) == {"git_commit"}

        assert client.post(f"/tasks/{tid}/reject", headers=_LOCAL_HEADERS).status_code == 200
        assert read_workspace_approvals(tmp_path) == set()
        assert get_store().get(tid).approved_tools == set()

    def test_工作区内写文件始终不产生放行记录(self, client: TestClient, tmp_path):
        """卡 44 的另一面：写产出文件根本不该走到放行这条路。"""
        target = tmp_path / "note.txt"
        tid = client.post(
            "/tasks", json={"requirement": "写文件"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
        entry = get_store().get(tid)
        assert entry is not None
        steps = {
            "step-001": Step(
                id="step-001",
                action="file_write",
                inputs={"path": str(target), "content": "hello", "overwrite": True},
            )
        }
        plan = Plan(
            task_id=tid,
            order=["step-001"],
            parallel_groups=[["step-001"]],
            confirmed_by_user=False,
        )
        plan.validate_steps(steps)
        entry.steps = steps
        entry.conductor.task_state.plan = plan

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert body["pending_approval"] is None
        assert target.read_text(encoding="utf-8") == "hello"
        assert read_workspace_approvals(tmp_path) == set()
