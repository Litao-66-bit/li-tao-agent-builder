"""LLM 调用预算追踪 —— P0-1「成本分级」的最小实现。

BudgetTracker 在每次 LLM 调用前后做两件事：
1. check()：调用前核对「累计调用次数」与「预估 token」是否已达上限，超限抛 E_COST（7000）。
2. record()：调用后累加确定性 token 估算（非精确计数，仅用于预算闸门）。

设计原则（与契约对齐）：
- E_COST 不可重试（NON_RETRYABLE）：预算超限重试只会继续消耗资源，必须人工调整预算。
- 默认上限保守：50 次调用 / 100k 预估 token，可在 CLI（--max-calls）与构造参数中调整。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_builder.contracts.errors import cost_error

DEFAULT_MAX_CALLS = 50  # 单任务 LLM 调用次数硬上限
DEFAULT_MAX_TOKENS = 100_000  # 单任务预估 token 硬上限（约合数元人民币量级，保守值）


def estimate_tokens(text: str) -> int:
    """确定性 token 估算：ASCII 字符 /4 + 非 ASCII 字符 /2（不精确，仅供预算闸门）。"""
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars // 4 + (len(text) - ascii_chars) // 2 + 1


@dataclass(slots=True)
class BudgetTracker:
    """预算闸门：check 不通过即抛 E_COST，任务中止。"""

    correlation_id: str = "c-unknown"
    max_calls: int = DEFAULT_MAX_CALLS
    max_tokens: int = DEFAULT_MAX_TOKENS
    calls: int = 0
    tokens: int = 0

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise cost_error(
                f"LLM 调用次数达预算上限 {self.max_calls}，任务中止（E_COST）",
                source="llm.budget",
                correlation_id=self.correlation_id,
            )
        if self.tokens >= self.max_tokens:
            raise cost_error(
                f"预估 token 达预算上限 {self.max_tokens}，任务中止（E_COST）",
                source="llm.budget",
                correlation_id=self.correlation_id,
            )

    def record(self, text: str) -> None:
        """调用后登记：估算 token 并计 1 次调用。"""
        self.tokens += estimate_tokens(text)
        self.calls += 1

    def snapshot(self) -> dict[str, int]:
        return {"calls": self.calls, "tokens": self.tokens}


__all__ = ["DEFAULT_MAX_CALLS", "DEFAULT_MAX_TOKENS", "BudgetTracker", "estimate_tokens"]
