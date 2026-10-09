"""密钥接入各 agent：角色 LLM 使用 + 执行链路派发测试。

覆盖：
- 五个角色（Searcher / Summarizer / DocWorker / CodeWorker / FactChecker）
  注入 LLM 客户端时真实调用，未注入时降级为原行为；
- 执行编排器的 action → 角色派发与状态语义映射；
- /plan 记录 LLM 开关、/approve 按开关把运行时密钥客户端注入执行阶段。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import routes as routes_module
from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_store
from agent_builder.api.orchestrator import ACTION_ROLE_MAP, dispatch_to_role, run_plan
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.contracts.schemas import Plan, Step
from agent_builder.roles.code_worker import CodeWorker
from agent_builder.roles.doc_worker import DocWorker
from agent_builder.roles.fact_checker import FactChecker
from agent_builder.roles.searcher import Searcher
from agent_builder.roles.summarizer import Summarizer

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


class _FakeLLM:
    """最小 LLM 客户端替身：记录调用并返回预置结果。"""

    def __init__(
        self,
        *,
        json_result: dict[str, Any] | None = None,
        text: str = "",
        available: bool = True,
    ) -> None:
        self.is_available = available
        self._json = json_result if json_result is not None else {}
        self._text = text
        self.json_calls: list[str] = []
        self.chat_calls: list[list[dict[str, str]]] = []

    def complete_json(self, prompt: str, schema_hint: str = "") -> dict[str, Any]:
        self.json_calls.append(prompt)
        return self._json

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.chat_calls.append(messages)
        return self._text


@pytest.fixture(autouse=True)
def _reset_state() -> Any:
    """每个用例前后重置单例。"""
    reset_store()
    reset_api_key_store()
    yield
    reset_store()
    reset_api_key_store()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


# ── 角色：注入 LLM 时真实调用 ────────────────────────────────────


class TestSearcherLLM:
    def test_llm拆解关键词(self) -> None:
        llm = _FakeLLM(json_result={"keywords": ["Python 3.14", "python release"]})
        step = Step(id="s1", action="web_search", inputs={"query": "Python 3.14"})
        result = Searcher(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: "ok"
        )
        assert result.keywords_used == ["Python 3.14", "python release"]
        assert llm.json_calls

    def test_无llm降级为原查询(self) -> None:
        step = Step(id="s1", action="web_search", inputs={"query": "Python 3.14"})
        result = Searcher(correlation_id="c").execute(step, executor_fn=lambda st: "ok")
        assert result.keywords_used == ["Python 3.14"]

    def test_llm返回非法结构降级(self) -> None:
        llm = _FakeLLM(json_result={"keywords": "not-a-list"})
        step = Step(id="s1", action="web_search", inputs={"query": "q"})
        result = Searcher(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: "ok"
        )
        assert result.keywords_used == ["q"]


class TestSummarizerLLM:
    def test_缺结论时由llm汇总(self) -> None:
        llm = _FakeLLM(
            json_result={
                "conclusions": ["结论A"],
                "evidence": ["依据A"],
                "sources": ["url"],
                "next_steps": ["下一步A"],
            }
        )
        step = Step(id="s1", action="summarize", inputs={"context": "x"})
        result = Summarizer(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: "ok"
        )
        assert result.status == "done"
        assert result.conclusions == ["结论A"]
        assert result.next_steps == ["下一步A"]
        assert len(result.sections) == 5

    def test_显式字段优先于llm(self) -> None:
        llm = _FakeLLM(json_result={"conclusions": ["LLM 结论"]})
        step = Step(id="s1", action="summarize", inputs={"conclusions": ["显式结论"]})
        result = Summarizer(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: "ok"
        )
        assert result.conclusions == ["显式结论"]
        assert llm.json_calls == []

    def test_无llm时走执行函数(self) -> None:
        called: list[str] = []
        step = Step(id="s1", action="summarize", inputs={})
        result = Summarizer(correlation_id="c").execute(
            step, executor_fn=lambda st: called.append(st.id) or "ok"
        )
        assert called == ["s1"]
        assert result.status == "done"


class TestDocWorkerLLM:
    def test_缺正文由llm起草(self) -> None:
        llm = _FakeLLM(text="# 起草文档")
        step = Step(
            id="s1",
            action="web_fetch",
            inputs={"url": "https://x.com", "materials": ["素材1"]},
        )
        result = DocWorker(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: "ok"
        )
        assert result.document == "# 起草文档"
        assert llm.chat_calls

    def test_缺素材但有llm仍执行工具(self) -> None:
        called: list[str] = []
        llm = _FakeLLM(text="doc")
        step = Step(id="s1", action="web_fetch", inputs={"url": "https://x.com"})
        result = DocWorker(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: called.append(st.id) or "ok"
        )
        assert called == ["s1"]  # 工具仍被调用（不再提前返回 pending）
        assert result.status == "done"

    def test_缺素材且无llm请求补充(self) -> None:
        step = Step(id="s1", action="web_fetch", inputs={"url": "https://x.com"})
        result = DocWorker(correlation_id="c").execute(step, executor_fn=lambda st: "ok")
        assert result.status == "pending"
        assert "素材不足" in result.pending_supplements[0]


class TestCodeWorkerLLM:
    def test_缺变更说明由llm生成(self) -> None:
        llm = _FakeLLM(text="新增函数 foo")
        step = Step(id="s1", action="file_write", inputs={"path": "/x.py"})
        result = CodeWorker(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: "ok"
        )
        assert result.change_desc == "新增函数 foo"
        assert llm.chat_calls

    def test_显式变更说明优先(self) -> None:
        llm = _FakeLLM(text="LLM 说明")
        step = Step(id="s1", action="file_write", inputs={"change_desc": "显式说明"})
        result = CodeWorker(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: "ok"
        )
        assert result.change_desc == "显式说明"
        assert llm.chat_calls == []


class TestFactCheckerLLM:
    def test_缺核验项由llm产出(self) -> None:
        llm = _FakeLLM(
            json_result={
                "verifications": [
                    {"claim": "Python 3.14 发布", "source": "python.org", "status": "passed"}
                ]
            }
        )
        step = Step(id="s1", action="citation_check", inputs={"materials": ["素材"]})
        result = FactChecker(correlation_id="c", llm_client=llm).execute(
            step, executor_fn=lambda st: "ok"
        )
        assert result.passed == 1
        assert result.items[0].claim == "Python 3.14 发布"

    def test_无llm时无核验项(self) -> None:
        step = Step(id="s1", action="citation_check", inputs={})
        result = FactChecker(correlation_id="c").execute(step, executor_fn=lambda st: "ok")
        assert result.items == []


# ── 执行链路：action → 角色派发 ──────────────────────────────────


class TestDispatchToRole:
    def test_未覆盖action不派发(self) -> None:
        step = Step(id="s1", action="diff_preview", inputs={})
        dispatched, result = dispatch_to_role(
            step, lambda st: "tool", correlation_id="c", llm_client=_FakeLLM()
        )
        assert dispatched is False
        assert result is None

    def test_覆盖action派发并返回结构化结果(self) -> None:
        llm = _FakeLLM(json_result={"conclusions": ["结论"], "next_steps": []})
        step = Step(id="s1", action="summarize", inputs={})
        dispatched, result = dispatch_to_role(
            step, lambda st: "tool", correlation_id="c", llm_client=llm
        )
        assert dispatched is True
        assert result["status"] == "done"
        assert result["conclusions"] == ["结论"]

    def test_failed映射为RuntimeError(self) -> None:
        def _deny(st: Step) -> str:
            raise PermissionError("URL 不在白名单")

        step = Step(id="s1", action="web_search", inputs={"query": "q"})
        with pytest.raises(RuntimeError, match="门卫拒绝"):
            dispatch_to_role(step, _deny, correlation_id="c", llm_client=_FakeLLM())

    def test_pending_approval映射为PermissionError(self) -> None:
        step = Step(id="s1", action="file_write", inputs={"dependencies": ["requests"]})
        with pytest.raises(PermissionError):
            dispatch_to_role(step, lambda st: "tool", correlation_id="c", llm_client=_FakeLLM())

    def test_动作映射表覆盖关键action(self) -> None:
        assert ACTION_ROLE_MAP["web_search"] == "searcher"
        assert ACTION_ROLE_MAP["summarize"] == "summarizer"

    def test_测试失败时带出真实原因(self) -> None:
        """实机回归：真跑了用例且失败时，异常信息不能只剩「角色执行未通过」。"""
        out = (
            "FAILED tests/test_x.py::test_b - ModuleNotFoundError: No module named 'foo'\n"
            "1 failed, 1 passed in 0.53s\n"
        )
        step = Step(id="s1", action="test_run", inputs={"target": "tests/test_x.py"})
        with pytest.raises(RuntimeError) as excinfo:
            dispatch_to_role(step, lambda st: out, correlation_id="c", llm_client=_FakeLLM())

        message = str(excinfo.value)
        assert "test_b" in message
        assert "ModuleNotFoundError" in message
        assert "角色执行未通过" not in message

    def test_错误用例数不被当成人话(self) -> None:
        """``TestReport.error`` 是计数（int），不能被当成失败原因返回 "1"。"""
        out = "ERROR tests/test_x.py - ImportError: boom\n1 error in 0.10s\n"
        step = Step(id="s1", action="test_run", inputs={"target": "tests/test_x.py"})
        with pytest.raises(RuntimeError) as excinfo:
            dispatch_to_role(step, lambda st: out, correlation_id="c", llm_client=_FakeLLM())

        message = str(excinfo.value)
        assert message != "1"
        assert "ImportError" in message


class TestNewlyDispatchedRoles:
    """新纳入派发的角色（观测 / 验证 / 数据 / 记忆）与状态语义映射。"""

    @pytest.mark.parametrize(
        ("action", "role"),
        [
            ("metric_collect", "auditor"),
            ("test_run", "test_runner"),
            ("data_query", "data_analyst"),
            ("memory_read", "memory_keeper"),
            ("memory_write", "memory_keeper"),
            ("memory_forget", "memory_keeper"),
        ],
    )
    def test_新增动作已映射到角色(self, action: str, role: str) -> None:
        assert ACTION_ROLE_MAP[action] == role

    def test_不可派发动作保持未映射(self) -> None:
        # historian 只接受 record / log_change（均非注册工具）→ 无法派发；
        # sandbox_run 被 test_runner / data_analyst / code_worker 共同接受（歧义）→ 不映射。
        for action in ("record", "log_change", "sandbox_run"):
            assert action not in ACTION_ROLE_MAP

    def test_auditor_采集指标(self) -> None:
        step = Step(
            id="s1",
            action="metric_collect",
            inputs={
                "metrics": [
                    {
                        "name": "success_rate",
                        "value": 0.5,
                        "baseline": 0.9,
                        "threshold": 0.8,
                        "status": "abnormal",
                        "consecutive": 3,
                    }
                ]
            },
        )
        dispatched, result = dispatch_to_role(
            step, lambda st: "ok", correlation_id="c", llm_client=_FakeLLM()
        )
        assert dispatched is True
        assert result["status"] == "done"
        assert result["abnormal_count"] == 1
        assert result["trigger_optimization"] is True

    def test_test_runner_全部通过(self) -> None:
        step = Step(
            id="s1",
            action="test_run",
            inputs={"test_cases": [{"name": "t1", "status": "passed"}]},
        )
        dispatched, result = dispatch_to_role(
            step, lambda st: "ok", correlation_id="c", llm_client=_FakeLLM()
        )
        assert dispatched is True
        assert result["status"] == "done"
        assert result["passed"] == 1

    def test_test_runner_权限不足映射为不可重试失败(self) -> None:
        def _deny(st: Step) -> str:
            raise PermissionError("role 无 test_run 权限")

        step = Step(id="s1", action="test_run", inputs={})
        with pytest.raises(RuntimeError, match="权限不足") as excinfo:
            dispatch_to_role(step, _deny, correlation_id="c", llm_client=_FakeLLM())
        # 角色内部已自行重试过 → 标记不可重试，Router 不再重派。
        assert getattr(excinfo.value, "retryable", True) is False

    def test_data_analyst_样本不足出质量报告且视为成功(self) -> None:
        step = Step(id="s1", action="data_query", inputs={"missing_ratio": 0.5})
        dispatched, result = dispatch_to_role(
            step, lambda st: "ok", correlation_id="c", llm_client=_FakeLLM()
        )
        assert dispatched is True
        assert result["status"] == "quality_report"
        assert "不下结论" in result["quality_report"]

    def test_memory_keeper_未确认结论拒写长期记忆(self) -> None:
        step = Step(
            id="s1",
            action="memory_write",
            inputs={"content": "可能是这样（未确认）", "tier": "knowledge"},
        )
        with pytest.raises(ValueError, match="未确认"):
            dispatch_to_role(step, lambda st: "ok", correlation_id="c", llm_client=_FakeLLM())

    def test_memory_keeper_读取返回结构化条目(self) -> None:
        step = Step(
            id="s1",
            action="memory_read",
            inputs={"results": [{"content": "记录", "tier": "session"}]},
        )
        dispatched, result = dispatch_to_role(
            step, lambda st: "ok", correlation_id="c", llm_client=_FakeLLM()
        )
        assert dispatched is True
        assert result["status"] == "done"
        assert len(result["entries"]) == 1


class TestRunPlanWithLLM:
    @staticmethod
    def _plan(task_id: str, steps: dict[str, Step]) -> Plan:
        order = list(steps)
        plan = Plan(
            task_id=task_id,
            order=order,
            parallel_groups=[order],
            confirmed_by_user=False,
        )
        plan.validate_steps(steps)
        return plan

    def test_注入客户端时按agent角色执行(self) -> None:
        llm = _FakeLLM(json_result={"conclusions": ["用密钥产出的结论"]})
        steps = {"step-001": Step(id="step-001", action="summarize", inputs={"ctx": "x"})}
        results, _audit = run_plan("t1", self._plan("t1", steps), steps, llm_client=llm)
        assert results[0]["status"] == "done"
        assert "用密钥产出的结论" in str(results[0]["result"])
        assert llm.json_calls

    def test_无客户端时保持纯工具执行(self) -> None:
        steps = {"step-001": Step(id="step-001", action="summarize", inputs={})}
        results, _audit = run_plan("t1", self._plan("t1", steps), steps)
        # summarize 不是注册工具 → 纯工具路径下该步失败（与接入前一致）。
        assert results[0]["status"] == "failed"

    def test_未覆盖action不受注入影响(self) -> None:
        steps = {
            "step-001": Step(
                id="step-001",
                action="diff_preview",
                inputs={"old_content": "a", "new_content": "b"},
            )
        }
        results, _audit = run_plan("t1", self._plan("t1", steps), steps, llm_client=_FakeLLM())
        assert results[0]["status"] == "done"


class TestUnsupportedActionGuard:
    """「臆造工具」防护：action 无对应工具 → 明确报错且**不重派**（确定性失败）。"""

    @staticmethod
    def _plan(task_id: str, steps: dict[str, Step]) -> Plan:
        order = list(steps)
        plan = Plan(
            task_id=task_id,
            order=order,
            parallel_groups=[order],
            confirmed_by_user=False,
        )
        plan.validate_steps(steps)
        return plan

    def test_无对应工具_失败且不重派(self) -> None:
        # file_rename 非注册工具（file_delete 已补全）→ 命中防护
        steps = {
            "step-001": Step(
                id="step-001", action="file_rename", inputs={"path": "notes/a.md"}
            )
        }
        results, _audit = run_plan("t1", self._plan("t1", steps), steps)
        row = results[0]
        assert row["status"] == "failed"
        assert row["retries"] == 0  # 确定性失败 → 不重派
        assert "无对应工具" in row["error"]

    def test_无对应工具_报错含可用工具清单(self) -> None:
        steps = {"step-001": Step(id="step-001", action="bogus_tool", inputs={})}
        results, _audit = run_plan("t1", self._plan("t1", steps), steps)
        error = results[0]["error"]
        assert "无对应工具" in error
        assert "file_read" in error  # 列出可用工具，便于诊断

    def test_已注册工具不受防护影响(self) -> None:
        # file_list 是注册工具 → 走正常工具链路（此处目录不存在 → 失败原因应为工具校验，而非「无对应工具」）
        steps = {"step-001": Step(id="step-001", action="file_list", inputs={"path": "notes"})}
        results, _audit = run_plan("t1", self._plan("t1", steps), steps)
        assert "无对应工具" not in (results[0]["error"] or "")


# ── 路由：开关 → 执行阶段密钥注入 ────────────────────────────────


class TestApproveInjectsRuntimeKey:
    @staticmethod
    def _seed(client: TestClient, task_id: str, *, use_llm: bool) -> None:
        # 本类测的是**固定工作流**路径下「LLM 拆分」开关的注入语义；
        # agentic（默认）路径的密钥使用由 test_agentic_mode.py 覆盖，
        # 故这里显式指定 mode=workflow，避免默认值变化影响该断言的意图。
        client.post(
            f"/tasks/{task_id}/plan",
            json={"use_llm": use_llm, "mode": "workflow"},
            headers=_LOCAL_HEADERS,
        )
        entry = get_store().get(task_id)
        assert entry is not None
        steps = {"step-001": Step(id="step-001", action="summarize", inputs={"ctx": "x"})}
        plan = Plan(
            task_id=task_id,
            order=["step-001"],
            parallel_groups=[["step-001"]],
            confirmed_by_user=False,
        )
        plan.validate_steps(steps)
        entry.steps = steps
        entry.conductor.task_state.plan = plan

    def test_开关打开时注入运行时密钥客户端(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(routes_module, "build_llm_client_or_none", lambda *a, **k: None)
        llm = _FakeLLM(json_result={"conclusions": ["密钥生效"]})
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: llm)

        client = TestClient(create_app())
        resp = client.post("/tasks", json={"requirement": "demo"}, headers=_LOCAL_HEADERS)
        tid = resp.json()["task_id"]
        self._seed(client, tid, use_llm=True)

        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        results = resp.json()["execution_results"]
        assert results[0]["status"] == "done"
        assert "密钥生效" in results[0]["result"]

    def test_开关关闭时不注入(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(routes_module, "build_llm_client_or_none", lambda *a, **k: None)
        called: list[object] = []
        monkeypatch.setattr(
            routes_module, "get_llm_client", lambda **k: called.append(k) or _FakeLLM()
        )

        client = TestClient(create_app())
        resp = client.post("/tasks", json={"requirement": "demo"}, headers=_LOCAL_HEADERS)
        tid = resp.json()["task_id"]
        self._seed(client, tid, use_llm=False)

        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        assert called == []  # 开关关闭 → 不构造执行阶段客户端
