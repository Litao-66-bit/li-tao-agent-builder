"""敏感信息脱敏 —— P0-3「日志/审计脱敏」。

原则：审计日志与错误信息里绝不能原样留存密钥、令牌、口令等敏感值。
覆盖两类：
1. 键名命中敏感模式（api_key / token / secret / password / authorization / credential…）
   → 值整体打码；
2. 字符串值命中常见密钥形态（sk-… / ghp_… / AKIA… / BEGIN PRIVATE KEY… / xox…）
   → 值打码（保留前 4 位便于对账）。

脱敏是递归的：dict 键、list 元素、嵌套结构全覆盖。
"""

from __future__ import annotations

import re
from typing import Any

# 键名命中即打码（子串匹配，大小写不敏感，-/_ 归一化）。
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "api",
    "key",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "auth",
    "credential",
    "private",
    "cookie",
    "session",
    "sig",
)

# 值形态命中即打码（常见密钥/凭据格式）。
_SECRET_VALUE_RE: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),  # OpenAI / DeepSeek 风格
    re.compile(r"ghp_[A-Za-z0-9]{16,}"),  # GitHub PAT
    re.compile(r"gho_|ghu_|ghs_", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS Access Key
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack token
    re.compile(r"-----BEGIN (RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----"),  # PEM 私钥
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),  # Google API key
    re.compile(r"[A-Za-z0-9]{32,}"),  # 兜底：超长无空格 token 串（保守，仅命中整串形态）
)


def is_sensitive_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(part in k for part in SENSITIVE_KEY_PARTS)


def _match_secret(value: str) -> bool:
    return any(pat.search(value) for pat in _SECRET_VALUE_RE)


def mask(value: str, *, keep: int = 4) -> str:
    """打码：保留前 keep 位，其余替换为 ***。"""
    if not value:
        return "***"
    return f"{value[:keep]}***[redacted]"


def redact_value(value: Any, *, key: str | None = None) -> Any:
    """递归脱敏：返回脱敏后的副本，不修改原对象。"""
    if isinstance(value, dict):
        return {k: redact_value(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, key=key) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v, key=key) for v in value)
    if isinstance(value, str):
        if (key and is_sensitive_key(key)) or _match_secret(value):
            return mask(value)
        return value
    return value


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """门卫审计入口：工具调用参数先脱敏再落审计日志。"""
    return redact_value(args)


__all__ = ["is_sensitive_key", "mask", "redact_args", "redact_value"]
