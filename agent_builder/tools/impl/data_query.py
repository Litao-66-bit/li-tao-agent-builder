"""data_query：数据读取/计算（CSV + JSON）。

安全边界：path 沙箱校验由 ToolGatekeeper._check_sandbox_path 负责。
本模块只做纯逻辑：格式检测、数据读取、截断、异常映射。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from agent_builder.contracts.errors import tool_error, validation_error
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 默认返回行数/条目数。
DEFAULT_LIMIT = 100
# 单次查询最大行数/条目数。
MAX_ROWS = 1000


def query_data(path: str, limit: int = DEFAULT_LIMIT) -> str:
    """读取 CSV 或 JSON 数据（只读）。

    Args:
        path: 数据文件路径（已由门卫校验落在 WORKSPACE_DIR 内）。
        limit: 返回的最大行数/条目数，超过 MAX_ROWS 自动截断。

    Returns:
        CSV: 每行一个制表符分隔的记录。
        JSON: 格式化的 JSON 文本（数组截断到 limit 条）。

    Raises:
        AgentError(E_VALIDATION): path 为空 / limit 非法 / 文件不存在 / 格式不支持。
        AgentError(E_TOOL): 读取失败 / 解析失败。
    """
    cid = current_correlation_id.get()
    if not path or not path.strip():
        raise validation_error(
            "data_query: path 不能为空", source="tool.data_query", correlation_id=cid
        )
    if limit <= 0:
        raise validation_error(
            f"data_query: limit 必须为正数: {limit}",
            source="tool.data_query",
            correlation_id=cid,
        )
    limit = min(limit, MAX_ROWS)

    p = Path(path)
    if not p.exists():
        raise validation_error(
            f"data_query: 文件不存在: {path}",
            source="tool.data_query",
            correlation_id=cid,
        )
    if not p.is_file():
        raise validation_error(
            f"data_query: 不是文件: {path}",
            source="tool.data_query",
            correlation_id=cid,
        )

    suffix = p.suffix.lower()
    if suffix == ".json":
        return _read_json(p, limit, cid)
    if suffix == ".csv":
        return _read_csv(p, limit, cid)
    raise validation_error(
        f"data_query: 不支持的格式 {suffix!r}（仅 .csv / .json）",
        source="tool.data_query",
        correlation_id=cid,
    )


def _read_csv(path: Path, limit: int, cid: str) -> str:
    """读取 CSV 文件，返回制表符分隔的行。"""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows: list[list[str]] = []
            truncated = False
            for _i, row in enumerate(reader):
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append(row)
    except OSError as exc:
        raise tool_error(
            f"data_query: 读取 CSV 失败: {exc}",
            source="tool.data_query",
            correlation_id=cid,
        ) from exc
    except csv.Error as exc:
        raise tool_error(
            f"data_query: CSV 解析失败: {exc}",
            source="tool.data_query",
            correlation_id=cid,
        ) from exc

    if not rows:
        return "(empty CSV)"
    lines = ["\t".join(row) for row in rows]
    if truncated:
        lines.append(f"…[已截断，仅显示前 {limit} 行]")
    return "\n".join(lines)


def _read_json(path: Path, limit: int, cid: str) -> str:
    """读取 JSON 文件，返回格式化文本（数组截断到 limit 条）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        raise tool_error(
            f"data_query: 读取 JSON 失败: {exc}",
            source="tool.data_query",
            correlation_id=cid,
        ) from exc
    except json.JSONDecodeError as exc:
        raise tool_error(
            f"data_query: JSON 解析失败: {exc}",
            source="tool.data_query",
            correlation_id=cid,
        ) from exc

    truncated = False
    if isinstance(data, list) and len(data) > limit:
        data = data[:limit]
        truncated = True

    text = json.dumps(data, ensure_ascii=False, indent=2)
    if truncated:
        text += f"\n…[已截断，仅显示前 {limit} 条]"
    return text


spec = ToolSpec(
    name="data_query",
    description="读取 CSV/JSON 数据（只读，支持行数截断）",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "数据文件路径（.csv 或 .json）"},
            "limit": {
                "type": "integer",
                "description": "返回的最大行数/条目数",
                "default": DEFAULT_LIMIT,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=15.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, query_data)


__all__ = ["DEFAULT_LIMIT", "MAX_ROWS", "query_data", "spec"]
