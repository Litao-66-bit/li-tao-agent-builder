"""卡 49：输入输出流（observation）端到端保真 —— 模型看到的就是工具返回的。

要守住的是"链路不丢关键信息"，逐段钉：

1. **未截断时逐字保真**（不是"大致一样"）；
2. 截断时**格式如实**：写原文长度、并给出"怎么继续读"的可操作提示；
3. **失败观察保尾**（pytest 的原因在末尾 —— 卡 30 的既有口径）；
4. 观察进提示词时**逐行第 0 列**（卡 19：缩进即原文，不许被排版改写）；
5. 原始返回经 `take_tool_observation()` 回灌时**不被摘要吃掉**（卡 1 的既有口径）；
6. 卡 48 新加的关键信息（命中条数 / 目录条目数）必须**活到提示词里**（不被裁掉）。
"""

from __future__ import annotations

import pytest

from agent_builder.api.agent_loop import LoopRound
from agent_builder.api.deciders import (
    MAX_OBSERVATION_CHARS,
    OBSERVATION_TRUNCATED_MARK,
    LoopContext,
    _clip_observation,
    build_decision_prompt,
)
from agent_builder.api.orchestrator import build_step_executor, take_tool_observation
from agent_builder.api.role_catalog import build_catalog
from agent_builder.contracts.schemas import Step


