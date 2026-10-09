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


def _make_pending_llm(steps: list[dict[str, Any]], pending: Any) -> MagicMock:
    client = MagicMock()
    client.is_available = True
    client.complete_json.return_value = {"steps": steps, "pending_questions": pending}
    return client


class TestDecomposePendingQuestions:
    """回归：模型显式列出的待确认点必须被回收（此前恒空，多义需求无法度量）。"""

    def test_模型列出的待确认点被回收(self) -> None:
        client = _make_pending_llm(
            [{"id": "step-001", "action": "web_search", "inputs": {}, "depends_on": []}],
            ["排名维度未定", "是否纳入衍生模型"],
        )
        result = Decomposer().decompose("t1", "调研", llm_client=client)
        assert result.pending_questions == ["排名维度未定", "是否纳入衍生模型"]

    def test_与结构歧义合并且保序去重(self) -> None:
        client = _make_pending_llm(
            [{"id": "step-001", "action": "a", "inputs": {}, "depends_on": ["ghost"]}],
            ["口径未定", "ghost 问题", "口径未定"],
        )
        result = Decomposer().decompose("t1", "调研", llm_client=client)
        # 模型项在前（去重），结构歧义（悬空依赖）在后
        assert result.pending_questions[:2] == ["口径未定", "ghost 问题"]
        assert any("未定义的步骤" in q for q in result.pending_questions)

    def test_非法待确认点被忽略(self) -> None:
        client = _make_pending_llm(
            [{"id": "s1", "action": "a", "inputs": {}, "depends_on": []}], "不是列表"
        )
        result = Decomposer().decompose("t1", "x", llm_client=client)
        assert result.pending_questions == []

    def test_无该字段时不报错(self) -> None:
        client = _make_mock_llm(
            [{"id": "s1", "action": "a", "inputs": {}, "depends_on": []}]
        )  # 不含 pending_questions
        result = Decomposer().decompose("t1", "x", llm_client=client)
        assert result.pending_questions == []


class TestPromptHygiene:
    """回归：提示词/ schema 不得举出系统不存在的工具名（曾举 code_gen）。"""

    def _capture(self) -> tuple[str, str]:
        client = _make_mock_llm(
            [{"id": "s1", "action": "a", "inputs": {}, "depends_on": []}]
        )
        Decomposer().decompose("t1", "需求", llm_client=client)
        call = client.complete_json.call_args
        return call.args[0], call.kwargs["schema_hint"]

    def test_提示词不含不存在的工具(self) -> None:
        prompt, schema = self._capture()
        assert "code_gen" not in prompt
        assert "code_gen" not in schema

    def test_提示词给出真实工具示例(self) -> None:
        prompt, _ = self._capture()
        assert "file_write" in prompt
        assert "web_search" in prompt

    def test_schema_声明待确认点字段(self) -> None:
        _, schema = self._capture()
        assert "pending_questions" in schema

    def test_提示词禁止编造工具且不再诱导动词短语(self) -> None:
        """回归：收紧后不得再出现「无对应工具时用动词短语」，且必须禁止编造工具名。"""
        prompt, schema = self._capture()
        assert "严禁编造系统不存在的工具名" in prompt
        assert "动词短语" not in prompt
        assert "动词短语" not in schema

    def test_提示词要求无工具时改写或转待确认(self) -> None:
        prompt, _ = self._capture()
        assert "file_read + file_write" in prompt
        assert "pending_questions" in prompt

    def test_提示词不点名不存在的工具(self) -> None:
        # file_delete 现已注册为真实工具（可出现在提示词）；file_rename 仍不存在 → 不得出现
        prompt, schema = self._capture()
        assert "file_rename" not in prompt
        assert "file_rename" not in schema

    def test_提示词要求覆盖时显式传overwrite(self) -> None:
        prompt, _ = self._capture()
        assert "overwrite=true" in prompt
