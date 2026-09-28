"""Decomposer LLM 集成测试 —— mock LLM 客户端。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from agent_builder.roles.decomposer import Decomposer


def _make_mock_llm(steps: list[dict[str, Any]]) -> MagicMock:
    """构造 mock LLM 客户端，complete_json 返回 {"steps": steps}。"""
    client = MagicMock()
    client.is_available = True
    client.complete_json.return_value = {"steps": steps}
    return client


class TestDecomposeWithLLM:
    def test_decompose_with_llm(self) -> None:
        client = _make_mock_llm([
            {"id": "step-001", "action": "file_write", "inputs": {"path": "/tmp/x"}, "depends_on": []},
            {"id": "step-002", "action": "web_search", "inputs": {"q": "test"}, "depends_on": ["step-001"]},
        ])
        d = Decomposer()
        result = d.decompose("t1", "写文件再搜索", llm_client=client)
        assert len(result.steps) == 2
        assert result.order == ["step-001", "step-002"]
        assert "step-001" in result.steps
        assert "step-002" in result.steps
        assert result.parallel_groups  # 有分组

    def test_decompose_llm_dedup(self) -> None:
        """相同 action + inputs 的步骤应去重。"""
        client = _make_mock_llm([
            {"id": "step-001", "action": "file_write", "inputs": {"path": "/x"}, "depends_on": []},
            {"id": "step-002", "action": "file_write", "inputs": {"path": "/x"}, "depends_on": []},
        ])
        d = Decomposer()
        result = d.decompose("t1", "test", llm_client=client)
        assert len(result.steps) == 1  # 去重

    def test_decompose_llm_parallel_grouping(self) -> None:
        """无依赖的步骤应在同一并行组。"""
        client = _make_mock_llm([
            {"id": "a", "action": "search", "inputs": {}, "depends_on": []},
            {"id": "b", "action": "fetch", "inputs": {}, "depends_on": []},
            {"id": "c", "action": "write", "inputs": {}, "depends_on": ["a", "b"]},
        ])
        d = Decomposer()
        result = d.decompose("t1", "test", llm_client=client)
        assert len(result.parallel_groups) >= 2
        assert set(result.parallel_groups[0]) == {"a", "b"}
        assert result.parallel_groups[1] == ["c"]


class TestDecomposeLLMFailure:
    def test_decompose_llm_invalid_json(self) -> None:
        client = MagicMock()
        client.is_available = True
        client.complete_json.return_value = {}  # 无 "steps" key
        d = Decomposer()
        result = d.decompose("t1", "test", llm_client=client)
        assert len(result.steps) == 0
        assert result.pending_questions  # 回退待确认

    def test_decompose_llm_exception(self) -> None:
        client = MagicMock()
        client.is_available = True
        client.complete_json.side_effect = RuntimeError("LLM down")
        d = Decomposer()
        result = d.decompose("t1", "test", llm_client=client)
        assert len(result.steps) == 0
        assert result.pending_questions

    def test_decompose_llm_empty_steps(self) -> None:
        client = _make_mock_llm([])
        d = Decomposer()
        result = d.decompose("t1", "test", llm_client=client)
        assert len(result.steps) == 0
        assert result.pending_questions


class TestDecomposeBackwardsCompat:
    def test_decompose_no_llm(self) -> None:
        """llm_client=None → 保持原待确认逻辑（回归保护）。"""
        d = Decomposer()
        result = d.decompose("t1", "test")
        assert len(result.steps) == 0
        assert result.pending_questions == ["需要 LLM 拆分需求（当前无预拆分步骤）"]

    def test_decompose_llm_not_available(self) -> None:
        """llm_client.is_available=False → 回退待确认。"""
        client = MagicMock()
        client.is_available = False
        d = Decomposer()
        result = d.decompose("t1", "test", llm_client=client)
        assert len(result.steps) == 0
        assert result.pending_questions

    def test_decompose_with_raw_steps_ignores_llm(self) -> None:
        """raw_steps 非 None 时 LLM 不被调用。"""
        client = MagicMock()
        client.is_available = True
        d = Decomposer()
        raw = [{"id": "s1", "action": "test", "inputs": {}, "depends_on": []}]
        result = d.decompose("t1", "test", raw_steps=raw, llm_client=client)
        assert len(result.steps) == 1
        client.complete_json.assert_not_called()
