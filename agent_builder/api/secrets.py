"""API 密钥运行时存储 —— 仅驻留进程内存，绝不落盘、绝不回显。

安全约束（P0）：
1. 密钥只保存在进程内存中，进程退出即失效；不写入磁盘、日志或审计记录。
2. 对外接口只暴露「是否已配置」、掩码提示与非可逆指纹，任何响应都不包含密钥明文。
3. 写入前做最小校验（非空 / 长度 / 无空白与控制字符），避免密钥被注入到
   HTTP 头或日志换行中。
4. 另提供 sha256 前 8 位的非可逆指纹，仅供「是不是同一把钥匙」核对。
5. 支持 TTL（到期自动失效）与轮换（用新密钥替换旧密钥并记录轮换元数据）；
   到期的第一时刻即清除明文，只保留掩码/指纹等派生值，保证状态可解释且不残留明文。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

MIN_KEY_LEN = 8
MAX_KEY_LEN = 512

# 非可逆指纹长度（sha256 十六进制前缀位数）。
FINGERPRINT_LEN = 8

# TTL 上限：30 天（避免误设超长有效期）。
MAX_TTL_S = 30 * 24 * 3600

# 禁止出现在密钥中的字符：空白与控制字符（防止注入 HTTP 头 / 日志换行）。
_FORBIDDEN_RE = re.compile(r"[\s\x00-\x1f\x7f]")


class InvalidApiKeyError(ValueError):
    """密钥格式不合法。

    注意：本异常的消息只描述校验失败原因，绝不包含密钥原文。
    """


class InvalidTtlError(ValueError):
    """TTL 取值不合法（必须为正数且不超过上限）。"""


class NoRotatableKeyError(ValueError):
    """当前没有可轮换的密钥（未配置/已过期）。"""


def validate_api_key(raw: str | None) -> str:
    """校验并规范化密钥明文；非法输入抛 InvalidApiKeyError。

    Args:
        raw: 用户提交的密钥明文。

    Returns:
        去掉首尾空白后的密钥。

    Raises:
        InvalidApiKeyError: 为空、长度越界或含空白/控制字符；消息只含原因。
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
    return key


def validate_ttl(ttl_s: float | None) -> float | None:
    """校验 TTL（秒）；None 表示不过期。

    Args:
        ttl_s: 生存期秒数。

    Returns:
        规范化后的 TTL；入参为 None 时返回 None。

    Raises:
        InvalidTtlError: 非正数或超过 MAX_TTL_S。
    """
    if ttl_s is None:
        return None
    if ttl_s <= 0:
        raise InvalidTtlError("ttl 必须为正数（秒）")
    if ttl_s > MAX_TTL_S:
        raise InvalidTtlError(f"ttl 过大（最多 {MAX_TTL_S} 秒，即 30 天）")
    return float(ttl_s)


