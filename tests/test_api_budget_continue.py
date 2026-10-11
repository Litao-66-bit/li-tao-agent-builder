"""步数预算用满后**由用户决定**要不要再来一轮（而不是直接判死）。

用户反馈原话："他的 20 轮预算用完了，他说就直接放弃任务了，我觉得应该让用户去决定
是否再跑 20 轮，嗯，去做个循环"。

守住四件事：

1. 预算用满时循环**如实停下**（``stopped_reason=budget_steps``），不假装收敛；
2. ``POST /tasks/{id}/continue`` 让**用户**把预算上浮并继续（默认 +20 步）；
3. 只有**真的预算停下**才能续 —— 空转 / 决策不可解析停下要给 409 + 可读原因
   （那两种是"模型没招了"，多给步数只是烧钱）；
4. 续跑有**上限**（``MAX_BUDGET_STEPS_LIMIT``），到顶如实拒绝并给下一步。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import routes as routes_module
from agent_builder.api.agent_loop import (
    BUDGET_STEP_INCREMENT,
    MAX_BUDGET_STEPS_LIMIT,
    MAX_LOOP_STEPS,
    STOP_BUDGET_STEPS,
    STOP_STAGNANT,
    LoopBudget,
)
from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.secrets import reset_api_key_store

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


class _FakeLLM:
    """假 LLM：依次吐出决策 payload；用尽后给 final（不联网）。"""

    def __init__(self, decisions: list[Any] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, schema_hint: str = "") -> Any:
        self.prompts.append(prompt)
        return self.decisions.pop(0) if self.decisions else {"kind": "final"}

    def chat(self, messages, **kwargs):
        return "1. 一步一步来\n验证：文件清单"


def _diff_step(n: int) -> dict[str, Any]:
    """一个必然成功的只读步骤（用来"烧"预算）。"""
    return {
        "thought": f"第 {n} 步：预览差异",
        "next": {
            "kind": "agent",
            "role": "operator",
            "action": "diff_preview",
            "inputs": {"old_content": f"a{n}", "new_content": f"b{n}"},
        },
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """带可用密钥的隔离客户端（agentic 循环要真跑起来）。"""
    monkeypatch.setenv("AGENT_BUILDER_PROJECTS_FILE", str(tmp_path / "projects.json"))
    reset_project_store()
    reset_store()
    reset_api_key_store()
    c = TestClient(create_app())
    resp = c.post("/projects", json={"path": str(tmp_path)}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 200, resp.text
    saved = c.post(
        "/settings/api-key", json={"api_key": "sk-" + "b" * 24}, headers=_LOCAL_HEADERS
    )
    assert saved.status_code == 200, saved.text
    yield c
    reset_project_store()
    reset_store()
    reset_api_key_store()


def _task_with_budget(client: TestClient, monkeypatch, *, max_steps: int, steps: int) -> str:
    """建任务 → 把预算与脚本设好 → 返回 task_id（**不执行**）。"""
    fake = _FakeLLM([_diff_step(i) for i in range(steps)])
    monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
    tid = client.post(
        "/tasks", json={"requirement": "慢慢做"}, headers=_LOCAL_HEADERS
    ).json()["task_id"]
    client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
    entry = get_store().get(tid)
    assert entry is not None
    entry.options["max_steps"] = max_steps
    return tid


class TestBudgetExhaustionIsHonest:
    def test_预算用满时如实停下(self, client: TestClient, monkeypatch) -> None:
        """不许假装收敛：停下原因是 budget_steps。"""
        tid = _task_with_budget(client, monkeypatch, max_steps=2, steps=5)

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["stopped_reason"] == STOP_BUDGET_STEPS
        assert body["loop_state"]["budget"]["steps_used"] == 2
        assert body["loop_state"]["budget"]["max_steps"] == 2
        # 没收敛就不该被当成"做完待交付"
        assert body["status"] != "delivered"

    def test_预算进度如实回传(self, client: TestClient, monkeypatch) -> None:
        tid = _task_with_budget(client, monkeypatch, max_steps=1, steps=3)

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        budget = body["loop_state"]["budget"]
        assert budget["steps_used"] == 1
        assert budget["max_steps"] == 1


class TestContinueAfterBudget:
    def test_用户续跑后真的再多跑(self, client: TestClient, monkeypatch) -> None:
        """核心验收：续一轮 → 预算上浮 → 模型继续动手。"""
        tid = _task_with_budget(client, monkeypatch, max_steps=1, steps=5)
        first = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert first["loop_state"]["stopped_reason"] == STOP_BUDGET_STEPS
        assert len(first["execution_results"]) == 1

        body = client.post(f"/tasks/{tid}/continue", headers=_LOCAL_HEADERS).json()

        # 预算从 1 上浮到 1+20
        assert body["loop_state"]["budget"]["max_steps"] == 1 + BUDGET_STEP_INCREMENT
        # 真的又多跑了（脚本还有 4 步可用，跑到脚本用尽给 final）
        assert len(body["execution_results"]) > 1
        assert get_store().get(tid).budget_extensions == 1

    def test_续跑轮次被记账(self, client: TestClient, monkeypatch) -> None:
        tid = _task_with_budget(client, monkeypatch, max_steps=1, steps=50)

        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        client.post(f"/tasks/{tid}/continue", headers=_LOCAL_HEADERS)

        assert get_store().get(tid).budget_extensions == 1

    def test_可以指定追加步数(self, client: TestClient, monkeypatch) -> None:
        tid = _task_with_budget(client, monkeypatch, max_steps=1, steps=50)
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)

        body = client.post(
            f"/tasks/{tid}/continue",
            json={"additional_steps": 5},
            headers=_LOCAL_HEADERS,
        ).json()

        assert body["loop_state"]["budget"]["max_steps"] == 6

    def test_默认增量为20(self) -> None:
        assert BUDGET_STEP_INCREMENT == 20

    def test_默认预算仍是20步(self, client: TestClient, monkeypatch) -> None:
        """没设过 max_steps 的任务走默认值（既有行为不变）。"""
        fake = _FakeLLM([_diff_step(0)])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = client.post(
            "/tasks", json={"requirement": "看看"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["budget"]["max_steps"] == MAX_LOOP_STEPS


class TestContinueGuards:
    def test_不是预算停下时拒绝并解释(self, client: TestClient, monkeypatch) -> None:
        """空转 / 决策不可解析 → 多给步数只是烧钱，如实 409。"""
        tid = _task_with_budget(client, monkeypatch, max_steps=20, steps=1)
        entry = get_store().get(tid)
        assert entry is not None
        entry.loop_state = entry.loop_state  # 占位：真实状态由下面的手工赋值给出
        from agent_builder.api.schemas import LoopState

        entry.loop_state = LoopState(stopped_reason=STOP_STAGNANT)

        resp = client.post(f"/tasks/{tid}/continue", headers=_LOCAL_HEADERS)

        assert resp.status_code == 409
        assert "预算用满" in resp.json()["detail"]
        assert STOP_STAGNANT in resp.json()["detail"]

    def test_没有停下记录时拒绝(self, client: TestClient, monkeypatch) -> None:
        tid = _task_with_budget(client, monkeypatch, max_steps=5, steps=1)
        resp = client.post(f"/tasks/{tid}/continue", headers=_LOCAL_HEADERS)
        assert resp.status_code == 409

    def test_任务不存在返回404(self, client: TestClient) -> None:
        assert client.post("/tasks/nope/continue", headers=_LOCAL_HEADERS).status_code == 404

    def test_执行中拒绝(self, client: TestClient, monkeypatch) -> None:
        tid = _task_with_budget(client, monkeypatch, max_steps=1, steps=5)
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        entry = get_store().get(tid)
        assert entry is not None
        entry.running = True

        resp = client.post(f"/tasks/{tid}/continue", headers=_LOCAL_HEADERS)

        assert resp.status_code == 409
        assert "正在执行" in resp.json()["detail"]

    def test_超过上限时拒绝并给下一步(self, client: TestClient, monkeypatch) -> None:
        tid = _task_with_budget(client, monkeypatch, max_steps=1, steps=50)
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)

        resp = client.post(
            f"/tasks/{tid}/continue",
            json={"additional_steps": MAX_BUDGET_STEPS_LIMIT},
            headers=_LOCAL_HEADERS,
        )

        assert resp.status_code == 409
        assert "上限" in resp.json()["detail"]
        assert "放弃" in resp.json()["detail"] or "重开" in resp.json()["detail"]

    def test_连续续跑到上限后停下(self, client: TestClient, monkeypatch) -> None:
        """上限是硬的：反复续跑到顶后必须拒绝，不能无限加。"""
        tid = _task_with_budget(client, monkeypatch, max_steps=MAX_LOOP_STEPS, steps=500)
        fake = _FakeLLM([_diff_step(i) for i in range(500)])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)

        rounds = 0
        for _ in range(20):  # 每轮 +20，理论上 9 轮就该到顶
            resp = client.post(f"/tasks/{tid}/continue", headers=_LOCAL_HEADERS)
            if resp.status_code == 409:
                assert "上限" in resp.json()["detail"]
                break
            rounds += 1
        else:  # pragma: no cover - 说明上限失效
            pytest.fail("连续续跑没有在上限处停下")

        assert rounds > 0
        assert get_store().get(tid).options["max_steps"] <= MAX_BUDGET_STEPS_LIMIT


class TestPlanCanSetBudget:
    """`/plan` 可以显式给步数预算（用户想一次给足时不用点续跑）。"""

    def test_plan下发max_steps后生效(self, client: TestClient, monkeypatch) -> None:
        fake = _FakeLLM([_diff_step(0), _diff_step(1), _diff_step(2), _diff_step(3)])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = client.post(
            "/tasks", json={"requirement": "慢慢做"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        resp = client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "mode": "agentic", "max_steps": 3},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200, resp.text

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["budget"]["max_steps"] == 3
        assert body["loop_state"]["stopped_reason"] == STOP_BUDGET_STEPS

    def test_超过上限被拒(self, client: TestClient) -> None:
        tid = client.post(
            "/tasks", json={"requirement": "x"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        resp = client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "max_steps": MAX_BUDGET_STEPS_LIMIT + 1},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 422


class TestBudgetUnit:
    """`LoopBudget.max_steps` 是用户可调的上限，语义要准。"""

    def test_达到上限即判预算用尽(self) -> None:
        budget = LoopBudget(max_steps=3)
        budget.record(tokens=1, progressed=True)
        budget.record(tokens=1, progressed=True)
        assert budget.stop_reason() is None
        budget.record(tokens=1, progressed=True)
        assert budget.stop_reason() == STOP_BUDGET_STEPS

    def test_上浮上限后可以继续(self) -> None:
        budget = LoopBudget(max_steps=2)
        budget.record(tokens=1, progressed=True)
        budget.record(tokens=1, progressed=True)
        assert budget.stop_reason() == STOP_BUDGET_STEPS

        budget.max_steps += BUDGET_STEP_INCREMENT  # 用户点「继续」

        assert budget.stop_reason() is None, "续跑后必须能真的继续"
