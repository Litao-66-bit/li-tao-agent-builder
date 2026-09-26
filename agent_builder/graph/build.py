"""LangGraph 图组装：最小闭环「需求 → 计划 → 用户确认 → 执行 → 验证 → 汇报」。

图结构（对应主架构执行层流水线）：
    START → decompose → schedule → confirm(interrupt) → execute → verify → summarize → END

- confirm 是 graph interrupt：第一次 invoke 停在确认点返回计划，
  用户确认后以 Command(resume=...) 恢复执行。
- checkpointer 用 MemorySaver（CLI 单次运行场景足够）。
"""

from __future__ import annotations

from functools import partial

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agent_builder.graph.nodes import (
    confirm_node,
    decompose_node,
    execute_node,
    schedule_node,
    summarize_node,
    verify_node,
)
from agent_builder.graph.state import AgentState
from agent_builder.llm.client import LLMClient


def build_graph(llm: LLMClient):
    """组装最小闭环图。llm 为 LLMClient 实现（DeepSeek 或 Mock）。"""
    builder = StateGraph(AgentState)

    builder.add_node("decompose", partial(decompose_node, llm=llm))
    builder.add_node("schedule", schedule_node)
    builder.add_node("confirm", confirm_node)
    builder.add_node("execute", partial(execute_node, llm=llm))
    builder.add_node("verify", verify_node)
    builder.add_node("summarize", partial(summarize_node, llm=llm))

    builder.add_edge(START, "decompose")
    builder.add_edge("decompose", "schedule")
    builder.add_edge("schedule", "confirm")
    builder.add_edge("confirm", "execute")
    builder.add_edge("execute", "verify")
    builder.add_edge("verify", "summarize")
    builder.add_edge("summarize", END)

    return builder.compile(checkpointer=MemorySaver())


__all__ = ["build_graph"]
