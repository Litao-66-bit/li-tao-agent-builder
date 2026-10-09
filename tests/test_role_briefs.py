"""角色简报（role brief）—— 派生、渲染、注入与对账护栏。

覆盖阶段 1 的三档位（off / core / full）、真源派生、客户端注入与任务级计量。
护栏重点：简报里出现的工具名必须 ⊆ permissions.py 实授；真源格式变化必须报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_builder.api import orchestrator, role_briefs, routes
from agent_builder.contracts.schemas import Step
from agent_builder.evaluation import role_brief_ab
from agent_builder.llm.client import LLMClient, LLMUsage, UsageAccumulator, merge_usage
from agent_builder.llm.config import LLMConfig

_SEARCHER_DOC_TEMPLATE = (
    "## Searcher（检索执行者）\n\n"
    "### 角色规格\n\n"
    "| 属性 | 值 |\n|---|---|\n"
    "| 角色名 | `searcher` |\n"
    "| 层级 | executor（主架构·执行层） |\n"
    "{mission}"
    "\n### 授权清单\n\n"
    "| 工具 | 用途 | 风险 |\n|---|---|---|\n"
    "| `web_search` | 网页搜索 | low |\n\n"
    "**边界声明**：无来源标未查证。\n\n"
    "### 执行协议\n\n#### 分步流程\n\n"
    "```\n1. 拆解检索需求\n2. 输出带来源清单\n```\n"
)


def _make_client(
    *,
    brief: str = "",
    role: str = "",
    content: str = "ok",
    usage_metadata: object = None,
    response_metadata: object = None,
    sink: object = None,
) -> tuple[LLMClient, MagicMock]:
    """构造注入了 mock 后端的 LLMClient（无真实密钥、不联网）。"""
    client = LLMClient(
        LLMConfig(), role_brief=brief, role=role, usage_sink=sink
    )
    resp = MagicMock(content=content)
    resp.usage_metadata = usage_metadata
    resp.response_metadata = response_metadata
    mock = MagicMock()
    mock.invoke.return_value = resp
    client._client = mock  # type: ignore[attr-defined]
    return client, mock


class TestCurrentMode:
    """档位解析：默认关、三档可选、非法值回落 off。"""

    def test_默认档位为off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(role_briefs.MODE_ENV, raising=False)
        assert role_briefs.current_mode() == role_briefs.MODE_OFF
        assert not role_briefs.is_enabled()

    @pytest.mark.parametrize("mode", ["off", "core", "full"])
    def test_三档位可读(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, mode)
        assert role_briefs.current_mode() == mode

    def test_大小写与空白归一(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, "  CORE ")
        assert role_briefs.current_mode() == role_briefs.MODE_CORE

    def test_非法值回落off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, "verbose")
        assert role_briefs.current_mode() == role_briefs.MODE_OFF


class TestSources:
    """真源派生：覆盖范围与字段齐备。"""

    def test_覆盖范围是六个会调llm的角色(self) -> None:
        assert role_briefs.LLM_ROLES == (
            "decomposer",
            "searcher",
            "doc_worker",
            "fact_checker",
            "code_worker",
            "summarizer",
        )

    @pytest.mark.parametrize("role", role_briefs.LLM_ROLES)
    def test_每个角色都有完整来源(self, role: str) -> None:
        source = role_briefs.build_role_brief_sources()[role]
        assert source.mission, f"{role} 缺使命"
        assert source.boundary, f"{role} 缺边界"
        assert source.tools, f"{role} 缺授权工具"
        assert source.steps, f"{role} 缺分步流程"
        assert source.cn_name, f"{role} 缺中文名"

    @pytest.mark.parametrize("role", role_briefs.LLM_ROLES)
    def test_授权工具取实授真源(self, role: str) -> None:
        source = role_briefs.build_role_brief_sources()[role]
        assert source.tools == role_briefs.granted_tools(role)


class TestRenderBrief:
    """渲染：三档位形态正确。"""

    @pytest.mark.parametrize("role", role_briefs.LLM_ROLES)
    def test_off档为空串(self, role: str) -> None:
        assert role_briefs.render_brief(role, role_briefs.MODE_OFF) == ""

    @pytest.mark.parametrize("role", role_briefs.LLM_ROLES)
    def test_core档含职责工具边界且无流程(self, role: str) -> None:
        brief = role_briefs.render_brief(role, role_briefs.MODE_CORE)
        for tag in ("【角色】", "【使命】", "【授权工具】", "【边界】"):
            assert tag in brief, f"{role} 缺 {tag}"
        assert "【流程】" not in brief

    @pytest.mark.parametrize("role", role_briefs.LLM_ROLES)
    def test_full档追加流程(self, role: str) -> None:
        brief = role_briefs.render_brief(role, role_briefs.MODE_FULL)
        assert "【流程】" in brief
        assert " → " in brief

    def test_角色行含中文名与层级(self) -> None:
        brief = role_briefs.render_brief("fact_checker", role_briefs.MODE_CORE)
        first_line = brief.splitlines()[0]
        assert first_line.startswith("【角色】fact_checker（事实核验者）")
        assert "验证层" in first_line

    def test_未知档位报错(self) -> None:
        with pytest.raises(ValueError):
            role_briefs.render_brief("searcher", "verbose")

    def test_未覆盖角色报错(self) -> None:
        with pytest.raises(ValueError):
            role_briefs.render_brief("proposer", role_briefs.MODE_CORE)


class TestGuardrail:
    """护栏（决策 8）：工具名 ⊆ 实授；真源格式变化即报错。"""

    @pytest.mark.parametrize("mode", [role_briefs.MODE_CORE, role_briefs.MODE_FULL])
    @pytest.mark.parametrize("role", role_briefs.LLM_ROLES)
    def test_简报工具名是实授子集(self, role: str, mode: str) -> None:
        brief = role_briefs.render_brief(role, mode)
        line = next(line for line in brief.splitlines() if line.startswith("【授权工具】"))
        names = [name for name in line.removeprefix("【授权工具】").split("、") if name]
        assert names, f"{role} 未渲染出工具"
        assert set(names) <= set(role_briefs.granted_tools(role)), f"{role} 出现未实授工具"

    def test_工具名格式为纯标识符(self) -> None:
        brief = role_briefs.render_brief("searcher", role_briefs.MODE_CORE)
        line = next(line for line in brief.splitlines() if line.startswith("【授权工具】"))
        names = line.removeprefix("【授权工具】").split("、")
        assert all(re.fullmatch(r"[a-z_]+", name) for name in names)

    def test_真源缺使命时报错(self) -> None:
        text = _SEARCHER_DOC_TEMPLATE.format(mission="")
        sources = role_briefs.parse_brief_sources(text)
        with pytest.raises(ValueError, match="缺字段"):
            role_briefs.render_brief("searcher", role_briefs.MODE_CORE, sources=sources)

    def test_真源正常时可渲染(self) -> None:
        text = _SEARCHER_DOC_TEMPLATE.format(mission="| 使命 | 多路关键词检索 |\n")
        sources = role_briefs.parse_brief_sources(text)
        brief = role_briefs.render_brief("searcher", role_briefs.MODE_FULL, sources=sources)
        assert "多路关键词检索" in brief
        assert "【流程】1 拆解检索需求 → 2 输出带来源清单" in brief


class TestBriefFor:
    """brief_for：关闭 / 未覆盖时静默返回空串，启用时返回简报。"""

    def test_默认关闭返回空串(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(role_briefs.MODE_ENV, raising=False)
        assert role_briefs.brief_for("searcher") == ""

    def test_未覆盖角色返回空串(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, role_briefs.MODE_FULL)
        assert role_briefs.brief_for("proposer") == ""

    def test_启用后返回简报(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, role_briefs.MODE_CORE)
        assert "【角色】searcher" in role_briefs.brief_for("searcher")


class TestClientInjection:
    """客户端层注入：system 段最前；不改动入参；无 system 时新建。"""

    def test_无system时新建且在最前(self) -> None:
        client, mock = _make_client(brief="【角色】searcher")
        client.chat([{"role": "user", "content": "hi"}])
        sent = mock.invoke.call_args[0][0]
        assert sent[0]["role"] == "system"
        assert sent[0]["content"] == "【角色】searcher"
        assert sent[1] == {"role": "user", "content": "hi"}

    def test_有system时简报在最前且保留原契约(self) -> None:
        client, mock = _make_client(brief="【角色】searcher")
        original = [{"role": "system", "content": "只返回 JSON"}, {"role": "user", "content": "hi"}]
        client.chat(original)
        sent = mock.invoke.call_args[0][0]
        assert sent[0]["content"].startswith("【角色】searcher")
        assert sent[0]["content"].endswith("只返回 JSON")
        # 入参未被就地修改。
        assert original[0]["content"] == "只返回 JSON"

    def test_无简报时消息原样透传(self) -> None:
        client, mock = _make_client()
        client.chat([{"role": "user", "content": "hi"}])
        sent = mock.invoke.call_args[0][0]
        assert sent == [{"role": "user", "content": "hi"}]

    def test_with_role_brief_派生独立实例并共享sink(self) -> None:
        sink = UsageAccumulator()
        client, _ = _make_client(sink=sink)
        derived = client.with_role_brief("【角色】searcher", role="searcher")
        assert derived is not client
        assert derived.role_brief == "【角色】searcher"
        assert client.with_role_brief("【角色】searcher", role="searcher") is derived
        assert derived is not client.with_role_brief("【角色】fact_checker", role="fact_checker")

        derived._client = MagicMock()  # type: ignore[attr-defined]
        derived._client.invoke.return_value = MagicMock(content="x", usage_metadata=None)  # type: ignore[attr-defined]
        derived.chat([{"role": "user", "content": "hi"}])
        assert sink.total.calls == 1
        assert "searcher" in sink.to_dict()["by_role"]

    def test_空简报派生返回自身(self) -> None:
        client, _ = _make_client()
        assert client.with_role_brief("", role="searcher") is client


class TestUsage:
    """usage 采集：cached / uncached 分开，两处元数据都能取。"""

    def test_usage_metadata有缓存明细(self) -> None:
        meta = {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_token_details": {"cache_read": 60},
        }
        client, _ = _make_client(usage_metadata=meta)
        client.chat([{"role": "user", "content": "hi"}])
        usage = client.usage
        assert (usage.prompt_tokens, usage.cached_prompt_tokens) == (100, 60)
        assert usage.uncached_prompt_tokens == 40
        assert usage.completion_tokens == 20
        assert usage.total_tokens == 120
        assert usage.calls == 1

    def test_回退到response_metadata(self) -> None:
        raw = {
            "token_usage": {
                "prompt_tokens": 50,
                "completion_tokens": 5,
                "prompt_cache_hit_tokens": 10,
            }
        }
        client, _ = _make_client(usage_metadata=None, response_metadata=raw)
        client.chat([{"role": "user", "content": "hi"}])
        usage = client.usage
        assert usage.prompt_tokens == 50
        assert usage.cached_prompt_tokens == 10
        assert usage.uncached_prompt_tokens == 40

    def test_无元数据记零且不报错(self) -> None:
        client, _ = _make_client(usage_metadata=None, response_metadata={})
        client.chat([{"role": "user", "content": "hi"}])
        assert client.usage.calls == 1
        assert client.usage.total_tokens == 0

    def test_accumulator按角色累计(self) -> None:
        sink = UsageAccumulator()
        sink.record("searcher", LLMUsage(calls=1, prompt_tokens=10, completion_tokens=2))
        sink.record("searcher", LLMUsage(calls=1, prompt_tokens=5, cached_prompt_tokens=5))
        sink.record("summarizer", LLMUsage(calls=1, prompt_tokens=3))
        payload = sink.to_dict()
        assert payload["calls"] == 3
        assert payload["prompt_tokens"] == 18
        assert payload["by_role"]["searcher"]["prompt_tokens"] == 15
        assert payload["by_role"]["summarizer"]["prompt_tokens"] == 3

    def test_merge_usage累加并保留mode(self) -> None:
        left = {"calls": 1, "total_tokens": 10, "mode": "core", "by_role": {}}
        right = UsageAccumulator()
        right.record("searcher", LLMUsage(calls=2, prompt_tokens=30, completion_tokens=4))
        merged = merge_usage(left, {**right.to_dict(), "mode": "full"})
        assert merged["calls"] == 3
        assert merged["total_tokens"] == 44
        assert merged["mode"] == "full"
        assert merged["by_role"]["searcher"]["calls"] == 2


class TestOrchestratorWiring:
    """串味防护：简报启用时按角色派生实例，关闭时原样透传。"""

    def _run(self, monkeypatch: pytest.MonkeyPatch, mode: str | None) -> tuple[object, MagicMock]:
        if mode is None:
            monkeypatch.delenv(role_briefs.MODE_ENV, raising=False)
        else:
            monkeypatch.setenv(role_briefs.MODE_ENV, mode)
        sentinel = MagicMock(name="base-client")
        sentinel.with_role_brief.side_effect = lambda brief, role="": f"derived:{role}:{brief}"
        captured: dict[str, object] = {}

        @dataclass
        class _Role:
            correlation_id: str
            llm_client: object

            def execute(
                self, step: Step, *, executor_fn: object, context: object = None
            ) -> object:
                captured["llm_client"] = self.llm_client
                outcome = MagicMock()
                outcome.status = "done"
                return outcome

        monkeypatch.setitem(orchestrator._ROLE_CLASSES, "searcher", _Role)
        step = Step(id="s1", action="web_search", inputs={}, assignee="searcher")
        dispatched, _ = orchestrator.dispatch_to_role(
            step, lambda _step: "tool-result", correlation_id="t1", llm_client=sentinel
        )
        assert dispatched
        return captured["llm_client"], sentinel

    def test_关闭时透传原客户端(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, sentinel = self._run(monkeypatch, None)
        assert client is sentinel
        sentinel.with_role_brief.assert_not_called()

    def test_启用时按角色派生(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client, sentinel = self._run(monkeypatch, role_briefs.MODE_CORE)
        assert isinstance(client, str) and client.startswith("derived:searcher:")
        sentinel.with_role_brief.assert_called_once()


class TestAbReport:
    """A/B 报告：确定性生成、三档位与指标齐备（脚本本身不调 LLM）。"""

    def test_报告可重复生成(self) -> None:
        assert role_brief_ab.render_markdown() == role_brief_ab.render_markdown()

    def test_报告覆盖三档位与指标(self) -> None:
        markdown = role_brief_ab.render_markdown()
        for arm in role_brief_ab.ARMS:
            assert f"`{arm}`" in markdown
        for metric in role_brief_ab.P0_METRICS:
            assert metric in markdown

    def test_成本估算单调且off为零(self) -> None:
        totals = role_brief_ab.arm_totals()
        assert totals[role_briefs.MODE_OFF] == (0, 0)
        assert totals[role_briefs.MODE_FULL][1] > totals[role_briefs.MODE_CORE][1] > 0

    def test_逐角色成本为正(self) -> None:
        costs = role_brief_ab.collect_costs()
        assert [cost.role for cost in costs] == sorted(role_briefs.LLM_ROLES)
        assert all(cost.core_chars > 0 and cost.full_chars >= cost.core_chars for cost in costs)


class TestTaskUsageRecording:
    """任务级计量落位：写入 TaskEntry.usage 并附当前档位（供审计消费）。"""

    def test_记录用量并附档位(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, role_briefs.MODE_CORE)
        entry = SimpleNamespace(usage={})
        accumulator = UsageAccumulator()
        accumulator.record("searcher", LLMUsage(calls=1, prompt_tokens=10, completion_tokens=2))
        routes._record_usage(entry, accumulator)
        assert entry.usage["calls"] == 1
        assert entry.usage["prompt_tokens"] == 10
        assert entry.usage["mode"] == role_briefs.MODE_CORE

    def test_二次记录累加保留分角色明细(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, role_briefs.MODE_FULL)
        entry = SimpleNamespace(usage={})
        first = UsageAccumulator()
        first.record("searcher", LLMUsage(calls=1, prompt_tokens=10))
        second = UsageAccumulator()
        second.record("summarizer", LLMUsage(calls=1, prompt_tokens=3))
        routes._record_usage(entry, first)
        routes._record_usage(entry, second)
        assert entry.usage["calls"] == 2
        assert entry.usage["prompt_tokens"] == 13
        assert set(entry.usage["by_role"]) == {"searcher", "summarizer"}
        assert entry.usage["mode"] == role_briefs.MODE_FULL

    def test_零调用不写入(self) -> None:
        entry = SimpleNamespace(usage={})
        routes._record_usage(entry, UsageAccumulator())
        assert entry.usage == {}


class TestRequestLevelModeOverride:
    """按请求切档（A/B 对照）：显式 mode 覆盖环境变量，不改默认行为。"""

    def test_显式档位覆盖环境关闭(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(role_briefs.MODE_ENV, raising=False)
        assert role_briefs.brief_for("searcher", role_briefs.MODE_CORE).startswith("【角色】searcher")
        assert role_briefs.brief_for("searcher", role_briefs.MODE_FULL).startswith("【角色】searcher")

    def test_显式关闭覆盖环境开启(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, role_briefs.MODE_FULL)
        assert role_briefs.brief_for("searcher", role_briefs.MODE_OFF) == ""

    def test_显式档位不影响未覆盖角色(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(role_briefs.MODE_ENV, raising=False)
        assert role_briefs.brief_for("proposer", role_briefs.MODE_CORE) == ""

    def test_非法档位安全返回空串(self) -> None:
        assert role_briefs.brief_for("searcher", "verbose") == ""

    def test_未传档位仍读环境变量(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(role_briefs.MODE_ENV, role_briefs.MODE_CORE)
        assert role_briefs.brief_for("searcher") == role_briefs.brief_for(
            "searcher", role_briefs.MODE_CORE
        )

    def test_orchestrator按参数派生实例(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 环境关闭，但请求档位=core → 仍按角色派生实例（A/B 切档生效）。
        monkeypatch.delenv(role_briefs.MODE_ENV, raising=False)
        sentinel = MagicMock(name="base-client")
        sentinel.with_role_brief.side_effect = lambda brief, role="": f"derived:{role}"
        captured: dict[str, object] = {}

        @dataclass
        class _Role:
            correlation_id: str
            llm_client: object

            def execute(
                self, step: Step, *, executor_fn: object, context: object = None
            ) -> object:
                captured["llm_client"] = self.llm_client
                outcome = MagicMock()
                outcome.status = "done"
                return outcome

        monkeypatch.setitem(orchestrator._ROLE_CLASSES, "searcher", _Role)
        step = Step(id="s1", action="web_search", inputs={}, assignee="searcher")
        dispatched, _ = orchestrator.dispatch_to_role(
            step,
            lambda _step: "tool-result",
            correlation_id="t1",
            llm_client=sentinel,
            role_brief_mode=role_briefs.MODE_CORE,
        )
        assert dispatched
        assert captured["llm_client"] == "derived:searcher"

    def test_记录用量按传入档位(self) -> None:
        entry = SimpleNamespace(usage={})
        accumulator = UsageAccumulator()
        accumulator.record("searcher", LLMUsage(calls=1, prompt_tokens=5))
        routes._record_usage(entry, accumulator, mode=role_briefs.MODE_FULL)
        assert entry.usage["mode"] == role_briefs.MODE_FULL