class ApiKeyStore:
    """进程内 API 密钥存储（支持 TTL 与轮换）。

    过期语义：到期后 `is_configured` 为 False、`get()` 返回 None，
    且明文会在首次判定到期时被清除；掩码与指纹作为非敏感派生值保留，
    以便界面提示「哪把钥匙过期了」。
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._api_key: str | None = None
        self._expires_at: float | None = None
        # 过期清除明文后保留的派生值（非敏感）。
        self._masked_cache: str | None = None
        self._fingerprint_cache: str | None = None
        self._rotated_at: float | None = None
        self._rotation_count = 0

    # ── 内部 ────────────────────────────────────────────────────

    def _now(self) -> float:
        return self._clock()

    def _expire_if_needed(self) -> None:
        """到期即清除明文，只保留掩码/指纹（幂等）。"""
        if self._api_key is None or not self.is_expired:
            return
        masked = self.masked_hint()
        fingerprint = self.fingerprint()
        self._api_key = None
        self._masked_cache = masked
        self._fingerprint_cache = fingerprint
        logger.info("运行时 API 密钥已过期并清除明文（掩码 %s，指纹 %s）", masked, fingerprint)

    # ── 写入 ────────────────────────────────────────────────────

    def set_key(self, raw: str | None, *, ttl_s: float | None = None) -> None:
        """校验并存入手写密钥；非法输入抛 InvalidApiKeyError / InvalidTtlError。

        Args:
            raw: 用户提交的密钥明文。
            ttl_s: 生存期秒数；None 表示不过期。

        Raises:
            InvalidApiKeyError: 为空、长度越界或含空白/控制字符。
            InvalidTtlError: TTL 非正数或超上限。
        """
        key = validate_api_key(raw)
        ttl = validate_ttl(ttl_s)
        self._api_key = key
        self._expires_at = None if ttl is None else self._now() + ttl
        self._masked_cache = None
        self._fingerprint_cache = None
        # 日志只记掩码与指纹，不记原文。
        logger.info(
            "已更新运行时 API 密钥（掩码 %s，指纹 %s，ttl=%s）",
            self.masked_hint(),
            self.fingerprint(),
            "无过期" if ttl is None else f"{ttl:g}s",
        )

    def rotate(self, raw: str | None, *, ttl_s: float | None = None) -> str:
        """用新密钥替换当前密钥，返回**被替换掉的旧密钥指纹**。

        轮换要求当前存在有效密钥；未配置或已过期时抛 NoRotatableKeyError
        （此时应改用 set_key 首次设置）。

        Args:
            raw: 新密钥明文。
            ttl_s: 新密钥的生存期秒数；None 表示不过期。

        Returns:
            旧密钥的指纹（非可逆，供调用方核对确实换掉了哪把钥匙）。

        Raises:
            NoRotatableKeyError: 当前没有可轮换的密钥。
            InvalidApiKeyError: 新密钥格式非法。
            InvalidTtlError: TTL 非法。
        """
        self._expire_if_needed()
        if self._api_key is None:
            raise NoRotatableKeyError("当前没有可轮换的密钥；请先设置密钥再轮换")
        previous = self.fingerprint()
        self.set_key(raw, ttl_s=ttl_s)
        self._rotation_count += 1
        self._rotated_at = self._now()
        logger.info(
            "已轮换运行时 API 密钥（旧指纹 %s → 新指纹 %s，累计 %d 次）",
            previous,
            self.fingerprint(),
            self._rotation_count,
        )
        return str(previous)

    def clear(self) -> None:
        """清除已保存的密钥及其轮换/过期元数据。"""
        self._api_key = None
        self._expires_at = None
        self._masked_cache = None
        self._fingerprint_cache = None
        self._rotated_at = None
        self._rotation_count = 0
        logger.info("已清除运行时 API 密钥")

    # ── 读取 ────────────────────────────────────────────────────

    @property
    def is_configured(self) -> bool:
        """是否已保存**且未过期**的密钥（会顺带清除到期明文）。"""
        self._expire_if_needed()
        return self._api_key is not None

    @property
    def is_expired(self) -> bool:
        """是否已到期（纯判定，不做清理）。"""
        return self._expires_at is not None and self._now() >= self._expires_at

    @property
    def expires_at(self) -> float | None:
        """到期时间戳（秒）；不过期返回 None。"""
        return self._expires_at

    @property
    def remaining_s(self) -> int | None:
        """距到期的剩余秒数（向上取整，不小于 0）；不过期或未配置返回 None。"""
        if self._expires_at is None or self._api_key is None:
            return None
        return max(0, math.ceil(self._expires_at - self._now()))

    @property
    def rotation_count(self) -> int:
        """当前密钥的累计轮换次数。"""
        return self._rotation_count

    @property
    def rotated_at(self) -> float | None:
        """最近一次轮换的时间戳（秒）；未轮换过返回 None。"""
        return self._rotated_at

    def get(self) -> str | None:
        """取用密钥明文；已过期视为未配置并清除明文。

        仅供进程内调用方（如 LLM 客户端工厂）使用，禁止直接对外暴露。
        """
        self._expire_if_needed()
        return self._api_key

    def masked_hint(self) -> str | None:
        """掩码提示，例如 ``sk-***``；未配置返回 None。

        只保留前缀 3 位，其余全部隐去，绝不可用于还原原文。
        """
        if self._api_key is not None:
            head = self._api_key[:3] if len(self._api_key) >= 3 else ""
            return f"{head}***"
        return self._masked_cache

    def fingerprint(self) -> str | None:
        """密钥的非可逆指纹（sha256 前 8 位十六进制）；未配置返回 None。

        仅用于「是不是同一把钥匙」核对：同一密钥恒等，不同密钥碰撞概率极低，
        且无法由指纹或掩码反推原文。
        """
        if self._api_key is None:
            return self._fingerprint_cache
        digest = hashlib.sha256(self._api_key.encode("utf-8")).hexdigest()
        return digest[:FINGERPRINT_LEN]


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
    "MAX_TTL_S",
    "ApiKeyStore",
    "InvalidApiKeyError",
    "InvalidTtlError",
    "NoRotatableKeyError",
    "get_api_key_store",
    "reset_api_key_store",
    "validate_api_key",
    "validate_ttl",
]
