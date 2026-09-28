"""LLMClient 单元测试 —— mock ChatOpenAI，不依赖真实网络。"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_builder.llm.client import LLMClient
from agent_builder.llm.config import LLMConfig


def _make_mock_client(content: str = "hello") -> LLMClient:
    """构造一个注入了 mock 后端的 LLMClient。"""
    config = LLMConfig()  # api_key="" → _client=None
    client = LLMClient(config)
    mock = MagicMock()
    mock.invoke.return_value = MagicMock(content=content)
    client._client = mock  # type: ignore[attr-defined]
    return client


class TestLLMClientAvailable:
    def test_no_key_unavailable(self) -> None:
        config = LLMConfig()
        client = LLMClient(config)
        assert not client.is_available

    def test_mock_client_available(self) -> None:
        client = _make_mock_client()
        assert client.is_available


class TestLLMClientChat:
    def test_chat_returns_content(self) -> None:
        client = _make_mock_client("world peace")
        result = client.chat([{"role": "user", "content": "hi"}])
        assert result == "world peace"

    def test_chat_unavailable_returns_empty(self) -> None:
        config = LLMConfig()
        client = LLMClient(config)
        assert client.chat([{"role": "user", "content": "hi"}]) == ""

    def test_chat_invoke_exception_returns_empty(self) -> None:
        client = _make_mock_client()
        client._client.invoke.side_effect = RuntimeError("network down")  # type: ignore[attr-defined]
        assert client.chat([{"role": "user", "content": "hi"}]) == ""


class TestLLMClientCompleteJson:
    def test_complete_json_valid(self) -> None:
        client = _make_mock_client('{"key": "value"}')
        result = client.complete_json("return json", schema_hint='{"key": ""}')
        assert result == {"key": "value"}

    def test_complete_json_markdown_block(self) -> None:
        client = _make_mock_client('```json\n{"steps": [1, 2]}\n```')
        result = client.complete_json("return json")
        assert result == {"steps": [1, 2]}

    def test_complete_json_invalid_returns_empty(self) -> None:
        client = _make_mock_client("not json at all")
        result = client.complete_json("return json")
        assert result == {}

    def test_complete_json_unavailable_returns_empty(self) -> None:
        config = LLMConfig()
        client = LLMClient(config)
        assert client.complete_json("return json") == {}


class TestLLMConfig:
    def test_default_config(self) -> None:
        config = LLMConfig()
        assert config.api_key == ""
        assert config.base_url == "https://api.deepseek.com/v1"
        assert config.model == "deepseek-chat"
        assert not config.is_available

    def test_config_with_key(self) -> None:
        config = LLMConfig(api_key="sk-test")
        assert config.is_available
