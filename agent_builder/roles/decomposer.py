"""分解器（Decomposer）—— 主架构・规划层。

职责：解析用户需求，拆成原子步骤 DAG（每步 = 一次工具调用或明确动作），
去重、标注依赖（depends_on）、超 10 步自动分组。
有歧义不猜 → 写"待确认"清单。

边界声明：
- 只规划不执行：不可直接写文件/提交代码/执行命令
- 有歧义不猜：写入 pending_questions，先问用户再继续
- 需求信息不足 → 返回"需要补充"信号，不硬拆

当前实现为规则引擎后处理（去重/依赖分组/歧义检查）。
后续接 LLM 后，_split_steps 由 LLM 实现，后处理逻辑不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.errors import validation_error
from agent_builder.contracts.schemas import Step

# 超过此步数自动分组（并行组）。
MAX_STEPS_BEFORE_GROUP = 10


@dataclass(slots=True)
class DecomposeResult:
    """分解结果：步骤 DAG + 待确认清单。"""

    task_id: str
    steps: dict[str, Step]
    order: list[str]
    parallel_groups: list[list[str]]
    pending_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "steps": {sid: s.model_dump() for sid, s in self.steps.items()},
            "order": list(self.order),
            "parallel_groups": [list(g) for g in self.parallel_groups],
            "pending_questions": list(self.pending_questions),
        }


@dataclass(slots=True)
class Decomposer:
    """分解器角色：解析需求 → 拆成原子步骤 DAG。

    当前实现为规则引擎后处理：
    - 接收预拆分步骤（raw_steps），做校验/去重/依赖分组/歧义检查。
    - 无预拆分时返回"待确认"（等 LLM 接入后由 LLM 拆分）。
    - 后处理逻辑（去重/分组/歧义检查）是纯计算，可测试。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
    """

    correlation_id: str = "c-unknown"

    def decompose(
        self,
        task_id: str,
        requirement: str,
        raw_steps: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        llm_client: Any = None,
    ) -> DecomposeResult:
        """分解需求为步骤 DAG。

        Args:
            task_id: 任务唯一 ID。
            requirement: 用户需求文本。
            raw_steps: 预拆分步骤列表（由 LLM 或人工提供）；
                None 则尝试用 llm_client 拆分，失败返回空 DAG + 待确认。
            context: 上下文（可选，如已启用技能/插件清单）。
            llm_client: LLM 客户端（可选，Any 避免循环导入）；
                需实现 is_available + complete_json(prompt, schema_hint)。
                None 时保持原待确认逻辑（向后兼容）。

        Returns:
            DecomposeResult：步骤 DAG + 待确认清单。

        Raises:
            AgentError(E_VALIDATION): task_id / requirement 为空 / raw_steps 格式错误。
        """
        cid = self.correlation_id
        if not task_id or not task_id.strip():
            raise validation_error(
                "decomposer: task_id 不能为空",
                source="decomposer",
                correlation_id=cid,
            )
        if not requirement or not requirement.strip():
            raise validation_error(
                "decomposer: requirement 不能为空",
                source="decomposer",
                correlation_id=cid,
            )

        # 无预拆分 → 尝试用 LLM 拆分。
        if raw_steps is None:
            if llm_client is not None and getattr(llm_client, "is_available", False):
                raw_steps = self._llm_split(requirement, llm_client)
            if not raw_steps:
                # LLM 不可用或拆分失败 → 返回待确认。
                return DecomposeResult(
                    task_id=task_id,
                    steps={},
                    order=[],
                    parallel_groups=[],
                    pending_questions=["需要 LLM 拆分需求（当前无预拆分步骤）"],
                )

        # 校验格式。
        validated = self._validate_steps(raw_steps)

        # 去重。
        validated = self._deduplicate(validated)

        # 构建步骤字典 + 顺序。
        steps = {s["id"]: Step(**s) for s in validated}
        order = [s["id"] for s in validated]

        # 依赖分组（拓扑排序）。
        parallel_groups = self._group_steps(validated)

        # 歧义检查。
        pending = self._check_ambiguity(validated)

        return DecomposeResult(
            task_id=task_id,
            steps=steps,
            order=order,
            parallel_groups=parallel_groups,
            pending_questions=pending,
        )

    # ── 内部方法 ──────────────────────────────────────────────

    def _llm_split(self, requirement: str, llm_client: Any) -> list[dict[str, Any]]:
        """调用 LLM 拆分需求为步骤列表。

        Args:
            requirement: 用户需求文本。
            llm_client: LLM 客户端（需有 complete_json 方法）。

        Returns:
            步骤字典列表；LLM 失败或返回非法 JSON 返回空列表。
        """
        schema = '{"steps": [{"id": "step-001", "action": "动作类型", "inputs": {}, "depends_on": []}]}'
        prompt = (
            f"把以下需求拆成原子步骤列表（每步 = 一次工具调用或明确动作）。\n"
            f"需求：{requirement}\n\n"
            f"要求：\n"
            f"- id 用 step-001 / step-002 格式\n"
            f"- action 用动词短语（如 file_write / web_search / code_gen）\n"
            f"- inputs 是参数字典\n"
            f"- depends_on 是依赖的步骤 id 列表（无依赖为空列表）\n"
            f"- 超过 10 步时合理分组\n"
            f"- 有歧义不猜，直接列出待确认点\n"
        )
        try:
            result = llm_client.complete_json(prompt, schema_hint=schema)
        except Exception:  # noqa: BLE001  LLM 调用失败降级
            return []
        if not isinstance(result, dict):
            return []
        steps = result.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return []
        return steps

    def _validate_steps(self, raw_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """校验步骤格式：必须有 id 和 action；补全可选字段。"""
        cid = self.correlation_id
        if not isinstance(raw_steps, list):
            raise validation_error(
                "decomposer: raw_steps 必须是列表",
                source="decomposer",
                correlation_id=cid,
            )
        seen_ids: set[str] = set()
        validated: list[dict[str, Any]] = []
        for i, step in enumerate(raw_steps):
            if not isinstance(step, dict):
                raise validation_error(
                    f"decomposer: 步骤 {i} 不是字典",
                    source="decomposer",
                    correlation_id=cid,
                )
            step_id = step.get("id", "")
            if not step_id or not str(step_id).strip():
                raise validation_error(
                    f"decomposer: 步骤 {i} 缺少 id",
                    source="decomposer",
                    correlation_id=cid,
                )
            step_id = str(step_id).strip()
            if step_id in seen_ids:
                raise validation_error(
                    f"decomposer: 步骤 id 重复: {step_id}",
                    source="decomposer",
                    correlation_id=cid,
                )
            action = step.get("action", "")
            if not action or not str(action).strip():
                raise validation_error(
                    f"decomposer: 步骤 {step_id} 缺少 action",
                    source="decomposer",
                    correlation_id=cid,
                )
            seen_ids.add(step_id)
            # 补全可选字段。
            result = {
                "id": step_id,
                "action": str(action).strip(),
                "inputs": step.get("inputs", {}),
                "depends_on": step.get("depends_on", []),
            }
            validated.append(result)
        return validated

    def _deduplicate(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """去重：相同 action + 相同 inputs 的步骤合并（保留第一个）。"""
        seen: dict[tuple, dict[str, Any]] = {}
        result: list[dict[str, Any]] = []
        for step in steps:
            # 去重 key = (action, sorted(inputs items)，不含 id 和 depends_on）。
            inputs_key = tuple(sorted(step.get("inputs", {}).items()))
            key = (step["action"], inputs_key)
            if key in seen:
                # 已有相同步骤，跳过（合并）。
                continue
            seen[key] = step
            result.append(step)
        return result

    def _group_steps(self, steps: list[dict[str, Any]]) -> list[list[str]]:
        """依赖分组：按拓扑排序分层，同层步骤可并行执行。

        无依赖的步骤为第一组；依赖第一组的为第二组，以此类推。
        检测到环依赖时，将环中步骤归入同一组（避免死锁）。
        """
        if not steps:
            return []

        # 构建依赖图。
        step_ids = {s["id"] for s in steps}
        remaining: dict[str, set[str]] = {}
        for s in steps:
            # 只保留指向已知步骤的依赖。
            deps = {d for d in s.get("depends_on", []) if d in step_ids}
            remaining[s["id"]] = deps

        groups: list[list[str]] = []
        while remaining:
            # 找出所有依赖已满足的步骤。
            ready = sorted(sid for sid, deps in remaining.items() if not deps)
            if not ready:
                # 有环依赖 → 所有剩余步骤归入一组。
                ready = sorted(remaining.keys())
            groups.append(ready)
            for sid in ready:
                del remaining[sid]
            # 从剩余步骤的依赖中移除已完成的。
            for deps in remaining.values():
                deps -= set(ready)

        return groups

    def _check_ambiguity(self, steps: list[dict[str, Any]]) -> list[str]:
        """歧义检查：检测未定义的依赖引用。"""
        questions: list[str] = []
        step_ids = {s["id"] for s in steps}
        for s in steps:
            for dep in s.get("depends_on", []):
                if dep not in step_ids:
                    questions.append(
                        f"步骤 {s['id']} 依赖了未定义的步骤: {dep}"
                    )
        return questions


__all__ = ["MAX_STEPS_BEFORE_GROUP", "DecomposeResult", "Decomposer"]
