"""卡 31：agentic 也要产出**可读的执行计划**，否则界面是黑箱。

实测（2026-10-11，真实浏览器）：agentic 下 `/plan` 不跑 Decomposer、前端又只在 workflow
模式渲染计划块 → 用户在整个执行期间只看到「收到，开始执行。」加一行折叠摘要，
**看不到要做什么、分几步、每步内容**。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import reset_project_store, reset_store
from agent_builder.api.secrets import reset_api_key_store

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


class _FakeLLM:
    def __init__(self, text: str = "", boom: bool = False) -> None:
        self.text = text
        self.boom = boom
        self.prompts: list[str] = []

    def chat(self, messages):  # 测试替身
        self.prompts.append(messages[-1]["content"])
        if self.boom:
            raise RuntimeError("模拟模型不可用")
        return self.text


def _new_task(client: TestClient, requirement: str = "写一个 hello 脚本") -> str:
    resp = client.post("/tasks", json={"requirement": requirement}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


class TestTrimPlanText:
    def test_去掉代码块围栏与首尾空白(self):
        from agent_builder.api.routes import _trim_plan_text

        assert _trim_plan_text("  ```\n1. 做事\n```  ") == "1. 做事"

    def test_限长(self):
        from agent_builder.api.routes import MAX_PLAN_TEXT_CHARS, _trim_plan_text

        assert len(_trim_plan_text("x" * 9000)) == MAX_PLAN_TEXT_CHARS

    def test_空与None都是空串(self):
        from agent_builder.api.routes import _trim_plan_text

        assert _trim_plan_text(None) == ""
        assert _trim_plan_text("") == ""


class TestDraftPlanText:
    def test_没有客户端时返回空串(self):
        from agent_builder.api.routes import _draft_plan_text

        assert _draft_plan_text("需求", None) == ""

    def test_模型报错时不抛异常(self):
        from agent_builder.api.routes import _draft_plan_text

        assert _draft_plan_text("需求", _FakeLLM(boom=True)) == ""

    def test_提示词带上需求并要验证行(self):
        from agent_builder.api.routes import _draft_plan_text

        fake = _FakeLLM("1. 做事\n验证：跑测试")
        out = _draft_plan_text("写一个 hello 脚本", fake)

        assert out == "1. 做事\n验证：跑测试"
        assert "写一个 hello 脚本" in fake.prompts[0]
        assert "验证" in fake.prompts[0]


class TestPlanTextThroughApi:
    def test_agentic的plan带上可读计划且能回看(self, client, monkeypatch):
        from agent_builder.api import routes

        fake = _FakeLLM("1. 写 outputs/a.py\n2. 跑测试\n验证：pytest 全绿")
        monkeypatch.setattr(routes, "build_llm_client_or_none", lambda *a, **k: fake)
        monkeypatch.setattr(routes, "get_llm_client", lambda **k: fake)

        task_id = _new_task(client)
        resp = client.post(
            f"/tasks/{task_id}/plan",
            json={"mode": "agentic", "use_llm": True},
            headers=_LOCAL_HEADERS,
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "agentic"
        assert body["steps"] == {}, "agentic 仍不该预分解 DAG（规划去工作流化不变）"
        assert "写 outputs/a.py" in body["plan_text"]

        # 刷新 / 回看也要带（前端靠它重建计划区块）
        detail = client.get(f"/tasks/{task_id}", headers=_LOCAL_HEADERS).json()
        assert "验证：pytest 全绿" in detail["plan_text"]

    def test_模型不可用时不影响建任务与执行(self, client, monkeypatch):
        from agent_builder.api import routes

        monkeypatch.setattr(routes, "build_llm_client_or_none", lambda *a, **k: _FakeLLM(boom=True))
        monkeypatch.setattr(routes, "get_llm_client", lambda **k: _FakeLLM(boom=True))

        task_id = _new_task(client)
        resp = client.post(
            f"/tasks/{task_id}/plan",
            json={"mode": "agentic", "use_llm": True},
            headers=_LOCAL_HEADERS,
        )

        assert resp.status_code == 200, "计划文本失败不能拖垮 /plan"
        assert resp.json()["plan_text"] == ""
