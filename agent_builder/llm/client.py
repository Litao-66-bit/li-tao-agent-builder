"""LLM 客户端 —— 封装 langchain_openai.ChatOpenAI 指向 DeepSeek。

提供：
- chat(messages) -> str：纯文本对话。
- complete_json(prompt, schema_hint) -> dict：要求 LLM 返回 JSON 并解析。

失败降级：无密钥或调用异常时返回空值 / 空字典，不抛错。

模型与采样温度可由调用方覆盖（对应前端「模型」下拉与「推理强度」滑块）；
未指定时用默认值。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from agent_builder.llm.config import LLMConfig

logger = logging.getLogger(__name__)

# 默认采样温度（前端「推理强度」未指定时使用）。
DEFAULT_TEMPERATURE = 0.3

# usage 里参与累加的数值字段（cached / uncached 分开计）。
_USAGE_INT_KEYS: tuple[str, ...] = (
    "calls",
    "prompt_tokens",
    "cached_prompt_tokens",
    "uncached_prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


@dataclass(slots=True)
class LLMUsage:
    """Token 用量（cached 与 uncached 分开计）。

    ``prompt_tokens`` 为输入总量（含命中缓存的 token）；``cached_prompt_tokens``
    为其中命中前缀缓存的部分，``uncached_prompt_tokens`` 为实际计费/新增部分。
    """

    calls: int = 0
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: LLMUsage) -> None:
        """把另一份用量累加进来。"""
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.cached_prompt_tokens += other.cached_prompt_tokens
        self.completion_tokens += other.completion_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "uncached_prompt_tokens": self.uncached_prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class UsageAccumulator:
    """按角色累计 token 用量（供 task 级计量与 A/B 归因）。

    作为 ``LLMClient`` 的 usage_sink 使用：客户端每次调用后把「本次增量」交给
    ``record``；按角色派生实例共享同一个 accumulator，因此可得到任务级总量。
    """

    def __init__(self) -> None:
        self._total = LLMUsage()
        self._by_role: dict[str, LLMUsage] = {}

    @property
    def total(self) -> LLMUsage:
        return self._total

    def record(self, role: str, usage: LLMUsage) -> None:
        """记录一次调用的用量增量。"""
        self._total.add(usage)
        key = role or "unknown"
        self._by_role.setdefault(key, LLMUsage()).add(usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._total.to_dict(),
            "by_role": {role: stat.to_dict() for role, stat in sorted(self._by_role.items())},
        }


def merge_usage(left: dict[str, Any] | None, right: dict[str, Any]) -> dict[str, Any]:
    """合并两份 usage 统计（数值累加 / by_role 逐角色累加 / mode 取后者）。"""
    base = dict(left or {})
    merged: dict[str, Any] = {
        key: int(base.get(key, 0)) + int(right.get(key, 0)) for key in _USAGE_INT_KEYS
    }
    by_role: dict[str, dict[str, int]] = {}
    for source in (base.get("by_role") or {}, right.get("by_role") or {}):
        for role, stats in source.items():
            current = by_role.setdefault(role, dict.fromkeys(_USAGE_INT_KEYS, 0))
            for key in _USAGE_INT_KEYS:
                current[key] += int(stats.get(key, 0))
    merged["by_role"] = {role: by_role[role] for role in sorted(by_role)}
    mode = right.get("mode") or base.get("mode")
    if mode:
        merged["mode"] = mode
    return merged


def _as_int(value: Any) -> int:
    """把数值字段安全转成非负整数（缺失/异常一律记 0）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _extract_usage(resp: Any) -> LLMUsage:
    """从 langchain 响应里提取用量（cached / uncached 分开）。

    langchain_openai 一般提供 ``usage_metadata``（``input_token_details.cache_read``
    为命中缓存量）；部分实现放在 ``response_metadata.token_usage``（DeepSeek 的
    ``prompt_cache_hit_tokens``）。两处都取不到时记 0，不影响主流程。
    """
    prompt = completion = cached = 0
    meta = getattr(resp, "usage_metadata", None)
    if isinstance(meta, dict):
        prompt = _as_int(meta.get("input_tokens"))
        completion = _as_int(meta.get("output_tokens"))
        details = meta.get("input_token_details")
        if isinstance(details, dict):
            cached = _as_int(details.get("cache_read")) or _as_int(details.get("cached_tokens"))
    else:
        raw_meta = getattr(resp, "response_metadata", None)
        token_usage = raw_meta.get("token_usage") if isinstance(raw_meta, dict) else None
        if isinstance(token_usage, dict):
            prompt = _as_int(token_usage.get("prompt_tokens"))
            completion = _as_int(token_usage.get("completion_tokens"))
            cached = _as_int(token_usage.get("prompt_cache_hit_tokens"))
    cached = min(cached, prompt)
    return LLMUsage(
        calls=1,
        prompt_tokens=prompt,
        cached_prompt_tokens=cached,
        completion_tokens=completion,
    )


