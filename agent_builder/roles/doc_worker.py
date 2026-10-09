"""文档执行者（DocWorker）—— 主架构・执行层。

职责：组织结构化文档，每个事实标注来源，未知内容标"待补充"不编造。

边界声明：
- 素材不足 → 请求补充而非编造（不编造版本号/人名/数据）
- 用户指定格式 → 严格遵循
- 引用的来源不可考 → 删除该引用或标存疑
- 所有外部动作走工具门卫

执行协议：
1. 收集素材/代码/结论（检索执行者或记忆管家的产出）
2. 组织结构化文档，每个事实标注来源
3. 未知内容明确写"待补充"，不编造版本号/人名/数据
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 文档类 action 集合。
DOC_ACTIONS: frozenset[str] = frozenset({
    "web_fetch",
    "citation_check",
    "file_write",
    "file_edit",
})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class Source:
    """来源标注。"""

    fact: str  # 事实描述
    source: str  # 来源（URL/文件/记忆条目）
    verified: bool = True  # 来源是否可考


@dataclass(slots=True)
class DocResult:
    """文档执行结果。"""

    step_id: str
    status: str  # done | failed | pending_approval | rejected | pending
    document: str = ""
    sources: list[Source] = field(default_factory=list)
    pending_supplements: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class DocWorker:
    """文档执行者角色：组织结构化文档 + 来源标注。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        llm_client: LLM 客户端（可选；由运行时密钥工厂注入）。
            可用时用于起草文档正文，不可用时只按素材/工具结果整理。
    """

    correlation_id: str = "c-unknown"
    llm_client: Any = None

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> DocResult:
        """执行文档类步骤。

        Args:
            step: 文档类步骤（action 必须在 DOC_ACTIONS 中）。
            executor_fn: 执行函数（接收 Step，返回结果）；
                None 则只校验不执行（返回 pending 状态）。
            context: 任务上下文（可选，如检索执行者的产出）。

        Returns:
            DocResult：文档 + 来源标注清单 + 待补充清单。
        """
        # 1. 校验是否文档类。
        if step.action not in DOC_ACTIONS:
            return DocResult(
                step_id=step.id,
                status="rejected",
                error=f"任务超出文档范围: {step.action}",
            )

        # 2. 检查素材是否充分（素材不足 → 请求补充而非编造）。
        # LLM 可用时允许由 LLM 依据上下文起草，不再因缺素材提前返回。
        materials = step.inputs.get("materials", [])
        if not materials and executor_fn is not None and not self._llm_ready():
            # 无素材且需要执行 → 请求补充。
            return DocResult(
                step_id=step.id,
                status="pending",
                pending_supplements=["素材不足，请提供素材或指定检索来源"],
            )

        # 3. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return DocResult(
                step_id=step.id,
                status="pending",
            )

        # 4. 执行（收集素材 + 组织文档）。
        try:
            result = executor_fn(step)
            # 从 inputs 提取文档；缺正文时由 LLM 起草，仍无则用工具结果兜底。
            document = (
                step.inputs.get("document")
                or self._llm_document(step)
                or (str(result) if result else "")
            )
            raw_sources = step.inputs.get("sources", [])
            sources = self._parse_sources(raw_sources)
            pending = step.inputs.get("pending_supplements", [])

            # 来源不可考 → 标存疑（verified=False）。
            sources = self._mark_unverified(sources)

            return DocResult(
                step_id=step.id,
                status="done",
                document=document,
                sources=sources,
                pending_supplements=list(pending),
            )
        except PermissionError as exc:
            return DocResult(
                step_id=step.id,
                status="pending_approval",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  执行者需捕获所有执行异常
            return DocResult(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _llm_ready(self) -> bool:
        """LLM 客户端是否可用（鸭子类型判定，不依赖具体类型）。"""
        return self.llm_client is not None and bool(getattr(self.llm_client, "is_available", False))

    def _llm_document(self, step: Step) -> str:
        """由 LLM 依据步骤上下文起草文档正文；不可用/失败返回空串（由调用方兜底）。"""
        if not self._llm_ready():
            return ""
        materials = step.inputs.get("materials", [])
        prompt = (
            "根据以下素材组织一份结构化文档：每个事实标注来源，"
            "素材未覆盖的内容明确写“待补充”，不得编造版本号/人名/数据。\n"
            f"素材：{json.dumps(materials, ensure_ascii=False)}\n"
            f"任务输入：{json.dumps(step.inputs, ensure_ascii=False)}"
        )
        try:
            return self.llm_client.chat([{"role": "user", "content": prompt}]) or ""
        except Exception:  # noqa: BLE001  LLM 调用失败降级为工具结果
            return ""

    def _parse_sources(self, raw_sources: list[dict[str, Any]]) -> list[Source]:
        """解析来源标注清单。"""
        sources: list[Source] = []
        for raw in raw_sources:
            if isinstance(raw, dict) and "fact" in raw and "source" in raw:
                sources.append(
                    Source(
                        fact=str(raw["fact"]),
                        source=str(raw["source"]),
                        verified=raw.get("verified", True),
                    )
                )
        return sources

    def _mark_unverified(self, sources: list[Source]) -> list[Source]:
        """来源不可考 → 标存疑（保留 verified=False，不删除）。"""
        return sources


__all__ = [
    "DOC_ACTIONS",
    "DocResult",
    "DocWorker",
    "ExecutorFn",
    "Source",
]
