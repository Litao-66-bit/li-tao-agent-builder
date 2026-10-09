"""对话入口（``/chat``）—— 「先聊天，再干活」的意图分流。

全部用假 LLM 客户端驱动，不碰网络；端点用例还会断言**不建任务**（无副作用）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.chat import (
    KIND_CHAT,
    KIND_TASK,
    build_prompt,
    classify_intent,
    missing_key_outcome,
    unavailable_outcome,
)
from agent_builder.api.deps import reset_store


class _FakeClient:
    """假 LLM 客户端：直接返回预置 JSON，并记下收到的提示词。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.prompt = ""
        self.schema_hint = ""

    def complete_json(self, prompt: str, schema_hint: str = "") -> dict[str, Any]:
        self.prompt = prompt
        self.schema_hint = schema_hint
        return self.payload


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_store()


class TestPrompt:
    def test_提示词含分流规则与用户消息(self) -> None:
        prompt = build_prompt("你好")
        assert 'kind="chat"' in prompt
        assert 'kind="task"' in prompt
        assert "拿不准时选 chat" in prompt  # 宁可多聊一句，也不平白弹计划
        assert "【用户这一句】你好" in prompt

    def test_带上最近对话上下文(self) -> None:
        history = [
            {"role": "user", "content": "帮我看看这个仓库"},
            {"role": "assistant", "content": "好的，你想看哪部分？"},
        ]
        prompt = build_prompt("先看测试", history=history)
        assert "【最近对话】" in prompt
        assert "用户：帮我看看这个仓库" in prompt
        assert "助手：好的，你想看哪部分？" in prompt

    def test_超长输入被截断且单行化(self) -> None:
        prompt = build_prompt("x" * 5000 + "\n第二行")
        assert "第二行" not in prompt  # 换行被折平后整段截断
        assert len(prompt) < 5000


class TestClassify:
    def test_闲聊直接回话(self) -> None:
        client = _FakeClient({"kind": "chat", "reply": "你好！有什么可以帮你的？", "reason": "打招呼"})
        outcome = classify_intent(client, "你好")
        assert outcome.kind == KIND_CHAT
        assert outcome.reply == "你好！有什么可以帮你的？"
        assert outcome.reason == "打招呼"

    def test_执行诉求转入任务链路(self) -> None:
        client = _FakeClient({"kind": "task", "reply": "好的，我来统计。", "reason": "明确要求做事"})
        outcome = classify_intent(client, "统计测试文件数量")
        assert outcome.kind == KIND_TASK
        assert outcome.reply == "好的，我来统计。"

    def test_模型乱返时如实说明且不转任务(self) -> None:
        """解析失败 / kind 非法 / reply 空 → 一律 chat + 如实说明，不臆造回答。"""
        for payload in ({}, {"kind": "teleport", "reply": "x"}, {"kind": "chat", "reply": ""}):
            outcome = classify_intent(_FakeClient(payload), "你好")
            assert outcome.kind == KIND_CHAT
            assert outcome.reply == unavailable_outcome().reply

    def test_kind与reply做了清洗(self) -> None:
        client = _FakeClient({"kind": "  TASK  ", "reply": "  好的，开始。  "})
        outcome = classify_intent(client, "帮我跑测试")
        assert outcome.kind == KIND_TASK
        assert outcome.reply == "好的，开始。"

    def test_无密钥提示不弹工作流(self) -> None:
        outcome = missing_key_outcome()
        assert outcome.kind == KIND_CHAT
        assert "API 密钥" in outcome.reply
        assert outcome.reason == "no_api_key"


class TestChatEndpoint:
    def test_无密钥返回人话且不建任务(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agent_builder.api.routes.get_llm_client", lambda **_: None)
        client = TestClient(create_app())

        resp = client.post("/chat", json={"message": "你好"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["kind"] == "chat"
        assert data["reply"] == missing_key_outcome().reply
        assert data["reason"] == "no_api_key"
        # 关键：不建任务 —— 这正是「你好也会弹出执行计划」的根因。
        assert client.get("/tasks").json() == []

    def test_有模型时按判定返回(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeClient({"kind": "task", "reply": "好的，我来办。", "reason": "执行诉求"})
        monkeypatch.setattr("agent_builder.api.routes.get_llm_client", lambda **_: fake)
        client = TestClient(create_app())

        data = client.post("/chat", json={"message": "统计测试文件数量"}).json()

        assert data == {"kind": "task", "reply": "好的，我来办。", "reason": "执行诉求"}
        assert fake.prompt  # 确实把提示词交给了模型

    def test_空消息被拒(self) -> None:
        client = TestClient(create_app())
        assert client.post("/chat", json={"message": "   "}).status_code == 422