class LLMClient:
    """DeepSeek LLM 客户端（OpenAI 兼容格式）。

    无密钥时 _client=None，所有方法返回空值（降级）。

    Attributes:
        temperature: 当前生效的采样温度（构造时可覆盖）。
        role_brief: 角色简报（阶段 1 默认关闭；非空时注入 system 段最前）。
        usage: 本实例累计的 token 用量。
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        temperature: float | None = None,
        role_brief: str = "",
        role: str = "",
        usage_sink: Any = None,
    ) -> None:
        self._config = config
        self._temperature = DEFAULT_TEMPERATURE if temperature is None else temperature
        self._role_brief = role_brief
        self._role = role
        self._usage_sink = usage_sink
        self._usage = LLMUsage()
        # 同角色派生客户端缓存：同一 run_plan 内重复派发不再重建连接对象。
        self._role_clients: dict[tuple[str, str], LLMClient] = {}
        self._client: Any = None
        if config.is_available:
            try:
                from langchain_openai import ChatOpenAI

                self._client = ChatOpenAI(
                    model=config.model,
                    base_url=config.base_url,
                    api_key=config.api_key,
                    temperature=self._temperature,
                )
            except Exception as exc:  # noqa: BLE001  降级：实例化失败不阻断
                logger.warning("LLM 客户端初始化失败，降级为不可用: %s", exc)
                self._client = None

    @property
    def is_available(self) -> bool:
        """客户端是否就绪。"""
        return self._client is not None

    @property
    def model(self) -> str:
        """当前生效的模型名。"""
        return self._config.model

    @property
    def temperature(self) -> float:
        """当前生效的采样温度。"""
        return self._temperature

    @property
    def role_brief(self) -> str:
        """当前生效的角色简报（空串表示不注入）。"""
        return self._role_brief

    @property
    def usage(self) -> LLMUsage:
        """本实例累计的 token 用量。"""
        return self._usage

    def with_role_brief(self, brief: str, *, role: str = "") -> LLMClient:
        """派生一个「带该角色简报」的客户端实例（共享用量 sink）。

        同一 (简报, 角色) 只派生一次；``run_plan`` 里多个步骤派给同一角色时复用，
        避免重复建连。这是「串味」的解法：每个角色拿到自己的身份实例，而不是共用
        一个无身份的客户端。
        """
        if not brief:
            return self
        key = (brief, role)
        cached = self._role_clients.get(key)
        if cached is not None:
            return cached
        derived = LLMClient(
            self._config,
            temperature=self._temperature,
            role_brief=brief,
            role=role,
            usage_sink=self._usage_sink,
        )
        self._role_clients[key] = derived
        return derived

    def _with_role_brief(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """把角色简报注入 system 段最前（无 system 时新建一段，不改动入参）。"""
        if not self._role_brief:
            return messages
        prepared = [dict(message) for message in messages]
        for message in prepared:
            if message.get("role") == "system":
                existing = str(message.get("content") or "")
                message["content"] = (
                    f"{self._role_brief}\n\n{existing}" if existing else self._role_brief
                )
                return prepared
        prepared.insert(0, {"role": "system", "content": self._role_brief})
        return prepared

    def chat(self, messages: list[dict[str, str]]) -> str:
        """纯文本对话。

        Args:
            messages: OpenAI 消息格式 [{"role": "user", "content": "..."}]。

        Returns:
            LLM 回复文本；不可用或异常时返回空串。
        """
        if not self.is_available:
            return ""
        try:
            resp = self._client.invoke(self._with_role_brief(messages))
            self._record_usage(resp)
            content = getattr(resp, "content", "")
            return str(content) if content else ""
        except Exception as exc:  # noqa: BLE001  降级：调用失败返回空串
            logger.warning("LLM chat 调用失败: %s", exc)
            return ""

    def _record_usage(self, resp: Any) -> None:
        """记录一次调用的 token 用量（本实例累计 + 上报 sink）。"""
        delta = _extract_usage(resp)
        self._usage.add(delta)
        sink = self._usage_sink
        if sink is not None:
            try:
                sink.record(self._role, delta)
            except Exception as exc:  # noqa: BLE001  计量失败不影响主流程
                logger.warning("usage 记录失败: %s", exc)

    def complete_json(self, prompt: str, schema_hint: str = "") -> dict[str, Any]:
        """要求 LLM 返回 JSON 并解析。

        Args:
            prompt: 用户提示（描述要 LLM 做什么）。
            schema_hint: 期望的 JSON 结构提示（可选）。

        Returns:
            解析后的字典；不可用或解析失败返回 {}。
        """
        if not self.is_available:
            return {}
        system = "你是一个严格的结构化输出器。只返回纯 JSON，不要任何额外文字或解释。"
        if schema_hint:
            system += f"\n期望结构：{schema_hint}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        raw = self.chat(messages)
        if not raw:
            return {}
        return self._extract_json(raw)

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """从可能含 markdown 代码块的文本中提取 JSON。"""
        # 尝试直接解析。
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 代码块。
        match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试提取首个 {...} 块。
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {}


def probe_api_key(
    api_key: str,
    *,
    base_url: str = "https://api.deepseek.com/v1",
    timeout_s: float = 5.0,
) -> tuple[bool, str]:
    """轻量连通性探针：``GET {base_url}/models`` 校验密钥是否真实可用。

    仅在调用方显式要求（``verify=true``）时使用；失败不改变任何已有状态，
    由调用方决定是否回滚。异常信息只描述结果，绝不包含密钥明文。

    Args:
        api_key: 待校验的密钥明文。
        base_url: OpenAI 兼容基址。
        timeout_s: 单次探针超时（秒）。

    Returns:
        ``(是否可用, 原因)``。
    """
    url = f"{base_url.rstrip('/')}/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            if 200 <= resp.status < 300:
                return True, "连通性校验通过"
            return False, f"服务返回 HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"密钥被拒绝（HTTP {exc.code}）"
    except urllib.error.URLError as exc:
        return False, f"无法连接服务（{exc.reason}）"
    except OSError as exc:
        return False, f"探针失败（{exc.__class__.__name__}）"


__all__ = [
    "LLMClient",
    "LLMUsage",
    "UsageAccumulator",
    "merge_usage",
    "probe_api_key",
]
