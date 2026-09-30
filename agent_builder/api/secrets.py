"""API 密钥运行时存储 —— 仅驻留进程内存，绝不落盘、绝不回显。

安全约束（P0）：
1. 密钥只保存在进程内存中，进程退出即失效；不写入磁盘、日志或审计记录。
2. 对外接口只暴露「是否已配置」与掩码提示，任何响应都不包含密钥明文。
3. 写入前做最小校验（非空 / 长度 / 无空白与控制字符），避免密钥被注入到
   HTTP 头或日志换行中。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MIN_KEY_LEN = 8
MAX_KEY_LEN = 512

# 禁止出现在密钥中的字符：空白与控制字符（防止注入 HTTP 头 / 日志换行）。
_FORBIDDEN_RE = re.compile(r"[\s\x00-\x1f\x7f]")


class InvalidApiKeyError(ValueError):
    """密钥格式不合法。

    注意：本异常的消息只描述校验失败原因，绝不包含密钥原文。
    """


class ApiKeyStore:
    """进程内 API 密钥存储。"""

    def __init__(self) -> None:
        self._api_key: str | None = None

    def set_key(self, raw: str | None) -> None:
        """校验并存入手写密钥；非法输入抛 InvalidApiKeyError。

        Args:
            raw: 用户提交的密钥明文。

        Raises:
            InvalidApiKeyError: 为空、长度越界或含空白/控制字符。
        """
        if raw is None:
            raise InvalidApiKeyError("密钥不能为空")
        key = raw.strip()
        if not key:
            raise InvalidApiKeyError("密钥不能为空")
        if len(key) < MIN_KEY_LEN:
            raise InvalidApiKeyError(f"密钥长度不足（至少 {MIN_KEY_LEN} 位）")
        if len(key) > MAX_KEY_LEN:
            raise InvalidApiKeyError(f"密钥过长（最多 {MAX_KEY_LEN} 位）")
        if _FORBIDDEN_RE.search(key):
            raise InvalidApiKeyError("密钥不能包含空白或控制字符")
        self._api_key = key
        # 日志只记掩码，不记原文。
        logger.info("已更新运行时 API 密钥（掩码 %s）", self.masked_hint())

    def clear(self) -> None:
        """清除已保存的密钥。"""
        self._api_key = None
        logger.info("已清除运行时 API 密钥")

    @property
    def is_configured(self) -> bool:
        """是否已保存密钥。"""
        return self._api_key is not None

    def get(self) -> str | None:
        """取用密钥明文。

        仅供进程内调用方（如 LLM 客户端工厂）使用，禁止直接对外暴露。
        """
        return self._api_key

    def masked_hint(self) -> str | None:
        """掩码提示，例如 ``sk-***``；未配置返回 None。

        只保留前缀 3 位，其余全部隐去，绝不可用于还原原文。
        """
        if self._api_key is None:
            return None
        head = self._api_key[:3] if len(self._api_key) >= 3 else ""
        return f"{head}***"


# ── 模块级单例 ──
_store: ApiKeyStore | None = None


def get_api_key_store() -> ApiKeyStore:
    """获取单例密钥存储。"""
    global _store
    if _store is None:
        _store = ApiKeyStore()
    return _store


def reset_api_key_store() -> None:
    """重置单例（测试用）。"""
    global _store
    _store = None


__all__ = [
    "ApiKeyStore",
    "InvalidApiKeyError",
    "get_api_key_store",
    "reset_api_key_store",
]
