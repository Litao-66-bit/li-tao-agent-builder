"""错误码体系（docs/contracts/04-error-codes.md 的实现）。

4 位数字错误码，前两位为大类。所有错误必须携带 correlation_id 进入审计日志。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── 错误码表（与契约 04 一一对应）───────────────────────────────
ERROR_NAMES: dict[str, int] = {
    "E_PERMISSION": 1000,  # 越权：路径出白名单 / 工具未授权 / 角色越职 → 不重试
    "E_TIMEOUT": 2000,     # 执行超时（工具/步骤/模型调用）→ 重试 1 次
    "E_VALIDATION": 3000,  # 格式/校验失败（DAG 非法、JSON 解析失败、计划含环）→ 计入 retry_count
    "E_MODEL": 4000,       # 模型错误（幻觉被拦截、输出截断、API 报错）→ 截断重试 1 次
    "E_TOOL": 5000,        # 工具异常（检索无结果、依赖安装失败、数据缺失）→ 换通道重试 1 次
    "E_USER_CANCEL": 6000, # 用户中断或拒绝审批 → 转 interrupted，保留 resume_point
    "E_INTERNAL": 9000,    # 内部错误（状态机非法转换、消息类型未知）→ 转 failed
}

# 名称 → 可重试性：仅 E_TIMEOUT/E_VALIDATION/E_MODEL/E_TOOL 允许重试。
# E_PERMISSION 永不重试（越权不是偶然故障，必须人工介入）。
NON_RETRYABLE: frozenset[str] = frozenset({"E_PERMISSION", "E_USER_CANCEL", "E_INTERNAL"})


def is_retryable(error_name: str) -> bool:
    """按错误码表判断是否允许重试。"""
    return error_name not in NON_RETRYABLE


@dataclass(slots=True)
class ErrorInfo:
    """错误消息体（契约 04 · 错误消息体 JSON）。"""

    error_code: int
    error_name: str
    message: str
    source: str
    correlation_id: str
    retryable: bool = True

    @classmethod
    def build(
        cls,
        error_name: str,
        message: str,
        source: str,
        correlation_id: str,
        *,
        error_code: int | None = None,
        retryable: bool | None = None,
    ) -> ErrorInfo:
        """从错误名构造，code/retryable 默认取错误码表。"""
        if error_name not in ERROR_NAMES:
            raise ValueError(f"未知错误名: {error_name!r}，可选: {sorted(ERROR_NAMES)}")
        return cls(
            error_code=error_code if error_code is not None else ERROR_NAMES[error_name],
            error_name=error_name,
            message=message,
            source=source,
            correlation_id=correlation_id,
            retryable=retryable if retryable is not None else is_retryable(error_name),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "error_name": self.error_name,
            "message": self.message,
            "source": self.source,
            "retryable": self.retryable,
            "correlation_id": self.correlation_id,
        }


class AgentError(Exception):
    """主架构统一异常。所有角色抛出的错误都必须包装为 AgentError。"""

    def __init__(
        self,
        error_name: str,
        message: str,
        source: str = "agent_builder",
        correlation_id: str = "c-unknown",
        *,
        error_code: int | None = None,
        retryable: bool | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.info = ErrorInfo.build(
            error_name,
            message,
            source,
            correlation_id,
            error_code=error_code,
            retryable=retryable,
        )
        super().__init__(self.info.message)
        self.__cause__ = cause

    @property
    def error_code(self) -> int:
        return self.info.error_code

    @property
    def error_name(self) -> str:
        return self.info.error_name

    @property
    def retryable(self) -> bool:
        return self.info.retryable

    def to_dict(self) -> dict[str, Any]:
        return self.info.to_dict()


# ── 便捷工厂：按错误码表生成对应错误 ─────────────────────────────

def permission_error(message: str, source: str, correlation_id: str) -> AgentError:
    """E_PERMISSION：越权，永不重试。"""
    return AgentError("E_PERMISSION", message, source, correlation_id, retryable=False)


def timeout_error(message: str, source: str, correlation_id: str) -> AgentError:
    return AgentError("E_TIMEOUT", message, source, correlation_id)


def validation_error(message: str, source: str, correlation_id: str) -> AgentError:
    return AgentError("E_VALIDATION", message, source, correlation_id)


def model_error(message: str, source: str, correlation_id: str) -> AgentError:
    return AgentError("E_MODEL", message, source, correlation_id)


def tool_error(message: str, source: str, correlation_id: str) -> AgentError:
    return AgentError("E_TOOL", message, source, correlation_id)


def user_cancel_error(message: str, source: str, correlation_id: str) -> AgentError:
    return AgentError("E_USER_CANCEL", message, source, correlation_id, retryable=False)


def internal_error(message: str, source: str, correlation_id: str, cause: BaseException | None = None) -> AgentError:
    return AgentError("E_INTERNAL", message, source, correlation_id, retryable=False, cause=cause)


__all__ = [
    "ERROR_NAMES",
    "AgentError",
    "ErrorInfo",
    "internal_error",
    "is_retryable",
    "model_error",
    "permission_error",
    "timeout_error",
    "tool_error",
    "user_cancel_error",
    "validation_error",
]
