"""高风险审批门端到端：`/plan` → `/approve` 的逐工具授权通道。

背景：此前 `Approval.required` 在生产路径上从未被置真，`high_risk_tools` 形同虚设
（写/删除类步骤无审批即执行）。本文件守住新契约：

- 高风险步骤（**删除 / 提交 / 回滚**）一律 `required=True`，**只有被显式授权**才带
  `granted_by` 通过门卫；未授权 → 门卫拒绝（E_PERMISSION，不重试）；
- `/approve` 省略 `approved_tools` = 不授权任何高风险工具（安全默认）；
- `/plan` 返回 `high_risk_actions`，供前端提示并选择要授权的工具；
- **卡 44**：工作区内的 `file_write` / `file_edit` **不是**审批项（写产出文件是本职），
  工作区**外**的写由路径沙箱硬拒绝 —— 这两条都有用例钉住，安全边界没有放松。
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


def _seed_step_plan(client: TestClient, task_id: str, step: Step) -> None:
    """把一个步骤注入任务（替换分解器产出的计划）。

    ``target`` 需为**工作区内绝对路径**（沙箱按 resolve() 判定，相对路径会按当前
    工作区解析，而工作区就是 ``tmp_path``）。
    """
    client.post(f"/tasks/{task_id}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
    entry = get_store().get(task_id)
    assert entry is not None
    steps = {step.id: step}
    plan = Plan(
        task_id=task_id,
        order=[step.id],
        parallel_groups=[[step.id]],
        confirmed_by_user=False,
    )
    plan.validate_steps(steps)
    entry.steps = steps
    entry.conductor.task_state.plan = plan


def _seed_write_plan(client: TestClient, task_id: str, target: str) -> None:
    """工作区内的 file_write 步骤（卡 44 后**不需**放行）。"""
    _seed_step_plan(
        client,
        task_id,
        Step(
            id="step-001",
            action="file_write",
            inputs={"path": target, "content": "hello", "overwrite": True},
        ),
    )


class TestApprovalGate:
    def test_未授权时高风险步骤被门卫拒绝(self, client: TestClient, tmp_path):
        """未授权的高风险动作（这里用 file_delete）走 /approve 时被门卫拒绝。"""
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me", encoding="utf-8")
        tid = client.post(
            "/tasks", json={"requirement": "删文件"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        _seed_step_plan(
            client,
            tid,
            Step(id="step-001", action="file_delete", inputs={"path": str(victim)}),
        )

        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        row = resp.json()["execution_results"][0]
        assert row["status"] == "failed"
        assert row["retries"] == 0  # E_PERMISSION 不可重试
        assert "高风险" in row["error"]
        assert victim.exists(), "未授权 → 删除不得发生"

    def test_显式授权后放行(self, client: TestClient, tmp_path):
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me", encoding="utf-8")
        tid = client.post(
            "/tasks", json={"requirement": "删文件"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        _seed_step_plan(
            client,
            tid,
            Step(id="step-001", action="file_delete", inputs={"path": str(victim)}),
        )

        resp = client.post(
            f"/tasks/{tid}/approve",
            json={"approved_tools": ["file_delete"]},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        row = resp.json()["execution_results"][0]
        assert row["status"] == "done"
        assert not victim.exists()

    def test_工作区内写文件无需授权(self, client: TestClient, tmp_path):
        """卡 44：写产出文件不再是审批项 —— 不授权也照写不误。"""
        tid = client.post(
            "/tasks", json={"requirement": "写文件"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        _seed_write_plan(client, tid, str(tmp_path / "out.txt"))

        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        row = resp.json()["execution_results"][0]
        assert row["status"] == "done"
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"

    def test_越界写仍被沙箱拒绝(self, client: TestClient, tmp_path):
        """安全边界：工作区**外**的写即便显式"授权"也执行不了（沙箱硬拒）。"""
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        tid = client.post(
            "/tasks", json={"requirement": "越界写"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        _seed_write_plan(client, tid, str(outside))

        resp = client.post(
            f"/tasks/{tid}/approve",
            json={"approved_tools": ["file_write"]},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        row = resp.json()["execution_results"][0]
        assert row["status"] == "failed"
        assert "沙箱" in row["error"] or "超出" in row["error"]
        assert not outside.exists()

    def test_非高风险步骤无需授权(self, client: TestClient):
        tid = client.post(
            "/tasks", json={"requirement": "预览差异"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        _seed_step_plan(
            client,
            tid,
            Step(
                id="step-001",
                action="diff_preview",
                inputs={"old_content": "a", "new_content": "b"},
            ),
        )

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

    def test_高风险动作助手只挑需审批的(self, tmp_path):
        """两种口径都要正确：默认**保守**（写类一律算待放行），按工作区判定则排除工作区内写。"""
        from agent_builder.api.routes import _high_risk_actions

        steps = {
            "a": Step(id="a", action="file_write", inputs={"path": str(tmp_path / "a.txt")}),
            "b": Step(id="b", action="file_delete", inputs={"path": str(tmp_path / "a.txt")}),
            "c": Step(id="c", action="file_list", inputs={"path": str(tmp_path)}),
        }
        # 默认（保守）：写类仍算待放行 —— 不区分落点，绝不漏标。
        assert _high_risk_actions(steps) == ["file_delete", "file_write"]
        # 按工作区判定：工作区内的写不再算高风险，只剩不可逆动作。
        assert _high_risk_actions(
            steps, workspace=tmp_path, count_in_workspace_writes=False
        ) == ["file_delete"]

    def test_展示口径不把工作区内写文件标成高风险(self, tmp_path):
        """前端"高风险标记"不该在用户只是写个工作区文件时吓唬他。"""
        from agent_builder.api.routes import _high_risk_actions

        inside = tmp_path / "note.txt"
        steps = {
            "a": Step(id="a", action="file_write", inputs={"path": str(inside)}),
            "b": Step(id="b", action="file_delete", inputs={"path": str(inside)}),
        }
        assert _high_risk_actions(
            steps, workspace=tmp_path, count_in_workspace_writes=False
        ) == ["file_delete"]

    def test_越界写在展示口径下仍标成高风险(self, tmp_path):
        from agent_builder.api.routes import _high_risk_actions

        steps = {
            "a": Step(
                id="a", action="file_write", inputs={"path": str(tmp_path.parent / "evil.txt")}
            ),
        }
        assert _high_risk_actions(
            steps, workspace=tmp_path, count_in_workspace_writes=False
        ) == ["file_write"]
