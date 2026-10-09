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

import json
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.errors import validation_error
from agent_builder.contracts.schemas import Step
from agent_builder.narrate import describe_step

# 超过此步数自动分组（并行组）。
MAX_STEPS_BEFORE_GROUP = 10

# 产出详细度（对应前端「高级」下拉）：影响 LLM 拆分提示词。
VALID_DETAIL_LEVELS = ("default", "detailed", "concise")

_DETAIL_HINTS: dict[str, str] = {
    "detailed": (
        "- 详细模式：每步 inputs 必须写全（键名 + 取值），并额外给出 expected 键说明该步产出\n"
        "- 尽量拆到可独立验证的原子粒度，不要合并步骤\n"
    ),
    "concise": (
        "- 精简模式：合并同类步骤，总步数控制在 5 步以内\n"
        "- 只保留关键路径，省略解释性/过渡性步骤\n"
    ),
}


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
        detail_level: str = "default",
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
            detail_level: 产出详细度 default | detailed | concise（前端「高级」下拉），
                仅影响 LLM 拆分的提示词，不改变去重/分组/歧义检查等后处理。

        Returns:
            DecomposeResult：步骤 DAG + 待确认清单。

        Raises:
            AgentError(E_VALIDATION): task_id / requirement 为空、raw_steps 格式错误
                或 detail_level 非法。
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
        if detail_level not in VALID_DETAIL_LEVELS:
            raise validation_error(
                f"decomposer: detail_level 非法 {detail_level!r}，可选: {list(VALID_DETAIL_LEVELS)}",
                source="decomposer",
                correlation_id=cid,
            )

        # 无预拆分 → 尝试用 LLM 拆分（同时回收模型显式列出的待确认点）。
        llm_pending: list[str] = []
        if raw_steps is None:
            if llm_client is not None and getattr(llm_client, "is_available", False):
                raw_steps, llm_pending = self._llm_split(requirement, llm_client, detail_level)
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

        # 去重（并重写因去重而悬空的 depends_on）。
        validated = self._deduplicate(validated)

        # 构建步骤字典 + 顺序。
        steps = {s["id"]: Step(**s) for s in validated}
        order = [s["id"] for s in validated]

        # 依赖分组（拓扑排序）。
        parallel_groups = self._group_steps(validated)

        # 待确认清单 = 模型显式列出的待确认点 + 结构歧义（悬空依赖），保序去重。
        pending = self._merge_pending(llm_pending, self._check_ambiguity(validated))

        return DecomposeResult(
            task_id=task_id,
            steps=steps,
            order=order,
            parallel_groups=parallel_groups,
            pending_questions=pending,
        )

    # ── 内部方法 ──────────────────────────────────────────────

    def _llm_split(
        self, requirement: str, llm_client: Any, detail_level: str = "default"
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """调用 LLM 拆分需求为步骤列表 + 待确认点。

        Args:
            requirement: 用户需求文本。
            llm_client: LLM 客户端（需有 complete_json 方法）。
            detail_level: 产出详细度；detailed / concise 会在提示词末尾追加对应要求。

        Returns:
            ``(步骤字典列表, 待确认点列表)``；LLM 失败或返回非法 JSON 返回 ``([], [])``。

        Note:
            ``action`` 的示例一律用**系统真实存在的工具名**（此前误举 `code_gen`，
            该系统并不存在，会诱导模型产出未授权 action）；并显式**禁止编造工具**
            （如 `file_delete` / `file_rename`），要求无对应工具时改写或转待确认点。
        """
        schema = (
            '{"steps": [{"id": "step-001", "action": "系统工具名", "inputs": {}, '
            '"depends_on": []}], "pending_questions": ["有歧义时在此列出待确认点"]}'
        )
        prompt = (
            f"把以下需求拆成原子步骤列表（每步 = 一次工具调用或明确动作）。\n"
            f"需求：{requirement}\n\n"
            f"要求：\n"
            f"- id 用 step-001 / step-002 格式\n"
            f"- action 必须用系统已有工具名（file_read / file_write / file_delete / "
            f"file_list / code_search / web_search / web_fetch / sandbox_run / test_run / "
            f"data_query / citation_check 等）；严禁编造系统不存在的工具名\n"
            f"- 若某操作没有对应工具，不要产出该步骤：在可用工具内改写"
            f"（例：改名 = file_read + file_write + file_delete），或把该诉求写进 pending_questions\n"
            f"- file_write 覆盖已存在文件时，必须在 inputs 里显式给 overwrite=true"
            f"（默认 false 会拒绝覆盖）\n"
            f"- inputs 是参数字典\n"
            f"- depends_on 只能引用本次列出的步骤 id（无依赖为空列表）\n"
            f"- 超过 10 步时合理分组\n"
            f"- 有歧义不猜，把待确认点写进 pending_questions（不要用假设代替）\n"
        )
        prompt += _DETAIL_HINTS.get(detail_level, "")
        try:
            result = llm_client.complete_json(prompt, schema_hint=schema)
        except Exception:  # noqa: BLE001  LLM 调用失败降级
            return [], []
        if not isinstance(result, dict):
            return [], []
        steps = result.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return [], []
        raw_pending = result.get("pending_questions")
        pending = (
            [str(item).strip() for item in raw_pending if str(item).strip()]
            if isinstance(raw_pending, list)
            else []
        )
        return steps, pending

    @staticmethod
    def _merge_pending(llm_pending: list[str], struct_pending: list[str]) -> list[str]:
        """合并「模型列出的待确认点」与「结构歧义」，保序去重。"""
        merged: list[str] = []
        for item in [*llm_pending, *struct_pending]:
            if item and item not in merged:
                merged.append(item)
        return merged

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
            # 补全可选字段 + 人读标题/描述（英文 action 与 key=value 不外泄给用户）。
            step_inputs = step.get("inputs", {})
            title, description = describe_step(str(action).strip(), step_inputs)
            result = {
                "id": step_id,
                "action": str(action).strip(),
                "title": title,
                "description": description,
                "inputs": step_inputs,
                "depends_on": step.get("depends_on", []),
            }
            validated.append(result)
        return validated

    def _deduplicate(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """去重：相同 action + 相同 inputs 的步骤合并（保留第一个），并重写依赖。

        去重键用确定性 JSON 序列化 inputs，避免 inputs 含 list / dict 等
        不可哈希值时 `tuple(...)` 抛 TypeError（LLM 常产出列表型参数，如文件清单）。

        被合并掉步骤的 id 可能被其它步骤的 ``depends_on`` 引用；去重后统一把
        「被删 id → 保留 id」重写进依赖，避免出现悬空依赖（曾致 `/plan` 409）。
        """
        kept_by_key: dict[tuple[str, str], str] = {}
        alias: dict[str, str] = {}
        result: list[dict[str, Any]] = []
        for step in steps:
            # 去重 key = (action, 确定性 JSON 化的 inputs)，不含 id 和 depends_on。
            inputs_key = json.dumps(
                step.get("inputs", {}), sort_keys=True, ensure_ascii=False, default=str
            )
            key = (step["action"], inputs_key)
            keeper = kept_by_key.get(key)
            if keeper is not None:
                # 已有相同步骤 → 合并，并记录 id 别名供依赖重写。
                alias[step["id"]] = keeper
                continue
            kept_by_key[key] = step["id"]
            result.append(step)

        if alias:
            for step in result:
                rewritten: list[str] = []
                for dep in step.get("depends_on", []) or []:
                    target = alias.get(dep, dep)
                    # 去掉自依赖与重复依赖，保持原顺序。
                    if target != step["id"] and target not in rewritten:
                        rewritten.append(target)
                step["depends_on"] = rewritten
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
