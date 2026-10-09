"""能力目录测试：架构分类 ↔ 文档契约、目录 ↔ 权限矩阵、副结构门控与提示词渲染。

覆盖：
- 三角一致性：主/副/权限三分与评分层 ``role_universe()`` 完全一致；
- 分层与 ``docs/execution-protocols.md`` 的「层级」字段一致（分类漂移即失败）；
- 目录里每个 (角色, action) 都落在该角色的 ``allowed_tools`` 内；
- 副结构门控：关闭只给主架构执行者 + operator，开启才追加副架构。
"""

from __future__ import annotations

from agent_builder.api.orchestrator import ACTION_ROLE_MAP
from agent_builder.api.role_catalog import (
    MAIN,
    MAIN_ARCHITECTURE,
    MAIN_EXECUTORS,
    NON_STEP_ROLES,
    PERMISSION,
    PERMISSION_EXECUTORS,
    PERMISSION_LAYER,
    SUB,
    SUB_ARCHITECTURE,
    RoleSpec,
    action_signature,
    all_roles,
    architecture_of,
    build_catalog,
    catalog_prompt,
    is_high_risk,
    selectable_roles,
)
from agent_builder.evaluation.scorecards import parse_protocols, role_universe
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS
from agent_builder.tools.registry import registry


class TestArchitectureClassification:
    def test_三分与评分层角色全集一致(self) -> None:
        """主 12 + 副 5 + 权限 3 == 评分层认定的全部角色（不重不漏）。"""
        assert all_roles() == set(role_universe())

    def test_三分类互斥(self) -> None:
        assert not (MAIN_ARCHITECTURE & SUB_ARCHITECTURE)
        assert not (MAIN_ARCHITECTURE & PERMISSION_LAYER)
        assert not (SUB_ARCHITECTURE & PERMISSION_LAYER)

    def test_分层与文档层级字段一致(self) -> None:
        """防分类漂移：主架构不得被文档标成副架构/权限层，反之亦然。"""
        docs = parse_protocols()
        for role in MAIN_ARCHITECTURE:
            layer = docs[role].layer or ""
            assert "副架构" not in layer and "权限层" not in layer, f"{role}: {layer}"
        for role in SUB_ARCHITECTURE:
            assert "副架构" in (docs[role].layer or ""), role
        for role in PERMISSION_LAYER:
            assert "权限层" in (docs[role].layer or ""), role

    def test_architecture_of(self) -> None:
        assert architecture_of("code_worker") == MAIN
        assert architecture_of("auditor") == SUB
        assert architecture_of("operator") == PERMISSION
        assert architecture_of("no_such_role") is None


class TestCatalogGating:
    def test_关闭副结构只给主架构与operator(self) -> None:
        assert selectable_roles(sub_arch=False) == MAIN_EXECUTORS | PERMISSION_EXECUTORS

    def test_开启副结构追加副架构(self) -> None:
        opened = selectable_roles(sub_arch=True)
        assert opened == MAIN_EXECUTORS | PERMISSION_EXECUTORS | SUB_ARCHITECTURE
        assert opened - selectable_roles(sub_arch=False) == SUB_ARCHITECTURE

    def test_流程控制角色永不进目录(self) -> None:
        for sub_arch in (False, True):
            names = {spec.name for spec in build_catalog(sub_arch=sub_arch)}
            assert not (names & NON_STEP_ROLES)
            assert "conductor" not in names

    def test_关闭时目录不含副架构角色(self) -> None:
        names = {spec.name for spec in build_catalog(sub_arch=False)}
        assert not (names & SUB_ARCHITECTURE)

    def test_已注册工具一律不越权(self) -> None:
        """目录里的**真实工具**必须落在对应角色的 allowed_tools 内。

        ``accepts`` 里还有 ``summarize`` / ``propose`` 这类**角色内行为**
        （未注册为工具、不经工具门卫），它们不参与权限矩阵校验。
        """
        for sub_arch in (False, True):
            for spec in build_catalog(sub_arch=sub_arch):
                perm = DEFAULT_ROLE_PERMS[spec.name]
                for action in spec.accepts:
                    if registry.get(action) is not None:
                        assert action in perm.allowed_tools, (spec.name, action)
                assert set(spec.high_risk_tools) <= set(perm.allowed_tools), spec.name

    def test_operator是通用工具身份(self) -> None:
        """权限层身份没有角色行为，可承接的就是它的工具白名单。"""
        specs = {spec.name: spec for spec in build_catalog(sub_arch=False)}
        allowed = frozenset(DEFAULT_ROLE_PERMS["operator"].allowed_tools)
        assert specs["operator"].accepts == allowed

    def test_角色内行为不冒充工具(self) -> None:
        """summarizer 的 summarize / report 不是注册工具（角色内行为）。"""
        specs = {spec.name: spec for spec in build_catalog(sub_arch=True)}
        internal = {a for a in specs["summarizer"].accepts if registry.get(a) is None}
        assert internal == {"report", "summarize"}

    def test_开启时目录覆盖全部静态映射动作(self) -> None:
        accepts: set[str] = set()
        for spec in build_catalog(sub_arch=True):
            accepts |= spec.accepts
        assert set(ACTION_ROLE_MAP) <= accepts

    def test_关闭时不含副架构专属动作(self) -> None:
        accepts: set[str] = set()
        for spec in build_catalog(sub_arch=False):
            accepts |= spec.accepts
        # 副架构角色自身的动作不可用（proposer / impact_analyzer 属副架构）。
        assert "propose" not in accepts
        assert "impact_analyze" not in accepts
        # 但 metric_collect 仍可达 —— operator 的工具白名单里含它（只是不再由 auditor 承接）。
        assert "metric_collect" in accepts
        assert "web_search" in accepts
        assert "file_write" in accepts

    def test_无可用动作的角色被如实标注(self) -> None:
        """看门人 / 记录员今天没有可承接动作 → executable=False（不假装能用）。"""
        specs = {spec.name: spec for spec in build_catalog(sub_arch=True)}
        assert specs["gatekeeper"].executable is False
        assert specs["historian"].executable is False
        assert specs["code_worker"].executable is True
        assert "暂无可执行的动作" in specs["gatekeeper"].to_prompt_line()


