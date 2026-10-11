"""卡 46b：底部开关**如实标注** —— 生效的说生效，不适用的直说"本模式不支持"。

本卡只补事实、不动模型控制权。三条不变量：

1. ``self_check`` 在 **agentic** 下也真的生效（以前只在固定工作流路径算过 → 空转）；
2. ``/plan`` 回传 ``effective_options``：每项都带 applied + 原因，
   agentic 下 ``advanced`` / ``use_llm`` 必须**如实标注为不适用**，不能假装生效；
3. ``deciders.py`` / ``agent_loop.py`` 的决策逻辑**一行未动**（有测试钉住）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import routes as routes_module
from agent_builder.api.app import create_app
from agent_builder.api.deps import reset_project_store, reset_store
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.api.store import TaskEntry
from agent_builder.contracts.schemas import Plan, Step, TaskState
from agent_builder.roles.conductor import Conductor

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


class _FakeLLM:
    """假 LLM：依次吐出决策 payload（不联网）。"""

    def __init__(self, decisions: list) -> None:
        self.decisions = list(decisions)
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, schema_hint: str = ""):
        self.prompts.append(prompt)
        return self.decisions.pop(0) if self.decisions else {"kind": "final"}

    def chat(self, messages, **kwargs):
        """``_draft_plan_text`` 走 chat（只用于展示，失败也不影响主流程）。"""
        return "1. 看一眼工作区\n验证：文件清单"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BUILDER_PROJECTS_FILE", str(tmp_path / "projects.json"))
    reset_project_store()
    reset_store()
    reset_api_key_store()
    c = TestClient(create_app())
    resp = c.post("/projects", json={"path": str(tmp_path)}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 200, resp.text
    yield c
    reset_project_store()
    reset_store()
    reset_api_key_store()


@pytest.fixture
def keyed_client(tmp_path, monkeypatch):
    """**有可用密钥**的客户端 —— agentic 分支的必要条件。

    为什么必须区分：agentic 分支的条件是"``mode=agentic`` **且有可用模型**"；
    没密钥时后端会**自动回退固定工作流**（那会真的去跑分解器），
    于是"agentic 下 advanced 不适用"这类断言根本没被走到。
    这里存一把格式合法的密钥；真正的模型调用仍被 monkeypatch 成假客户端，不联网。
    """
    monkeypatch.setenv("AGENT_BUILDER_PROJECTS_FILE", str(tmp_path / "projects.json"))
    reset_project_store()
    reset_store()
    reset_api_key_store()
    c = TestClient(create_app())
    resp = c.post("/projects", json={"path": str(tmp_path)}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 200, resp.text
    saved = c.post(
        "/settings/api-key", json={"api_key": "sk-" + "t" * 24}, headers=_LOCAL_HEADERS
    )
    assert saved.status_code == 200, saved.text
    yield c
    reset_project_store()
    reset_store()
    reset_api_key_store()


def _plan(client: TestClient, **payload) -> dict:
    tid = client.post(
        "/tasks", json={"requirement": "看看工作区"}, headers=_LOCAL_HEADERS
    ).json()["task_id"]
    resp = client.post(
        f"/tasks/{tid}/plan", json={"use_llm": False, **payload}, headers=_LOCAL_HEADERS
    )
    assert resp.status_code == 200, resp.text
    return {"task_id": tid, "body": resp.json()}


def _by_option(body: dict) -> dict[str, dict]:
    return {item["option"]: item for item in body["effective_options"]}


class TestEffectiveOptionsReport:
    def test_响应带生效清单(self, client: TestClient) -> None:
        body = _plan(client, mode="workflow")["body"]
        options = _by_option(body)

        # 六个底部/相关选项都要有出处，界面才不会"没有出处的开关"。
        assert set(options) == {
            "council",
            "self_check",
            "sub_arch",
            "use_llm",
            "advanced",
            "capabilities",
        }
        for name, item in options.items():
            assert item["label"], name
            assert item["detail"], name
            assert isinstance(item["applied"], bool)

    def test_agentic下advanced如实标注不适用(self, keyed_client: TestClient) -> None:
        """默认模式（agentic）下「高级」无处可施 —— 必须直说，而不是假装生效。

        用带密钥的客户端：否则后端会回退固定工作流（真的跑分解器），断言就走偏了。
        """
        body = _plan(keyed_client, mode="agentic", advanced="detailed")["body"]
        advanced = _by_option(body)["advanced"]

        assert advanced["applied"] is False
        assert "不适用" in advanced["detail"]
        assert advanced["value"] == "detailed"

    def test_workflow下advanced真的生效(self, client: TestClient) -> None:
        """反证：真跑了分解器时它就是生效的（不能一刀切说"永远不适用"）。"""
        body = _plan(client, mode="workflow", advanced="detailed")["body"]
        advanced = _by_option(body)["advanced"]

        assert advanced["applied"] is True
        assert "detailed" in advanced["detail"]

    def test_agentic下use_llm如实说明不影响规划(self, keyed_client: TestClient) -> None:
        body = _plan(keyed_client, mode="agentic", use_llm=True)["body"]
        use_llm = _by_option(body)["use_llm"]

        assert use_llm["applied"] is False
        assert "agentic" in use_llm["detail"]

    def test_无密钥时agentic回退固定工作流且如实标注(
        self, client: TestClient
    ) -> None:
        """无密钥 → 真跑了分解器，所以 advanced / use_llm **确实生效**（不是撒谎）。"""
        body = _plan(client, mode="agentic", advanced="detailed")["body"]

        assert _by_option(body)["advanced"]["applied"] is True

    def test_自检开关在任何模式都算生效(self, client: TestClient) -> None:
        for mode in ("agentic", "workflow"):
            body = _plan(client, mode=mode, self_check=True)["body"]
            assert _by_option(body)["self_check"]["applied"] is True, mode

    def test_未开启的选项如实说未开启(self, client: TestClient) -> None:
        body = _plan(client, mode="agentic")["body"]
        assert _by_option(body)["council"]["applied"] is False
        assert "未开启" in _by_option(body)["council"]["detail"]

    def test_能力开关的如实说明(self, client: TestClient) -> None:
        on = _plan(client, mode="agentic", capabilities=["file"])["body"]
        assert _by_option(on)["capabilities"]["applied"] is True
        assert "白名单" in _by_option(on)["capabilities"]["detail"]

        off = _plan(client, mode="agentic")["body"]
        assert "全部能力启用" in _by_option(off)["capabilities"]["detail"]

    def test_副结构在固定工作流下如实标注无效果(self, client: TestClient) -> None:
        body = _plan(client, mode="workflow", sub_arch=True)["body"]
        sub_arch = _by_option(body)["sub_arch"]

        assert sub_arch["applied"] is False
        assert "固定工作流" in sub_arch["detail"]


class TestSelfCheckWorksInAgentic:
    """卡 46b 的核心修复：``self_check`` 以前在 agentic 下**永远为空**。"""

    def _agentic_task(self, client: TestClient, tmp_path, *, self_check: bool) -> str:
        tid = client.post(
            "/tasks", json={"requirement": "预览差异"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "mode": "agentic", "self_check": self_check},
            headers=_LOCAL_HEADERS,
        )
        return tid

    def test_勾选自检后agentic任务收尾带self_check(
        self, keyed_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        fake = _FakeLLM(
            [
                {
                    "thought": "先预览差异",
                    "next": {
                        "kind": "agent",
                        # 用 operator 身份：diff_preview 在它的白名单里
                        # （code_worker 不接受该动作 → 决策会被判 invalid，测的不是自检了）。
                        "role": "operator",
                        "action": "diff_preview",
                        "inputs": {"old_content": "a", "new_content": "b"},
                    },
                },
                {"kind": "final", "answer": "看完了"},
            ]
        )
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = self._agentic_task(keyed_client, tmp_path, self_check=True)

        body = keyed_client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"] is not None, "必须是 agentic 路径（有密钥才会走循环）"
        assert body["self_check"] is not None, "agentic 下「执行自检」必须真的生效（卡 46b）"
        assert body["self_check"]["checked"] >= 1
        assert isinstance(body["self_check"]["issues"], list)

    def test_不勾选自检则不带(
        self, keyed_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        fake = _FakeLLM([{"kind": "final", "answer": "够了"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = self._agentic_task(keyed_client, tmp_path, self_check=False)

        body = keyed_client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["self_check"] is None

    def test_自检不影响终止原因(
        self, keyed_client: TestClient, monkeypatch, tmp_path
    ) -> None:
        """附加核对**不参与决策**：开着自检不会改变收敛结果（不许降灵活性）。"""
        fake = _FakeLLM([{"kind": "final", "answer": "收尾"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = self._agentic_task(keyed_client, tmp_path, self_check=True)

        body = keyed_client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["stopped_reason"] == "final"
        assert body["status"] == "verifying"

    def test_自检报告如实暴露漏执行(self, client: TestClient, tmp_path) -> None:
        """自检是**真核对**，不是空壳：计划里有步骤没执行时 issues 必须报出来。"""
        conductor = Conductor(task_state=TaskState(task_id="t-manual"), correlation_id="t-manual")
        entry = TaskEntry(conductor=conductor, requirement="x")
        entry.options = {"self_check": True}
        plan = Plan(task_id="t-manual", order=["s1"], parallel_groups=[["s1"]])
        steps = {"s1": Step(id="s1", action="diff_preview")}
        plan.validate_steps(steps)
        conductor.task_state.plan = plan
        entry.steps = steps
        entry.execution_results = []  # 计划有、执行结果空 → 漏执行

        routes_module._run_self_check(entry)

        assert entry.self_check is not None
        assert entry.self_check["passed"] is False
        kinds = {item["kind"] for item in entry.self_check["issues"]}
        assert "missing_execution" in kinds


class TestFlexibilityUntouched:
    """硬约束：本卡只补事实，**不许动模型控制权**。"""

    def test_决策与循环模块零改动(self) -> None:
        """用源码事实钉住：这两个模块里不得出现"按选项改决策"的痕迹。

        （真正的保护是"本卡没改这两个文件"，这里再补一层可见断言防止以后被顺手改坏。）
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "agent_builder" / "api"
        for name in ("deciders.py", "agent_loop.py"):
            source = (root / name).read_text(encoding="utf-8")
            assert "self_check" not in source, f"{name} 不该感知自检开关（它不参与决策）"
            assert "effective_options" not in source, f"{name} 不该感知生效清单"

    def test_自检只读不写执行结果(self) -> None:
        conductor = Conductor(task_state=TaskState(task_id="t2"), correlation_id="t2")
        entry = TaskEntry(conductor=conductor, requirement="y")
        entry.options = {"self_check": True}
        plan = Plan(task_id="t2", order=["s1"], parallel_groups=[["s1"]])
        steps = {"s1": Step(id="s1", action="diff_preview")}
        plan.validate_steps(steps)
        conductor.task_state.plan = plan
        entry.steps = steps
        entry.execution_results = [
            {"step_id": "s1", "action": "diff_preview", "status": "done", "summary": "ok"}
        ]
        before = [dict(row) for row in entry.execution_results]

        routes_module._run_self_check(entry)

        assert entry.execution_results == before, "自检只能读，不许改执行结果"
        assert entry.self_check["passed"] is True
