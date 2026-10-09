"""高风险审批门端到端：`/plan` → `/approve` 的逐工具授权通道。

背景：此前 `Approval.required` 在生产路径上从未被置真，`high_risk_tools` 形同虚设
（写/删除类步骤无审批即执行）。本文件守住新契约：

- 高风险步骤（写/删除/提交/回滚）一律 `required=True`，**只有被显式授权**才带
  `granted_by` 通过门卫；未授权 → 门卫拒绝（E_PERMISSION，不重试）；
- `/approve` 省略 `approved_tools` = 不授权任何高风险工具（安全默认）；
- `/plan` 返回 `high_risk_actions`，供前端提示并选择要授权的工具。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import (
    get_store,
    reset_project_store,
    reset_store,
)
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.contracts.schemas import Plan, Step

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离的客户端：项目注册表落临时文件，并把工作区切到 tmp_path。"""
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


def _seed_write_plan(client: TestClient, task_id: str, target: str) -> None:
    """把一个 file_write 步骤注入任务（file_write 属 operator.high_risk_tools）。

    ``target`` 需为**工作区内绝对路径**（沙箱按 resolve() 判定，相对路径会以进程
    CWD 解析而与工作区不一致）。
    """
    client.post(f"/tasks/{task_id}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
    entry = get_store().get(task_id)
    assert entry is not None
    steps = {
        "step-001": Step(
            id="step-001",
            action="file_write",
            inputs={"path": target, "content": "hello", "overwrite": True},
        )
    }
    plan = Plan(
        task_id=task_id,
        order=["step-001"],
        parallel_groups=[["step-001"]],
        confirmed_by_user=False,
    )
    plan.validate_steps(steps)
    entry.steps = steps
    entry.conductor.task_state.plan = plan


class TestApprovalGate:
    def test_未授权时高风险步骤被门卫拒绝(self, client: TestClient, tmp_path):
        tid = client.post(
            "/tasks", json={"requirement": "写文件"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        _seed_write_plan(client, tid, str(tmp_path / "out.txt"))

        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        row = resp.json()["execution_results"][0]
        assert row["status"] == "failed"
        assert row["retries"] == 0  # E_PERMISSION 不可重试
        assert "高风险" in row["error"]

    def test_显式授权后放行(self, client: TestClient, tmp_path):
        tid = client.post(
            "/tasks", json={"requirement": "写文件"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        _seed_write_plan(client, tid, str(tmp_path / "out.txt"))

        resp = client.post(
            f"/tasks/{tid}/approve",
            json={"approved_tools": ["file_write"]},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        row = resp.json()["execution_results"][0]
        assert row["status"] == "done"
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"

    def test_非高风险步骤无需授权(self, client: TestClient):
        tid = client.post(
            "/tasks", json={"requirement": "预览差异"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
        entry = get_store().get(tid)
        assert entry is not None
        steps = {
            "step-001": Step(
                id="step-001",
                action="diff_preview",
                inputs={"old_content": "a", "new_content": "b"},
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

        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["execution_results"][0]["status"] == "done"


class TestHighRiskActionsField:
    def test_plan_响应含高风险动作字段(self, client: TestClient):
        tid = client.post(
            "/tasks", json={"requirement": "x"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        resp = client.post(
            f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 200
        assert resp.json()["high_risk_actions"] == []

    def test_高风险动作助手只挑需审批的(self):
        from agent_builder.api.routes import _high_risk_actions

        steps = {
            "a": Step(id="a", action="file_write"),
            "b": Step(id="b", action="file_delete"),
            "c": Step(id="c", action="file_list"),
        }
        assert _high_risk_actions(steps) == ["file_delete", "file_write"]
