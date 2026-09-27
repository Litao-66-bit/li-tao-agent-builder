"""LangGraph 图组装：最小闭环「需求 → 计划 → 用户确认 → 执行 → 验证 → 汇报」。

图结构（对应主架构执行层流水线）：
    START → decompose → schedule → confirm(interrupt) → execute → verify → summarize → END

- confirm 是 graph interrupt：第一次 invoke 停在确认点返回计划，
  用户确认后以 Command(resume=...) 恢复执行。
- checkpointer 可注入（P1-1 状态持久化）：
  - 默认 MemorySaver：单次运行，进程退出即丢中断状态；
  - 生产用 SqliteSaver（build_persistent_graph）：中断/恢复跨进程保持。
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

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


def build_graph(llm: LLMClient, *, checkpointer: Any | None = None):
    """组装最小闭环图。

    - llm：LLMClient 实现（DeepSeek 或 Mock）。
    - checkpointer：LangGraph 检查点（MemorySaver / SqliteSaver）；None 时默认 MemorySaver。
    """
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

    if checkpointer is None:
        checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)


def build_persistent_graph(llm: LLMClient, db_path: str | Path):
    """持久化检查点版：中断/恢复跨进程保持（P1-1）。

    db_path 为 SQLite 数据库文件路径（父目录自动创建）。
    返回 (graph, checkpointer)；调用方负责在结束时关闭连接（saver.conn.close()）。
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    return build_graph(llm, checkpointer=saver), saver


__all__ = ["build_graph", "build_persistent_graph"]
