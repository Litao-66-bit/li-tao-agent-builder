"""卡 46：**直连大模型**（「LLM 拆分」）—— 把大模型与工作流拆开。

验收要点：直连**不产生任何任务**、**不写工作区**、不经过门卫与状态机；
关掉开关后原有 agentic 工作流**一行不改**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.security import CLIENT_HEADER_NAME, CLIENT_HEADER_VALUE

LOCAL_HEADERS = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离任务 store 与项目注册表（避免写进用户真实注册表）。"""
    monkeypatch.setenv("AGENT_BUILDER_PROJECTS_FILE", str(tmp_path / "projects.json"))
    reset_store()
    reset_project_store()


class _FakeClient:
    """假模型客户端：记录收到的 messages（直连端点只依赖 .chat 与 .usage）。"""

    def __init__(self, reply: str = "模型回复") -> None:
        self.reply = reply
        self.seen: list[list[dict[str, str]]] = []
        self.usage = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.seen.append([dict(item) for item in messages])
        return self.reply


def _client_on(workspace: Path) -> TestClient:
    client = TestClient(create_app())
    resp = client.post("/projects", json={"path": str(workspace)}, headers=LOCAL_HEADERS)
    assert resp.status_code == 200, resp.text
    return client


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    from agent_builder.api import routes

    monkeypatch.setattr(routes, "get_llm_client", lambda **kwargs: fake)


class TestDirectLlmChat:
    """直连大模型：只跟模型对话，不碰工作流。"""

    def test_直连不产生任何任务(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeClient()
        _patch_client(monkeypatch, fake)
        client = _client_on(tmp_path)

        resp = client.post("/llm/chat", json={"message": "你好"}, headers=LOCAL_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["reply"] == "模型回复"
        assert get_store().list_all() == [], "直连绝不能建任务"
        assert client.get("/task-summaries", headers=LOCAL_HEADERS).json() == []

    def test_多轮消息按顺序原样转发(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeClient()
        _patch_client(monkeypatch, fake)
        client = _client_on(tmp_path)

        resp = client.post(
            "/llm/chat",
            json={
                "system": "你是助手",
                "messages": [
                    {"role": "user", "content": "第一句"},
                    {"role": "assistant", "content": "第一答"},
                ],
                "message": "第二句",
            },
            headers=LOCAL_HEADERS,
        )

        assert resp.status_code == 200, resp.text
        sent = fake.seen[0]
        assert sent[0] == {"role": "system", "content": "你是助手"}
        assert [item["content"] for item in sent[1:]] == ["第一句", "第一答", "第二句"]

    def test_直连不写工作区(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, _FakeClient())
        client = _client_on(tmp_path)
        before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

        # 就算用户让它"写个文件"，直连也只回话、不动磁盘
        resp = client.post(
            "/llm/chat", json={"message": "帮我在 outputs 写个文件"}, headers=LOCAL_HEADERS
        )

        assert resp.status_code == 200
        after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
        assert after == before, "直连不得写工作区"
        assert not (tmp_path / ".agent-memory").exists(), "直连不得写项目记忆"

    def test_无密钥时给可读错误(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, None)
        client = _client_on(tmp_path)

        resp = client.post("/llm/chat", json={"message": "你好"}, headers=LOCAL_HEADERS)

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error_name"] == "E_MODEL"
        assert "密钥" in detail["message"]

    def test_模型返回空时报错而不是假装成功(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(monkeypatch, _FakeClient(reply=""))
        client = _client_on(tmp_path)

        resp = client.post("/llm/chat", json={"message": "你好"}, headers=LOCAL_HEADERS)

        assert resp.status_code == 502

    def test_空消息被拒(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, _FakeClient())
        client = _client_on(tmp_path)

        resp = client.post(
            "/llm/chat", json={"messages": [], "message": "   "}, headers=LOCAL_HEADERS
        )

        assert resp.status_code == 400

    def test_必须带本机标识头(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, _FakeClient())
        client = _client_on(tmp_path)

        # 直连等于"把模型暴露给调用方" → 必须有本机标识闸门
        assert client.post("/llm/chat", json={"message": "你好"}).status_code == 403
