"""检索执行者（Searcher）—— 主架构・执行层。

职责：拆解检索需求为多路关键词，调用检索工具，去重排序，
输出带来源链接与置信度的清单。

边界声明：
- 结果不足 → 换关键词再搜 1 轮
- 关键声明无来源 → 标"未查证"（verified=False）
- 工具被门卫拒绝 → 查明原因，换合法工具或上报

执行协议：
1. 拆解检索需求为多路关键词（中英文各一路）
2. 经工具门卫调用检索工具；去重、按权威性排序
3. 输出带来源链接与置信度的清单，区分"已查证/单方声称"
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 检索类 action 集合。
SEARCH_ACTIONS: frozenset[str] = frozenset({
    "web_search",
    "web_fetch",
})

# 最大搜索轮次（结果不足时换关键词再搜 1 轮）。
MAX_ROUNDS = 2

# 最少结果数（不足则换关键词再搜）。
MIN_RESULTS = 3

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class SearchItem:
    """单条检索结果。"""

    claim: str  # 声明/标题
    source_url: str  # 来源链接
    snippet: str  # 摘要
    confidence: float  # 置信度 0-1
    verified: bool = False  # 已查证 / 单方声称


@dataclass(slots=True)
class SearchResult:
    """检索执行结果。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    items: list[SearchItem] = field(default_factory=list)
    keywords_used: list[str] = field(default_factory=list)  # 使用的关键词
    rounds: int = 0  # 搜索轮次
    error: str | None = None


@dataclass(slots=True)
class Searcher:
    """检索执行者角色：多路关键词 + 去重排序 + 来源标注。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        max_rounds: 最大搜索轮次。
    """

    correlation_id: str = "c-unknown"
    max_rounds: int = MAX_ROUNDS

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> SearchResult:
        """执行检索类步骤。

        Args:
            step: 检索类步骤（action 必须在 SEARCH_ACTIONS 中）。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            SearchResult：检索结果清单 + 关键词 + 轮次。
        """
        # 1. 校验是否检索类。
        if step.action not in SEARCH_ACTIONS:
            return SearchResult(
                step_id=step.id,
                status="rejected",
                error=f"任务超出检索范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return SearchResult(
                step_id=step.id,
                status="pending",
            )

        # 3. 拆解检索需求为多路关键词。
        keywords = self._expand_keywords(step.inputs.get("query", ""))
        all_items: list[SearchItem] = []
        rounds = 0

        # 4. 多轮搜索（结果不足 → 换关键词再搜）。
        for round_idx in range(self.max_rounds):
            rounds = round_idx + 1
            try:
                executor_fn(step)
                # 从 inputs 提取结果。
                raw_items = step.inputs.get("results", [])
                round_items = self._parse_items(raw_items)
                all_items.extend(round_items)
                if len(all_items) >= MIN_RESULTS:
                    break
            except PermissionError as exc:
                return SearchResult(
                    step_id=step.id,
                    status="failed",
                    error=f"工具被门卫拒绝: {exc}",
                )
            except Exception as exc:  # noqa: BLE001  执行者需捕获所有执行异常
                return SearchResult(
                    step_id=step.id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

        # 5. 去重 + 按权威性排序。
        all_items = self._dedup(all_items)
        all_items = self._sort_by_confidence(all_items)

        # 6. 关键声明无来源 → 标"未查证"。
        for item in all_items:
            if not item.source_url:
                item.verified = False

        return SearchResult(
            step_id=step.id,
            status="done",
            items=all_items,
            keywords_used=keywords,
            rounds=rounds,
        )

    def _expand_keywords(self, query: str) -> list[str]:
        """拆解检索需求为多路关键词（中英文各一路）。

        当前实现为骨架：实际关键词拆解由 LLM 完成。
        """
        if not query:
            return []
        return [query]  # 骨架：返回原查询

    def _parse_items(self, raw_items: list[dict[str, Any]]) -> list[SearchItem]:
        """解析检索结果清单。"""
        items: list[SearchItem] = []
        for raw in raw_items:
            if isinstance(raw, dict) and "claim" in raw:
                items.append(
                    SearchItem(
                        claim=str(raw["claim"]),
                        source_url=str(raw.get("source_url", "")),
                        snippet=str(raw.get("snippet", "")),
                        confidence=float(raw.get("confidence", 0.0)),
                        verified=raw.get("verified", False),
                    )
                )
        return items

    def _dedup(self, items: list[SearchItem]) -> list[SearchItem]:
        """去重（按 source_url + claim）。"""
        seen: set[str] = set()
        result: list[SearchItem] = []
        for item in items:
            key = f"{item.source_url}|{item.claim}"
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    def _sort_by_confidence(self, items: list[SearchItem]) -> list[SearchItem]:
        """按置信度降序排序。"""
        return sorted(items, key=lambda x: x.confidence, reverse=True)


__all__ = [
    "MAX_ROUNDS",
    "MIN_RESULTS",
    "SEARCH_ACTIONS",
    "ExecutorFn",
    "SearchItem",
    "SearchResult",
    "Searcher",
]
