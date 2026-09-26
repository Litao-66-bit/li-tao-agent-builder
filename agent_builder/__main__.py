"""CLI 入口：python -m agent_builder "你的需求" [--mock]

最小闭环演示：需求 → 分解 → 计划 → 用户确认 → 执行 → 验证 → 汇报。

- 默认使用 DeepSeek（需 .env 中配置 DEEPSEEK_API_KEY）。
- --mock 使用 MockClient（无需密钥，用于演示与测试）。
"""

from __future__ import annotations

import argparse
import sys
import uuid

from dotenv import load_dotenv

from agent_builder.llm.client import DeepSeekClient, MockClient


def _print_plan(plan: dict) -> None:
    print("\n======== 执行计划（请确认） ========")
    print(f"任务: {plan['task_id']}")
    for i, sid in enumerate(plan["order"], 1):
        print(f"  {i}. {sid}")
    print("====================================")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-builder",
        description="Agent Builder：输入需求，自动规划、生成、验证并交付可运行的 Agent。",
    )
    parser.add_argument("requirement", nargs="?", help="自然语言需求，如：帮我生成一个每日新闻摘要 Agent")
    parser.add_argument("--mock", action="store_true", help="使用 MockClient（无需 API Key 的演示模式）")
    args = parser.parse_args(argv)

    requirement = args.requirement
    if not requirement:
        requirement = input("请输入你的需求：").strip()
        if not requirement:
            parser.error("需求不能为空")

    # ── 构建 LLM 客户端 ──────────────────────────────────────────
    if args.mock:
        llm = MockClient()
        print("[mock] 使用 MockClient 演示模式")
    else:
        load_dotenv()
        import os

        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            print(
                "错误：未配置 DEEPSEEK_API_KEY。\n"
                "  1. 复制 .env.example 为 .env，填入你的 DeepSeek API Key\n"
                "  2. 或使用演示模式：python -m agent_builder \"需求\" --mock",
                file=sys.stderr,
            )
            return 2
        llm = DeepSeekClient(api_key)

    # ── 运行最小闭环 ─────────────────────────────────────────────
    from langgraph.types import Command

    from agent_builder.graph.build import build_graph

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": task_id}}
    graph = build_graph(llm)

    try:
        first = graph.invoke(
            {"task_id": task_id, "requirement": requirement, "correlation_id": f"c-{task_id}"},
            config,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 兜底展示
        print(f"规划失败：{exc}", file=sys.stderr)
        return 1

    _print_plan(first["plan"])

    answer = input("确认执行？(y/n) > ").strip().lower()
    if answer not in ("y", "yes", "是"):
        print("已取消，任务未执行。")
        return 0

    final = graph.invoke(Command(resume={"confirmed": True}), config)
    print("\n======== 最终交付报告 ========")
    print(final.get("report", "（报告缺失）"))
    print("==============================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
