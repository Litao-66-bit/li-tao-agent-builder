"""交接提示（handover hint）测试：纯函数判定 + 端点 + 本机标识头 + 无副作用。

门控：对话轮次 > 5 或 累计 token ≥ 12000 或 累计字符 ≥ 24000（任一命中即进入判定）。
判定：门控命中 + 未决项 + 任务状态综合；限次（每会话最多 N 次）+ ack 去重。
本版不调 LLM（generated_by='rule'）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import reset_store
from agent_builder.api.handover import (
    ACK_IGNORED,
    ACK_NONE,
    ACK_SEEN,
    STRENGTH_NONE,
    STRENGTH_STRONG,
    STRENGTH_SUGGEST,
    collect_open_items,
    should_suggest_handover,
    thresholds,
)
from agent_builder.api.store import TaskEntry
from agent_builder.contracts.schemas import TaskState
from agent_builder.roles.conductor import Conductor

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


def _make_entry(**overrides) -> TaskEntry:
    state = TaskState(task_id="t1")
    conductor = Conductor(task_state=state, correlation_id="t1")
    entry = TaskEntry(conductor=conductor, requirement="测试需求")
    for key, value in overrides.items():
        setattr(entry, key, value)
    return entry


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


# ── 纯函数：should_suggest_handover ────────────────────────────────────────


class TestGateConditions:
    """门控三条件任一命中即进入判定。"""

    def test_轮次命中(self):
        d = should_suggest_handover(
            turns=6, context_tokens=0, context_chars=0,
            task_status="executing", prompts_shown=0, ack_status=ACK_NONE, open_items=0,
        )
        assert d.should_suggest is True
        assert d.strength == STRENGTH_SUGGEST
        assert d.turns_hit is True

    def test_token命中(self):
        d = should_suggest_handover(
            turns=0, context_tokens=12000, context_chars=0,
            task_status="executing", prompts_shown=0, ack_status=ACK_NONE, open_items=0,
        )
        assert d.strength == STRENGTH_SUGGEST
        assert d.tokens_hit is True

    def test_字符命中(self):
        d = should_suggest_handover(
            turns=0, context_tokens=0, context_chars=24000,
            task_status="executing", prompts_shown=0, ack_status=ACK_NONE, open_items=0,
        )
        assert d.strength == STRENGTH_SUGGEST
        assert d.chars_hit is True

    def test_三门控均未命中(self):
        d = should_suggest_handover(
            turns=2, context_tokens=100, context_chars=200,
            task_status="executing", prompts_shown=0, ack_status=ACK_NONE, open_items=3,
        )
        assert d.should_suggest is False
        assert d.strength == STRENGTH_NONE


class TestStrengthByOpenItems:
    """门控命中 + 有未决项 → strong；无未决项 → suggest。"""

    def test_有未决项为strong(self):
        d = should_suggest_handover(
            turns=10, context_tokens=0, context_chars=0,
            task_status="executing", prompts_shown=0, ack_status=ACK_NONE, open_items=2,
        )
        assert d.strength == STRENGTH_STRONG
        assert d.should_suggest is True

    def test_无未决项为suggest(self):
        d = should_suggest_handover(
            turns=10, context_tokens=0, context_chars=0,
            task_status="executing", prompts_shown=0, ack_status=ACK_NONE, open_items=0,
        )
        assert d.strength == STRENGTH_SUGGEST


class TestStatusAndLimits:
    """任务状态 / 限次 / ack 去重。"""

    def test_delivered不提示(self):
        d = should_suggest_handover(
            turns=100, context_tokens=99999, context_chars=99999,
            task_status="delivered", prompts_shown=0, ack_status=ACK_NONE, open_items=5,
        )
        assert d.should_suggest is False
        assert d.strength == STRENGTH_NONE

    def test_限次耗尽不提示(self):
        d = should_suggest_handover(
            turns=10, context_tokens=0, context_chars=0,
            task_status="executing", prompts_shown=thresholds()["max_prompts"],
            ack_status=ACK_NONE, open_items=3,
        )
        assert d.should_suggest is False
        assert d.exhausted is True

    def test_ignored去重(self):
        d = should_suggest_handover(
            turns=10, context_tokens=0, context_chars=0,
            task_status="executing", prompts_shown=0, ack_status=ACK_IGNORED, open_items=3,
        )
        assert d.should_suggest is False


class TestCollectOpenItems:
    """从 TaskEntry 收集人的未决 + 机器未决。"""

    def test_pending_questions与self_check_issues都收集(self):
        entry = _make_entry(
            pending_questions=["q1", "q2"],
            self_check={"issues": [{"kind": "mismatch", "detail": []}]},
        )
        human, _machine = collect_open_items(entry)
        assert len(human) == 3
        assert human[0]["source"] == "pending_questions"
        assert human[2]["source"] == "self_check"

    def test_execution_results非done为机器未决(self):
        entry = _make_entry(
            execution_results=[
                {"step_id": "s1", "action": "web_fetch", "status": "done", "result": "ok"},
                {"step_id": "s2", "action": "file_write", "status": "failed", "error": "boom"},
                {"step_id": "s3", "action": "code_gen", "status": "pending_approval"},
            ],
        )
        human, machine = collect_open_items(entry)
        assert len(human) == 0
        assert len(machine) == 2
        assert machine[0]["step_id"] == "s2"
        assert machine[0]["status"] == "failed"

    def test_机器未决带中文动作名与人话原因(self):
        """卡片/复制文本不得直接露英文 action 与异常原文。"""
        entry = _make_entry(
            execution_results=[
                {
                    "step_id": "s1",
                    "action": "web_fetch",
                    "status": "failed",
                    "error": "RuntimeError: AgentError: web_fetch: HTTP 错误: 404 Not Found",
                },
                {"step_id": "s2", "action": "file_write", "status": "pending_approval"},
            ],
        )
        _human, machine = collect_open_items(entry)
        assert machine[0]["action"] == "web_fetch"  # 英文名保留（审计用）
        assert machine[0]["title"] == "抓取网页"  # 展示用中文名
        assert "RuntimeError" not in machine[0]["detail"]
        assert "AgentError" not in machine[0]["detail"]
        assert "HTTP 404" in machine[0]["detail"]
        assert machine[1]["title"] == "写入文件"
        assert "放行" in machine[1]["detail"]

    def test_优先用已算好的summary(self):
        entry = _make_entry(
            execution_results=[
                {
                    "step_id": "s1",
                    "action": "web_fetch",
                    "status": "failed",
                    "error": "RuntimeError: boom",
                    "summary": "抓取网页失败：目标站点不可达",
                },
            ],
        )
        _human, machine = collect_open_items(entry)
        assert machine[0]["detail"] == "抓取网页失败：目标站点不可达"

    def test_空条目不崩溃(self):
        entry = _make_entry()
        human, machine = collect_open_items(entry)
        assert human == []
        assert machine == []


# ── 端点测试 ───────────────────────────────────────────────────────────────


def _create_task(client: TestClient) -> str:
    resp = client.post("/tasks", json={"requirement": "测试需求"}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 201
    return resp.json()["task_id"]


class TestHandoverEndpoint:
    def test_未带本机标识头被拒(self, client):
        task_id = _create_task(client)
        resp = client.get(f"/tasks/{task_id}/handover?turns=10")
        assert resp.status_code in (401, 403)

    def test_任务不存在返回404(self, client):
        resp = client.get("/tasks/nope/handover", headers=_LOCAL_HEADERS)
        assert resp.status_code == 404

    def test_门控未命中返回none(self, client):
        task_id = _create_task(client)
        resp = client.get(
            f"/tasks/{task_id}/handover?turns=1&context_tokens=10&context_chars=20",
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["should_suggest"] is False
        assert data["strength"] == STRENGTH_NONE
        assert data["generated_by"] == "rule"

    def test_门控命中返回suggest(self, client):
        task_id = _create_task(client)
        resp = client.get(
            f"/tasks/{task_id}/handover?turns=10&context_tokens=0&context_chars=0",
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["should_suggest"] is True
        assert data["strength"] == STRENGTH_SUGGEST
        assert data["gate"]["turns_hit"] is True
        assert data["gate"]["prompts_shown"] == 0

    def test_ack_seen计入提示次数(self, client):
        task_id = _create_task(client)
        # 首次 ack=seen
        resp = client.post(
            f"/tasks/{task_id}/handover/ack",
            json={"status": ACK_SEEN},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["ack"]["status"] == ACK_SEEN
        # 再次查询应看到 prompts_shown=1
        resp = client.get(
            f"/tasks/{task_id}/handover?turns=10", headers=_LOCAL_HEADERS,
        )
        assert resp.json()["gate"]["prompts_shown"] == 1

    def test_ack_ignored去重(self, client):
        task_id = _create_task(client)
        client.post(
            f"/tasks/{task_id}/handover/ack",
            json={"status": ACK_IGNORED},
            headers=_LOCAL_HEADERS,
        )
        resp = client.get(
            f"/tasks/{task_id}/handover?turns=10", headers=_LOCAL_HEADERS,
        )
        data = resp.json()
        assert data["should_suggest"] is False
        assert data["ack"]["status"] == ACK_IGNORED

    def test_ack非法状态被拒(self, client):
        task_id = _create_task(client)
        resp = client.post(
            f"/tasks/{task_id}/handover/ack",
            json={"status": "bogus"},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 422

    def test_限次耗尽不提示(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_BUILDER_HANDOVER_MAX_PROMPTS", "1")
        task_id = _create_task(client)
        # 先 ack seen 一次 → prompts_shown=1 → 达上限
        client.post(
            f"/tasks/{task_id}/handover/ack",
            json={"status": ACK_SEEN},
            headers=_LOCAL_HEADERS,
        )
        resp = client.get(
            f"/tasks/{task_id}/handover?turns=10", headers=_LOCAL_HEADERS,
        )
        data = resp.json()
        assert data["should_suggest"] is False
        assert data["gate"]["exhausted"] is True
