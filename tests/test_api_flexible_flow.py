"""柔性流程契约：`POST /tasks/{id}/run`（默认直接跑 + 到点暂停）与 `/resume` 放行续跑。

背景：此前「确认」是**前置阻塞**——不调 `/approve` 就永不执行；而高风险步骤在未授权
时会被门卫直接判成不可重试失败（E_PERMISSION）。本轮把流程改成：

- **默认直接跑**：`/run` 一调就执行，不需要先确认；
- **到点暂停**：只有当计划里存在**尚未放行**的高风险步骤（写 / 删除 / 提交 / 回滚）
  时才挂起（`interrupted` + `pending_approval`），等用户决定；
- **放行续跑**：`/resume` 带 `approved_tools` 累积放行后继续，全部放行才真正执行。

安全边界不放松：放行仍走逐工具授权通道，`/approve` 的严格语义保持不变。
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
from agent_builder.roles.decomposer import Decomposer

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


def _new_task(client: TestClient, requirement: str) -> str:
    resp = client.post("/tasks", json={"requirement": requirement}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


def _seed_plan(client: TestClient, task_id: str, steps: dict[str, Step]) -> None:
    """把指定步骤注入任务（先走一遍 /plan 满足 AWAITING_CONFIRM 前置状态）。"""
    client.post(f"/tasks/{task_id}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
    entry = get_store().get(task_id)
    assert entry is not None
    order = list(steps.keys())
    plan = Plan(
        task_id=task_id,
        order=order,
        parallel_groups=[order],
        confirmed_by_user=False,
    )
    plan.validate_steps(steps)
    entry.steps = steps
    entry.conductor.task_state.plan = plan


def _write_plan(target: str) -> dict[str, Step]:
    """单个 file_write 步骤（属 operator.high_risk_tools，需要放行）。"""
    return {
        "step-001": Step(
            id="step-001",
            action="file_write",
            inputs={"path": target, "content": "hello", "overwrite": True},
        )
    }


def _safe_plan() -> dict[str, Step]:
    """单个非高风险步骤（无需放行，应当直接跑完）。"""
    return {
        "step-001": Step(
            id="step-001",
            action="diff_preview",
            inputs={"old_content": "a", "new_content": "b"},
        )
    }


class TestRunDirectly:
    def test_无高风险时直接跑完(self, client: TestClient, tmp_path):
        tid = _new_task(client, "预览差异")
        _seed_plan(client, tid, _safe_plan())

        resp = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pending_approval"] is None
        assert body["execution_results"][0]["status"] == "done"
        assert body["status"] == "verifying"  # 全 done 自动推进

    def test_无需先调approve(self, client: TestClient):
        """契约：不再要求前置确认——直接 /run 即可。"""
        tid = _new_task(client, "预览差异")
        _seed_plan(client, tid, _safe_plan())
        assert get_store().get(tid).conductor.task_state.status.value == "awaiting_confirm"

        resp = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["execution_results"], "应当已执行，而不是空计划"


class TestDeliver:
    """交付出口：收敛后停在 ``verifying`` 的任务必须有办法收尾。

    此前既不会自动交付、UI 也没有任何按钮 → 任务永远停在顶栏「验证中」（实机踩过）。
    """

    def test_确认交付推到delivered(self, client: TestClient):
        tid = _new_task(client, "预览差异")
        _seed_plan(client, tid, _safe_plan())
        run = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        assert run.json()["status"] == "verifying"  # 全 done 自动推进

        resp = client.post(f"/tasks/{tid}/deliver", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "delivered"
        # 回看走同一份数据 → 刷新后不丢。
        detail = client.get(f"/tasks/{tid}", headers=_LOCAL_HEADERS).json()
        assert detail["status"] == "delivered"

    def test_非verifying状态拒绝交付(self, client: TestClient):
        """只在 verifying 合法：其余状态 409（走契约里已有的两步转换）。"""
        tid = _new_task(client, "还没跑")
        resp = client.post(f"/tasks/{tid}/deliver", headers=_LOCAL_HEADERS)
        assert resp.status_code == 409

    def test_任务不存在返回404(self, client: TestClient):
        assert client.post("/tasks/nope/deliver", headers=_LOCAL_HEADERS).status_code == 404


class TestPauseOnHighRisk:
    def test_未放行时到点暂停且不执行(self, client: TestClient, tmp_path):
        target = tmp_path / "out.txt"
        tid = _new_task(client, "写文件")
        _seed_plan(client, tid, _write_plan(str(target)))

        resp = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "interrupted"  # 挂起而非失败
        assert body["pending_approval"]["tools"] == ["file_write"]
        assert body["pending_approval"]["steps"] == [
            {"step_id": "step-001", "action": "file_write"}
        ]
        assert body["execution_results"] == []  # 未执行任何步骤
        assert not target.exists()  # 文件未被写出

    def test_挂起后resume不带授权仍然挂起(self, client: TestClient, tmp_path):
        target = tmp_path / "out.txt"
        tid = _new_task(client, "写文件")
        _seed_plan(client, tid, _write_plan(str(target)))
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)

        resp = client.post(f"/tasks/{tid}/resume", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "interrupted"  # 再次挂起
        assert body["pending_approval"]["tools"] == ["file_write"]
        assert not target.exists()

    def test_放行后resume续跑并落盘(self, client: TestClient, tmp_path):
        target = tmp_path / "out.txt"
        tid = _new_task(client, "写文件")
        _seed_plan(client, tid, _write_plan(str(target)))
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)

        resp = client.post(
            f"/tasks/{tid}/resume",
            json={"approved_tools": ["file_write"]},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pending_approval"] is None
        assert body["status"] == "verifying"
        assert body["execution_results"][0]["status"] == "done"
        assert target.read_text(encoding="utf-8") == "hello"

    def test_run时直接携带授权则一次跑完(self, client: TestClient, tmp_path):
        target = tmp_path / "out.txt"
        tid = _new_task(client, "写文件")
        _seed_plan(client, tid, _write_plan(str(target)))

        resp = client.post(
            f"/tasks/{tid}/run",
            json={"approved_tools": ["file_write"]},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "verifying"
        assert target.read_text(encoding="utf-8") == "hello"

    def test_暂停后可以改计划(self, client: TestClient, tmp_path):
        """到点暂停（interrupted）后 /reject 应当放行到 PLANNING，而不是 409。"""
        target = tmp_path / "out.txt"
        tid = _new_task(client, "写文件")
        _seed_plan(client, tid, _write_plan(str(target)))
        paused = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert paused["status"] == "interrupted"

        resp = client.post(f"/tasks/{tid}/reject", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "planning"
        assert body["current_stage"] == "planning"
        # 旧计划的待放行清单与中断快照都必须清掉，否则任务列表会继续显示「待放行」。
        assert body["pending_approval"] is None
        assert body["interrupted"] is None
        rows = {row["task_id"]: row for row in client.get("/task-summaries").json()}
        assert rows[tid]["has_pending_approval"] is False
        assert not target.exists()  # 改计划全程未写文件

    def test_未暂停时改计划仍被拒(self, client: TestClient):
        """状态机边界不放宽：planning 状态下 /reject 仍是 409。"""
        tid = _new_task(client, "写文件")
        resp = client.post(f"/tasks/{tid}/reject", headers=_LOCAL_HEADERS)
        assert resp.status_code == 409

    def test_改计划后放行记录清零(self, client: TestClient, tmp_path):
        """改计划 = 作废旧计划的授权：新计划的高风险步骤必须重新逐工具放行。"""
        target = tmp_path / "out.txt"
        tid = _new_task(client, "写文件")
        _seed_plan(client, tid, _write_plan(str(target)))
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        # 放行一个「本计划用不到」的高风险工具：授权被累积，但计划仍有未放行步骤 → 继续挂起。
        resumed = client.post(
            f"/tasks/{tid}/resume",
            json={"approved_tools": ["file_delete"]},
            headers=_LOCAL_HEADERS,
        ).json()
        assert resumed["status"] == "interrupted"
        assert get_store().get(tid).approved_tools == {"file_delete"}

        # 改计划 → 放行记录清零（否则旧授权会继承到用户还没看过的新计划上）。
        resp = client.post(f"/tasks/{tid}/reject", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        assert get_store().get(tid).approved_tools == set()

        # 重新规划同一份计划 → file_write 仍需重新放行（再次「到点暂停」）。
        _seed_plan(client, tid, _write_plan(str(target)))
        again = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert again["status"] == "interrupted"
        assert again["pending_approval"]["tools"] == ["file_write"]
        assert not target.exists()


class TestResumeDoesNotReplay:
    def test_手动中断的恢复不自动重跑(self, client: TestClient):
        """用户手动中断后恢复只翻转状态，避免重复副作用（不自动重放计划）。"""
        tid = _new_task(client, "写文件")
        _seed_plan(client, tid, _safe_plan())
        # 先进入 EXECUTING（/run 会跑完并转 verifying），故手工构造中断态：
        entry = get_store().get(tid)
        conductor = entry.conductor
        conductor.handle_plan_accepted()  # AWAITING_CONFIRM → EXECUTING
        conductor.handle_interrupt("user_stop")  # EXECUTING → INTERRUPTED
        assert conductor.task_state.status.value == "interrupted"

        resp = client.post(f"/tasks/{tid}/resume", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "executing"
        assert body["execution_results"] == []  # 未自动重跑


class TestTaskSummaries:
    def test_列表含标题与状态(self, client: TestClient, tmp_path):
        tid = _new_task(client, "给会话增加交接提示\n第二行不应进标题")
        resp = client.get("/task-summaries", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        rows = {row["task_id"]: row for row in resp.json()}
        assert tid in rows
        assert rows[tid]["title"] == "给会话增加交接提示"  # 取首个非空行
        assert rows[tid]["status"] == "planning"
        assert rows[tid]["has_pending_approval"] is False

    def test_挂起任务在列表中标记(self, client: TestClient, tmp_path):
        target = tmp_path / "out.txt"
        tid = _new_task(client, "写文件")
        _seed_plan(client, tid, _write_plan(str(target)))
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)

        rows = {row["task_id"]: row for row in client.get("/task-summaries").json()}
        assert rows[tid]["has_pending_approval"] is True
        assert rows[tid]["status"] == "interrupted"

    def test_任务响应带标题(self, client: TestClient):
        tid = _new_task(client, "交付一份周报")
        body = client.get(f"/tasks/{tid}", headers=_LOCAL_HEADERS).json()
        assert body["title"] == "交付一份周报"
        assert body["pending_approval"] is None

    def test_原有tasks列表契约不变(self, client: TestClient):
        """`GET /tasks` 仍是 ID 字符串列表（新增接口与之互补，不破坏既有契约）。"""
        t1 = _new_task(client, "a")
        t2 = _new_task(client, "b")
        assert set(client.get("/tasks").json()) == {t1, t2}


class TestReopenPlanSteps:
    """回看完整性：`GET /tasks/{id}` 必须带 steps 明细（此前只有 plan.order → 只剩 step_id 占位）。"""

    def test_任务详情带步骤明细与人读标题(self, client: TestClient):
        tid = _new_task(client, "写文件")
        decomposed = Decomposer(correlation_id=tid).decompose(
            tid,
            "写文件",
            raw_steps=[
                {
                    "id": "step-001",
                    "action": "file_write",
                    "inputs": {"path": "a.md", "content": "hi"},
                }
            ],
        )
        _seed_plan(client, tid, decomposed.steps)

        body = client.get(f"/tasks/{tid}", headers=_LOCAL_HEADERS).json()
        step = body["steps"]["step-001"]
        # 回看所需的三要素：动作、输入、人读标题/描述。
        assert step["action"] == "file_write"
        assert step["inputs"] == {"path": "a.md", "content": "hi"}
        assert step["title"] == "写入文件"
        assert "文件：a.md" in step["description"]
        # plan 仍只含 order / parallel_groups（既有契约不变），明细在 steps。
        assert body["plan"]["order"] == ["step-001"]

    def test_无计划时steps为空对象(self, client: TestClient):
        tid = _new_task(client, "还没规划")
        body = client.get(f"/tasks/{tid}", headers=_LOCAL_HEADERS).json()
        assert body["steps"] == {}
        assert body["plan"] is None
