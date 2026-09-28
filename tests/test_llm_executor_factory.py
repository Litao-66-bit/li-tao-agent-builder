"""make_llm_executor 单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_builder.contracts.schemas import Step
from agent_builder.llm.client import LLMClient
from agent_builder.llm.config import LLMConfig
from agent_builder.llm.executor_factory import make_llm_executor


def _make_mock_client(content: str = "LLM result") -> LLMClient:
    config = LLMConfig()
    client = LLMClient(config)
    mock = MagicMock()
    mock.invoke.return_value = MagicMock(content=content)
    client._client = mock  # type: ignore[attr-defined]
    return client


class TestMakeLLMExecutor:
    def test_executor_calls_llm(self) -> None:
        client = _make_mock_client("analysis done")
        executor = make_llm_executor(client, system_prompt="你是助手")
        step = Step(id="s1", action="analyze", inputs={"data": "x"})
        result = executor(step)
        assert result == "analysis done"

    def test_executor_without_system_prompt(self) -> None:
        client = _make_mock_client("ok")
        executor = make_llm_executor(client)
        step = Step(id="s1", action="test", inputs={})
        result = executor(step)
        assert result == "ok"

    def test_executor_unavailable(self) -> None:
        config = LLMConfig()
        client = LLMClient(config)
        executor = make_llm_executor(client)
        step = Step(id="s1", action="test", inputs={})
        assert executor(step) == "LLM_UNAVAILABLE"

    def test_executor_includes_action_and_inputs(self) -> None:
        client = _make_mock_client("ok")
        executor = make_llm_executor(client)
        step = Step(id="s1", action="file_write", inputs={"path": "/tmp/x"})
        executor(step)
        # 验证 invoke 被调用。
        call_args = client._client.invoke.call_args  # type: ignore[attr-defined]
        messages = call_args[0][0]
        user_msg = messages[-1]["content"]
        assert "file_write" in user_msg
        assert "/tmp/x" in user_msg
