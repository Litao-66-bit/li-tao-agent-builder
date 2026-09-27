"""LLM 客户端：统一抽象 + DeepSeek 实现 + 可测试的 Mock 实现。

- DeepSeekClient：生产实现（ChatOpenAI 指向 DeepSeek 端点）。
- MockClient：测试/无密钥环境用，按系统提示词关键词路由返回确定性结果。

安全集成（P0）：
- 调用超时：ChatOpenAI request_timeout（默认 60s），超时转 E_TIMEOUT。
- 成本预算：注入 BudgetTracker，调用前 check()（超限抛 E_COST）、调用后 record()。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from agent_builder.contracts.errors import model_error, timeout_error
from agent_builder.llm.budget import BudgetTracker


class LLMClient(Protocol):
    """所有角色访问模型层的唯一接口。"""

    def chat_text(self, system: str, user: str, *, temperature: float = 0.7) -> str: ...

    def chat_json(self, system: str, user: str, *, temperature: float = 0.7) -> dict[str, Any]: ...


class DeepSeekClient:
    """DeepSeek 生产客户端（langchain-openai 兼容端点）。

    安全参数：
    - request_timeout：单次模型调用超时（秒），超时抛 E_TIMEOUT。
    - budget：BudgetTracker 实例；None 表示不设预算（生产建议始终注入）。
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        temperature: float = 0.3,
        request_timeout: float = 60.0,
        budget: BudgetTracker | None = None,
        correlation_id: str = "c-unknown",
    ) -> None:
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY 不能为空，请在 .env 中配置")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("缺少依赖 langchain-openai，请执行 pip install -e '.[dev]'") from exc
        self._llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=2000,
            request_timeout=request_timeout,
        )
        self.request_timeout = request_timeout
        self.budget = budget
        self.correlation_id = correlation_id

    def chat_text(self, system: str, user: str, *, temperature: float = 0.7) -> str:
        if self.budget is not None:
            self.budget.check()
        try:
            resp = self._llm.invoke(
                [{"role": "system", "content": system}, {"role": "user", "content": user}]
            )
            text = str(resp.content).strip()
        except Exception as exc:
            if self._is_timeout(exc):
                raise timeout_error(
                    f"模型调用超时（>{self.request_timeout}s）",
                    source="llm.deepseek",
                    correlation_id=self.correlation_id,
                ) from exc
            raise model_error(
                f"模型调用失败: {exc}",
                source="llm.deepseek",
                correlation_id=self.correlation_id,
            ) from exc
        if self.budget is not None:
            self.budget.record(system + user + text)
        return text

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        """识别超时类异常：openai.APITimeoutError / TimeoutError / 名称含 Timeout。"""
        if isinstance(exc, TimeoutError):
            return True
        name = type(exc).__name__.lower()
        return "timeout" in name or "deadline" in name

    def chat_json(self, system: str, user: str, *, temperature: float = 0.7) -> dict[str, Any]:
        text = self.chat_text(system, user, temperature=temperature)
        return self._parse_json(text, retry=1, system=system, user=user)

    def _parse_json(self, text: str, *, retry: int, system: str, user: str) -> dict[str, Any]:
        try:
            # 容忍模型在 JSON 前后附加说明文字：取首个 { 到末个 } 之间的子串。
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise ValueError("响应中未找到 JSON 对象")
            return json.loads(text[start : end + 1])
        except (ValueError, json.JSONDecodeError) as exc:
            if retry > 0:
                return self._parse_json(
                    self.chat_text(
                        system + "\n注意：请只输出合法 JSON，不要附加任何说明文字。",
                        user,
                    ),
                    retry=retry - 1,
                    system=system,
                    user=user,
                )
            raise model_error(
                f"模型 JSON 输出解析失败: {exc}",
                source="llm.deepseek",
                correlation_id=self.correlation_id,
            ) from exc


class MockClient:
    """确定性 Mock：按 system 关键词路由，供测试与无密钥演示。

    - system 含「分解器」→ 返回固定步骤 JSON
    - system 含「汇报员」→ 返回 mock 最终报告
    - system 含「执行者」→ 返回 mock 执行结果

    与 DeepSeekClient 同样支持 budget 注入（测试预算中断用）。
    """

    def __init__(self, *, correlation_id: str = "c-mock", budget: BudgetTracker | None = None) -> None:
        self.correlation_id = correlation_id
        self.budget = budget
        self.calls: list[tuple[str, str]] = []

    def chat_text(self, system: str, user: str, *, temperature: float = 0.7) -> str:
        if self.budget is not None:
            self.budget.check()
        self.calls.append((system, user))
        if "分解器" in system:
            text = self._mock_decompose(user)
        elif "汇报员" in system:
            text = f"mock 最终报告：{user[:80]}"
        elif "执行者" in system:
            text = f"mock 执行结果：{user[:60]}"
        else:
            text = f"mock 回复：{user[:80]}"
        if self.budget is not None:
            self.budget.record(system + user + str(text))
        return text

    def chat_json(self, system: str, user: str, *, temperature: float = 0.7) -> dict[str, Any]:
        if self.budget is not None:
            self.budget.check()
        self.calls.append((system, user))
        if "分解器" in system:
            return self._mock_decompose(user)
        raise model_error(
            "MockClient 仅支持「分解器」的 JSON 输出",
            source="llm.mock",
            correlation_id=self.correlation_id,
        )

    @staticmethod
    def _mock_decompose(user: str) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "id": "step-001",
                    "action": "llm_think",
                    "inputs": {"prompt": user},
                    "depends_on": [],
                    "status": "pending",
                }
            ]
        }


__all__ = ["DeepSeekClient", "LLMClient", "MockClient"]