class TestClipFidelity:
    def test_短观察逐字保真(self) -> None:
        raw = "line1\nline2\n  indented\n\ttabbed\n末尾"
        assert _clip_observation(raw) == raw

    def test_空观察返回空串(self) -> None:
        assert _clip_observation("") == ""
        assert _clip_observation(None) == ""

    def test_刚好到上限不截断(self) -> None:
        raw = "x" * MAX_OBSERVATION_CHARS
        assert _clip_observation(raw) == raw

    def test_超长时写明原文长度(self) -> None:
        raw = "A" * (MAX_OBSERVATION_CHARS + 500)
        clipped = _clip_observation(raw)

        assert clipped.startswith("A" * 100), "保头"
        assert str(len(raw)) in clipped, "必须报原文长度（模型才知道还有多少没看到）"
        assert OBSERVATION_TRUNCATED_MARK in clipped

    def test_截断时给出继续读的办法(self) -> None:
        """只说"已截断"没有用 —— 模型需要知道下一步怎么读到后面。"""
        raw = "行\n" * (MAX_OBSERVATION_CHARS // 2 + 200)
        clipped = _clip_observation(raw)

        assert "file_read" in clipped
        assert "start_line" in clipped

    def test_失败观察保尾(self) -> None:
        """卡 30：pytest 的失败原因在**末尾**，保头会把它丢掉。"""
        head = "收集完成\n" * 700
        tail = "FAILED tests/test_x.py::test_y - AssertionError: 期望 1 实际 2"
        raw = head + tail
        assert len(raw) > MAX_OBSERVATION_CHARS, "头部必须够长到真的触发截断"
        clipped = _clip_observation(raw, prefer_tail=True)

        assert tail in clipped, "失败时必须保住尾部原因"
        assert str(len(raw)) in clipped

    def test_截断后长度有界(self) -> None:
        clipped = _clip_observation("y" * 50_000)
        # 允许比窗口略长（要装下标记与提示），但不能失控
        assert len(clipped) < MAX_OBSERVATION_CHARS + 400


class TestObservationIntoPrompt:
    def _prompt_with(self, observation: str, status: str = "done") -> str:
        context = LoopContext(
            requirement="看看",
            catalog=build_catalog(sub_arch=False),
            history=[
                {
                    "round": 1,
                    "thought": "先列目录",
                    "role": "code_worker",
                    "action": "file_list",
                    "status": status,
                    "summary": "列出文件完成",
                    "observation": observation,
                }
            ],
        )
        return build_decision_prompt(context)

    def test_观察逐行第0列进提示词(self) -> None:
        """卡 19：缩进即原文 —— 排版不得改写观察内容。"""
        raw = "def outer():\n    def inner():\n        return 1"
        prompt = self._prompt_with(raw)

        assert raw in prompt, "观察必须原样出现（含缩进）"

    def test_观察有定界符(self) -> None:
        prompt = self._prompt_with("SOME_OBSERVATION_MARKER")
        assert "<observation>" in prompt
        assert "</observation>" in prompt

    def test_卡48的命中条数活到提示词(self) -> None:
        """关键信息放第一行的目的就是这个：裁到 2000 字也还在。"""
        big = "命中 201 条（下方每行 file:line:content；如需更多请缩小 pattern）\n" + (
            "x.py:1:aaa\n" * 400
        )
        prompt = self._prompt_with(big)

        assert "命中 201 条" in prompt

    def test_长观察在提示词里也是有界的(self) -> None:
        prompt = self._prompt_with("z" * 100_000)
        assert len(prompt) < 100_000, "超长观察必须被裁剪后才进提示词"


def _executor_for(workspace):
    """把工作区钉到 ``workspace`` 的单步执行器（executor 的工作区取自项目注册表）。"""
    from agent_builder.api import routes as routes_module
    from agent_builder.tools.gatekeeper import current_workspace_dir

    token = current_workspace_dir.set(workspace)
    original = routes_module._resolve_workspace_dir
    routes_module._resolve_workspace_dir = lambda: workspace
    try:
        executor, gatekeeper = build_step_executor("t-obs", capabilities=None)
    finally:
        current_workspace_dir.reset(token)
        routes_module._resolve_workspace_dir = original
    return executor, gatekeeper


class TestRawReturnRoundTrip:
    """`take_tool_observation()`：原始返回必须真的到得了循环（卡 1 的既有口径）。"""

    def test_原始返回被留底且读取即清空(self, tmp_path, monkeypatch) -> None:
        """留底发生在**执行链路唯一出口** `build_step_executor` 的 executor_fn 里。

        注意不能直接调 `registry.execute` 来验 —— 那条路不经过留底逻辑（本测试第一版
        就是这么写错的：断言永远为空）。这里走真实的单步执行器，
        并把**工作区**钉到 tmp_path（executor 的工作区取自项目注册表，测试里必须显式指定，
        否则越界校验会按默认工作区判定）。
        """
        target = tmp_path / "note.txt"
        target.write_text("hello-observation", encoding="utf-8")
        executor, _gk = _executor_for(tmp_path)

        executor(
            Step(id="s1", action="file_read", assignee="operator", inputs={"path": str(target)})
        )

        first = take_tool_observation()
        assert "hello-observation" in first, "工具真实返回必须留底给循环"
        assert take_tool_observation() == "", "读取即清空（防串到下一轮）"

    def test_工具失败时不留脏观察(self, tmp_path) -> None:
        """失败调用必须**清掉**上一轮的留底，否则模型会看到过期内容。"""
        executor, _gk = _executor_for(tmp_path)
        ok = tmp_path / "ok.txt"
        ok.write_text("旧内容", encoding="utf-8")
        executor(Step(id="s1", action="file_read", assignee="operator", inputs={"path": str(ok)}))
        assert take_tool_observation() != ""

        with pytest.raises(Exception):  # noqa: B017 - 契约异常族
            executor(
                Step(
                    id="s3",
                    action="file_read",
                    assignee="operator",
                    inputs={"path": str(tmp_path / "nope.txt")},
                )
            )

        # 失败这一步不该把"旧的观察"留在槽里冒充本次结果
        assert "旧内容" not in take_tool_observation()


class TestLoopRoundCarriesObservation:
    def test_轮次记录带观察字段(self) -> None:
        """`LoopRound.observation` 是观察进入提示词的唯一入口，字段不能丢。"""
        from dataclasses import fields

        names = {field.name for field in fields(LoopRound)}
        assert "observation" in names
        assert "summary" in names
