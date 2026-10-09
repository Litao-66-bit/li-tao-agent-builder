"""副结构提议的脚手架生成 —— 回答「批准之后到底落盘什么」。

边界（与"半自动"的口径一致）：
- 生成物**只写入任务工作区的** ``proposals/<proposal_id>/`` 目录（目录镜像 + 并入清单），
  **绝不改仓库既有文件**：``tools/permissions.py``（授权）、``tools/registry.py``（工具注册）、
  ``docs/execution-protocols.md``（协议文档）一律留人工合并 —— 自动改这三处等于
  让一次模型提议直接获得执行权限，越过了「新角色三道门」。
- 因此新角色在人工并入授权之前**天然不可派发**：这正是"批准 ≠ 立刻获得权限"的落地方式。
- 所有目标路径都做工作区沙箱校验（与 ``ToolGatekeeper`` 同口径的"不得越界"原则），
  且因为落在独立的 ``proposals/<id>/`` 下，不存在覆盖用户已有文件的风险。

生成物（相对工作区）::

    proposals/<proposal_id>/README.md                        并入清单（先读这个）
    proposals/<proposal_id>/agent_builder/roles/<name>.py    角色模块骨架
    proposals/<proposal_id>/tests/test_roles_<name>.py       测试脚手架（≥10 例）
    proposals/<proposal_id>/docs/execution-protocols.addon.md 协议文档章节草稿
    proposals/<proposal_id>/tools/permissions.addon.py       待并入的授权片段

模板版本写在生成物与 ``ChangeProposal.version`` 里，便于回看"这份骨架是哪版模板产的"。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_builder.api.deciders import ProposalDraft

# 脚手架模板版本（并入 ChangeProposal.version）。
SCAFFOLD_TEMPLATE_VERSION = "scaffold-1"

# 生成物在 workspace 下的固定根目录名。
SCAFFOLD_ROOT = "proposals"


def pascal_case(name: str) -> str:
    """``data_cleaner`` → ``DataCleaner``（用于类名）。"""
    return "".join(part.capitalize() for part in name.split("_") if part) or "NewRole"


def render_role_module(draft: ProposalDraft, proposal_id: str) -> str:
    """渲染角色模块骨架（形态对齐既有 roles/*.py）。

    刻意只 import 标准库 + ``contracts.schemas.Step``：``roles`` 层禁止 import
    ``api`` / ``tools`` / ``llm``，工具调用一律经注入的 ``executor_fn``。
    """
    class_name = pascal_case(draft.name)
    accepts = "\n".join(f'    "{action}",' for action in draft.accepts)
    return f'''"""<待填写：中文名>（{class_name}）—— <待填写：主架构・层次>。

职责：{draft.mission}

边界声明（由副结构提议 {proposal_id} 生成，请人工评审后补全）：
- <待填写：什么情况下必须停下并上报>
- <待填写：明确不做的事>

执行协议：
1. 校验 action 是否在本角色受理范围内（不在 → rejected，不执行）
2. 无 executor_fn 时只校验不执行（返回 pending）
3. 经注入的 executor_fn 调用工具；异常转状态，不向上抛

注意：本文件是脚手架。在把角色名加入 ``tools/permissions.py`` 的授权表、
并在 ``docs/execution-protocols.md`` 补齐协议章节之前，本角色不会被派发。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 本角色受理的 action 集合（来自提议的 accepts）。
ACCEPTS: frozenset[str] = frozenset({{
{accepts}
}})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class {class_name}Report:
    """本角色的执行结果。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    outputs: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class {class_name}:
    """{draft.mission}

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        llm_client: LLM 客户端（可选；由运行时密钥工厂注入）。
    """

    correlation_id: str = "c-unknown"
    llm_client: Any = None

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> {class_name}Report:
        """执行本角色受理的步骤。"""
        # 1. 只受理自己声明的 action。
        if step.action not in ACCEPTS:
            return {class_name}Report(
                step_id=step.id,
                status="rejected",
                error=f"超出本角色受理范围: {{step.action}}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return {class_name}Report(step_id=step.id, status="pending")

        # 3. 执行（异常转状态，不向上抛）。
        try:
            result = executor_fn(step)
        except PermissionError as exc:
            return {class_name}Report(
                step_id=step.id, status="failed", error=f"权限不足: {{exc}}"
            )
        except Exception as exc:  # noqa: BLE001  执行者需捕获所有执行异常
            return {class_name}Report(
                step_id=step.id,
                status="failed",
                error=f"{{type(exc).__name__}}: {{exc}}",
            )
        return {class_name}Report(
            step_id=step.id,
            status="done",
            outputs=[str(result)] if result is not None else [],
        )


__all__ = [
    "ACCEPTS",
    "ExecutorFn",
    "{class_name}",
    "{class_name}Report",
]
'''


def render_role_test(draft: ProposalDraft) -> str:
    """渲染测试脚手架（≥10 例：功能 / 边界 / 常量 / 授权四类）。

    授权类三条标了 ``xfail``：并入 ``tools/permissions.py`` 之后它们应当转为通过，
    届时删掉标记即可（``strict=False`` 保证转为通过时不会误报失败）。
    """
    class_name = pascal_case(draft.name)
    first_action = draft.accepts[0] if draft.accepts else "web_search"
    accepts_literal = "{" + ", ".join(f'"{a}"' for a in (draft.accepts or (first_action,))) + "}"
    return f'''"""{class_name} 角色测试脚手架（由副结构提议生成）。

四类覆盖：功能 / 边界 / 常量 / 授权。
授权类三条当前标 ``xfail``：把角色并入 ``tools/permissions.py`` 后删掉标记即转正。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.schemas import Step
from agent_builder.roles.{draft.name} import ACCEPTS, {class_name}, {class_name}Report
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _step(action: str = "{first_action}", step_id: str = "s-001") -> Step:
    return Step(id=step_id, action=action, inputs={{}})


class Test功能:
    def test_受理内的动作正常执行(self) -> None:
        report = {class_name}().execute(_step(), lambda step: "ok")
        assert report.status == "done"
        assert report.outputs == ["ok"]

    def test_未提供执行函数时不执行(self) -> None:
        report = {class_name}().execute(_step())
        assert report.status == "pending"


class Test边界:
    def test_受理外的动作被拒(self) -> None:
        report = {class_name}().execute(_step("rollback"))
        assert report.status == "rejected"
        assert report.error

    def test_权限不足转失败(self) -> None:
        def _deny(step: Step):
            raise PermissionError("需用户放行")

        report = {class_name}().execute(_step(), _deny)
        assert report.status == "failed"
        assert "权限" in (report.error or "")

    def test_其他异常转失败且不抛出(self) -> None:
        def _boom(step: Step):
            raise RuntimeError("boom")

        report = {class_name}().execute(_step(), _boom)
        assert report.status == "failed"
        assert "RuntimeError" in (report.error or "")


class Test常量:
    def test_受理范围与提议一致(self) -> None:
        assert ACCEPTS == frozenset({accepts_literal})

    def test_结果对象默认值(self) -> None:
        report = {class_name}Report(step_id="s-001", status="done")
        assert report.outputs == []
        assert report.error is None


@pytest.mark.xfail(reason="待并入 tools/permissions.py 授权后删掉标记", strict=False)
class Test授权待并入:
    def test_角色已获授权(self) -> None:
        assert "{draft.name}" in DEFAULT_ROLE_PERMS

    def test_白名单与受理范围一致(self) -> None:
        perm = DEFAULT_ROLE_PERMS["{draft.name}"]
        assert set(perm.allowed_tools) == set(ACCEPTS)

    def test_默认不含高风险工具(self) -> None:
        perm = DEFAULT_ROLE_PERMS["{draft.name}"]
        assert perm.high_risk_tools == frozenset()
'''


def render_protocol_section(draft: ProposalDraft, proposal_id: str) -> str:
    """渲染协议文档章节草稿（待人工并入 ``docs/execution-protocols.md``）。"""
    accepts = "、".join(draft.accepts) if draft.accepts else "（待补）"
    return f"""## {pascal_case(draft.name)}（{draft.name}）

层级：`（待填写：主架构／治理／副架构·某层）`
受理动作：`{accepts}`
风险：`{draft.risk}`

职责：{draft.mission}

边界：
- （待填写：什么情况下必须停下并上报）

为什么需要它（提议理由）：{draft.rationale or "（待补）"}

来源：副结构提议 `{proposal_id}`，经用户批准后生成此草稿；请评审后并入本文档。
"""


def render_permissions_note(draft: ProposalDraft) -> str:
    """渲染待并入的授权片段（``tools/permissions.py``）。

    只给片段、不自动改文件：授权是**权限决策**，必须由人落笔。
    """
    allowed = ",\n            ".join(f'"{action}"' for action in draft.accepts)
    return f'''"""待并入 tools/permissions.py 的授权片段（由副结构提议生成）。

并入方式：把下面的 RolePerm 加进 ``DEFAULT_ROLE_PERMS`` 字典，然后删掉
``tests/test_roles_{draft.name}.py`` 里 ``Test授权待并入`` 的 xfail 标记。
在并入之前，``{draft.name}`` 不会出现在权限矩阵与角色目录里，**无法被派发**。

高风险工具默认留空：新角色先只做低风险动作，需要写/提交/回滚时单独评估。
"""

# 待并入的下述条目（注意缩进与逗号，直接加到 DEFAULT_ROLE_PERMS 里）：
#
#     "{draft.name}": RolePerm(
#         role="{draft.name}",
#         allowed_tools=frozenset({{
#             {allowed}
#         }}),
#         high_risk_tools=frozenset(),
#     ),
'''


def render_readme(draft: ProposalDraft, proposal_id: str) -> str:
    """渲染并入清单（人工接手时的唯一入口）。"""
    accepts = "\n".join(f"- `{action}`" for action in draft.accepts) or "- （待补）"
    return f"""# 副结构提议 {proposal_id} · 并入清单

提议内容：新增角色 `{draft.name}`
用途：{draft.mission}
自评风险：`{draft.risk}`
提议理由：{draft.rationale or "（待补）"}

受理动作：
{accepts}

## 这里是什么

本目录是**批准后生成的脚手架**，没有改动仓库任何既有文件 —— 因为一次模型提议
不应直接获得执行权限。请按下面清单人工并入。

## 并入步骤（三道门）

1. **角色实现**：把 `agent_builder/roles/{draft.name}.py` 复制到仓库同路径，
   补全文档串里的 `<待填写>` 与边界声明。
2. **授权**（`tools/permissions.py`）：按 `tools/permissions.addon.py` 里的片段，
   把 `RolePerm` 加进 `DEFAULT_ROLE_PERMS`。**不做这步，新角色不会被派发。**
3. **协议文档**（`docs/execution-protocols.md`）：把 `docs/execution-protocols.addon.md`
   的章节并入。
4. **测试**：把 `tests/test_roles_{draft.name}.py` 复制到仓库 `tests/` 下，
   删掉 `Test授权待并入` 的 xfail 标记（此时授权已就位，应当转为通过）。
5. 跑一次 `ruff` + 全量 `pytest`，确认基线不降。

## 为什么这样设计

- **只生成、不自动改既有文件**：`permissions.py` / `registry.py` / 协议文档是权限与
  契约的落点，自动改写等于绕过评审。
- **默认无高风险工具**：新角色先只做低风险动作；需要写/提交/回滚时单独评估。
- **可审计**：模板版本写在生成物与任务的 `ChangeProposal.version` 里（当前
  `{SCAFFOLD_TEMPLATE_VERSION}`），便于回看这份骨架是哪版模板产的。
"""


def plan_scaffold(draft: ProposalDraft, proposal_id: str) -> dict[str, str]:
    """规划生成物：``相对工作区的路径 → 文件内容``（纯计算，不落盘）。"""
    root = f"{SCAFFOLD_ROOT}/{proposal_id}"
    return {
        f"{root}/README.md": render_readme(draft, proposal_id),
        f"{root}/agent_builder/roles/{draft.name}.py": render_role_module(draft, proposal_id),
        f"{root}/tests/test_roles_{draft.name}.py": render_role_test(draft),
        f"{root}/docs/execution-protocols.addon.md": render_protocol_section(draft, proposal_id),
        f"{root}/tools/permissions.addon.py": render_permissions_note(draft),
    }


@dataclass(slots=True)
class ScaffoldResult:
    """落盘结果。"""

    files: list[str]  # 已写入的相对路径（相对工作区）
    error: str | None = None


def _resolve_in_workspace(workspace_dir: Path, relative: str) -> Path:
    """把相对路径解析成工作区内的绝对路径；越界即拒绝（沙箱校验）。"""
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"生成路径越界: {relative}")
    root = workspace_dir.resolve()
    target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"生成路径越界: {relative}")
    return target


def write_scaffold(workspace_dir: str | Path, files: Mapping[str, str]) -> ScaffoldResult:
    """把生成物写入工作区（沙箱校验 + 原子性：任一失败即整体报错，不写半个）。

    Args:
        workspace_dir: 任务工作区根目录（唯一可写区域）。
        files: ``相对路径 → 内容``（通常来自 :func:`plan_scaffold`）。

    Returns:
        :class:`ScaffoldResult`：已写入的相对路径列表；越界 / 写失败时
        ``error`` 非空且 ``files`` 为空（不会留下写了一半的目录）。
    """
    root = Path(workspace_dir)
    try:
        targets = {relative: _resolve_in_workspace(root, relative) for relative in files}
    except ValueError as exc:
        return ScaffoldResult(files=[], error=str(exc))
    written: list[str] = []
    try:
        for relative, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[relative], encoding="utf-8")
            written.append(relative)
    except OSError as exc:
        return ScaffoldResult(files=[], error=f"{type(exc).__name__}: {exc}")
    return ScaffoldResult(files=written)


__all__ = [
    "SCAFFOLD_ROOT",
    "SCAFFOLD_TEMPLATE_VERSION",
    "ScaffoldResult",
    "pascal_case",
    "plan_scaffold",
    "render_permissions_note",
    "render_protocol_section",
    "render_readme",
    "render_role_module",
    "render_role_test",
    "write_scaffold",
]