class TestActionSignature:
    """回归：目录里给的动作必须带**参数签名**（必填 + 枚举）。

    只给动作名时模型只能猜 ``inputs`` —— 实测连着两次失败：`memory_write` 缺 `key`、
    `scope` 猜成 `long_term`，白跑两轮后空转停下（`file_list` 缺 `path` 同理）。
    """

    def test_必填标星且枚举给出取值(self) -> None:
        sig = action_signature("memory_write")
        assert sig.startswith("memory_write(")
        assert "key*" in sig
        assert "content*" in sig
        assert "scope=short|long" in sig

    def test_可选且无枚举的参数不铺开(self) -> None:
        assert action_signature("file_list") == "file_list(path*)"

    def test_会改变行为的可选参数也进签名(self) -> None:
        """卡 10：``file_write`` 的 ``overwrite`` 此前完全不进签名 ——

        模型撞上「文件已存在且未授权覆盖」才知道有这个参数（只能靠错误信息补救）。
        """
        assert "overwrite" in action_signature("file_write")

    def test_角色内行为不编造签名(self) -> None:
        # summarize 不是注册工具（没有 schema）→ 原样返回，不假装它有参数。
        assert action_signature("summarize") == "summarize"


class TestPromptAndRisk:
    def test_目录里动作带签名且给出写法说明(self) -> None:
        text = catalog_prompt(sub_arch=False)
        assert "file_list(path*)" in text
        assert "按签名给 inputs" in text

    def test_没有签名时不加写法说明(self) -> None:
        spec = RoleSpec(
            name="ghost",
            architecture=MAIN,
            mission="无动作",
            accepts=frozenset({"summarize"}),
            high_risk_tools=frozenset(),
        )
        assert "参数写法" not in catalog_prompt(sub_arch=False, specs=(spec,))

    def test_参数语义写进目录(self) -> None:
        """签名只说得出参数名；path 该填文件还是目录必须写清。

        实测缺了它：模型把 file_read 的 path 传成目录 ``.``、file_list 的 path 传成
        通配符 ``*``，连着两步 E_VALIDATION 后判空转停下。
        """
        text = catalog_prompt(sub_arch=False)
        assert "path 传**目录**" in text
        assert "path 传**具体文件**" in text

    def test_提示词按门控渲染(self) -> None:
        closed = catalog_prompt(sub_arch=False)
        assert "副结构未开启" in closed
        assert "不得提议" in closed
        assert "auditor" not in closed

        opened = catalog_prompt(sub_arch=True)
        assert "副结构已开启" in opened
        assert "kind=propose" in opened
        assert "auditor" in opened

    def test_流程控制角色被显式禁止(self) -> None:
        text = catalog_prompt(sub_arch=False)
        assert "禁止选择" in text
        for role in NON_STEP_ROLES:
            assert role in text, role

    def test_高风险判定取自权限矩阵(self) -> None:
        assert is_high_risk("code_worker", "file_write") is True
        assert is_high_risk("searcher", "web_search") is False
        assert is_high_risk("no_such_role", "file_write") is False
