"""metric_collect：采集运行指标。

安全边界：只读审计日志并聚合统计，无副作用，无审批。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from agent_builder.contracts.errors import validation_error
from agent_builder.tools.impl.audit_store import load_audit
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 支持的统计维度。
VALID_SCOPES = frozenset({"summary", "by_action", "by_role", "by_status"})
# 占位状态值：审计日志条目无 status 字段时计入 unknown。
_UNKNOWN = "unknown"


def collect_metrics(scope: str = "summary", window: int = 0) -> str:
    """采集运行指标。

    Args:
        scope: 统计维度 ``summary`` / ``by_action`` / ``by_role`` / ``by_status``。
        window: 统计时间窗口（秒），0 = 全部历史。

    Returns:
        统计文本。summary 返回总览；其他维度返回逐项计数。

    Raises:
        AgentError(E_VALIDATION): scope 非法 / window 为负。
        AgentError(E_TOOL): 审计日志读取失败（由 audit_store 抛出）。
    """
    cid = current_correlation_id.get()
    if scope not in VALID_SCOPES:
        raise validation_error(
            f"metric_collect: scope 非法 {scope!r}，可选: {sorted(VALID_SCOPES)}",
            source="tool.metric_collect",
            correlation_id=cid,
        )
    if window < 0:
        raise validation_error(
            f"metric_collect: window 不能为负数: {window}",
            source="tool.metric_collect",
            correlation_id=cid,
        )

    entries = load_audit()

    # 按时间窗口过滤。
    if window > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
        entries = [e for e in entries if _parse_ts(e.get("ts", "")) >= cutoff]

    if scope == "summary":
        return _summary(entries)
    if scope == "by_action":
        return _by_field(entries, "action", "动作")
    if scope == "by_role":
        return _by_field(entries, "role", "角色")
    return _by_field(entries, "status", "状态")


def _parse_ts(ts: str) -> datetime:
    """解析 ISO 时间戳，失败返回 epoch（视为最早，不被过滤掉）。"""
    if not ts:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _summary(entries: list[dict]) -> str:
    """总览：总数 + 各维度 top-3。"""
    total = len(entries)
    actions = Counter(e.get("action", _UNKNOWN) for e in entries)
    roles = Counter(e.get("role", _UNKNOWN) for e in entries)
    lines = [f"total: {total}"]
    if actions:
        top_actions = ", ".join(f"{a}={n}" for a, n in actions.most_common(3))
        lines.append(f"top_actions: {top_actions}")
    if roles:
        top_roles = ", ".join(f"{r}={n}" for r, n in roles.most_common(3))
        lines.append(f"top_roles: {top_roles}")
    return "\n".join(lines)


def _by_field(entries: list[dict], field: str, label: str) -> str:
    """按字段逐项计数。"""
    counts = Counter(e.get(field, _UNKNOWN) for e in entries)
    if not counts:
        return f"(no {label} data)"
    return "\n".join(f"{k}: {v}" for k, v in counts.most_common())


spec = ToolSpec(
    name="metric_collect",
    description="采集运行指标（从审计日志聚合统计）",
    parameters={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["summary", "by_action", "by_role", "by_status"],
                "description": "统计维度",
                "default": "summary",
            },
            "window": {
                "type": "integer",
                "description": "统计时间窗口（秒），0 = 全部历史",
                "default": 0,
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, collect_metrics)


__all__ = ["VALID_SCOPES", "collect_metrics", "spec"]
