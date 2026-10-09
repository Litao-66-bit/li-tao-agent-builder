"""规划选项（模型 / 推理强度 / 高级 / 副结构自检）贯通测试。

对应前端底部栏四个控件 → POST /tasks/{id}/plan 请求字段 → 后端真实生效：
- model / temperature → LLM 客户端；
- advanced → 分解器提示词详细度；
- self_check → 执行阶段「计划 ↔ 执行结果」一致性自检。

本文件既测校验与透传，也测「不传时行为与扩展前完全一致」（向后兼容）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import deps as deps_module
from agent_builder.api import routes as routes_module
from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_store
from agent_builder.api.orchestrator import check_execution_consistency
from agent_builder.api.schemas import MAX_MODEL_CHARS, PlanRequest
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Plan, Step
from agent_builder.llm.client import DEFAULT_TEMPERATURE, LLMClient
from agent_builder.llm.config import LLMConfig
from agent_builder.roles.decomposer import Decomposer

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前后重置单例。"""
    reset_store()
    reset_api_key_store()
    yield
    reset_store()
    reset_api_key_store()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def _create_task(client: TestClient, requirement: str = "demo") -> str:
    resp = client.post("/tasks", json={"requirement": requirement}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 201
    return resp.json()["task_id"]


class TestPlanRequestSchema:
    """字段校验：非法输入必须在入口被拒（400/422），不进入下游。"""

    def test_默认值与扩展前一致(self) -> None:
        req = PlanRequest()
        assert req.use_llm is False
        assert req.model is None
        assert req.temperature is None
        assert req.advanced == "default"
        assert req.self_check is False
        assert req.role_brief is None

    @pytest.mark.parametrize("temperature", [0.0, 0.5, 1.0])
    def test_温度边界内接受(self, temperature: float) -> None:
        assert PlanRequest(temperature=temperature).temperature == temperature

    @pytest.mark.parametrize("temperature", [-0.1, 1.1, 2, -1])
    def test_温度越界被拒(self, temperature: float) -> None:
        with pytest.raises(ValueError):
            PlanRequest(temperature=temperature)

    @pytest.mark.parametrize("model", ["", "   ", "deep seek", "a\tb", "m" * (MAX_MODEL_CHARS + 1)])
    def test_非法模型名被拒(self, model: str) -> None:
        with pytest.raises(ValueError):
            PlanRequest(model=model)

    def test_模型名首尾空白被裁剪(self) -> None:
        assert PlanRequest(model="  deepseek-reasoner  ").model == "deepseek-reasoner"

    @pytest.mark.parametrize("advanced", ["default", "detailed", "concise"])
    def test_合法详细度接受(self, advanced: str) -> None:
        assert PlanRequest(advanced=advanced).advanced == advanced

    def test_非法详细度被拒(self, advanced: str = "verbose") -> None:
        with pytest.raises(ValueError):
            PlanRequest.model_validate({"advanced": advanced})

    @pytest.mark.parametrize("mode", ["off", "core", "full"])
    def test_合法角色简报档位接受(self, mode: str) -> None:
        assert PlanRequest(role_brief=mode).role_brief == mode

    @pytest.mark.parametrize("mode", ["verbose", "CORE", "", "none"])
    def test_非法角色简报档位被拒(self, mode: str) -> None:
        with pytest.raises(ValueError):
            PlanRequest.model_validate({"role_brief": mode})

    def test_接口层非法参数返回422(self, client: TestClient) -> None:
        tid = _create_task(client)
        for body in (
            {"use_llm": False, "temperature": 1.5},
            {"use_llm": False, "advanced": "verbose"},
            {"use_llm": False, "model": "bad model"},
            {"use_llm": False, "role_brief": "verbose"},
        ):
            resp = client.post(f"/tasks/{tid}/plan", json=body, headers=_LOCAL_HEADERS)
            assert resp.status_code == 422, body

    def test_旧请求体仍可用(self, client: TestClient) -> None:
        # 向后兼容：只传 use_llm（扩展前的唯一字段）
        tid = _create_task(client)
        resp = client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["pending_questions"]


class TestPlanOptionsThreaded:
    """接线正确：请求字段真的传到了 LLM 客户端与分解器。"""

    def test_模型与温度透传到LLM客户端(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, object]] = []

        def _spy(use_llm: bool, **kwargs: object) -> None:
            seen.append({"use_llm": use_llm, **kwargs})

        monkeypatch.setattr(routes_module, "build_llm_client_or_none", _spy)
        tid = _create_task(client)
        resp = client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": True, "model": "deepseek-reasoner", "temperature": 0.1},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        assert len(seen) == 1
        call = seen[0]
        assert call["use_llm"] is True
        assert call["model"] == "deepseek-reasoner"
        assert call["temperature"] == 0.1
        # 角色简报（阶段 1）与任务级计量：规划阶段以 decomposer 身份构造并挂载采集器。
        assert call["role"] == "decomposer"
        assert call["usage_sink"] is not None

    def test_角色简报档位透传到LLM客户端并写入选项(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, object]] = []

        def _spy(use_llm: bool, **kwargs: object) -> object:
            seen.append({"use_llm": use_llm, **kwargs})
            return None

        monkeypatch.setattr(routes_module, "build_llm_client_or_none", _spy)
        tid = _create_task(client)
        resp = client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": True, "role_brief": "core"},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        assert len(seen) == 1
        assert seen[0]["role_brief_mode"] == "core"
        entry = get_store().get(tid)
        assert entry is not None
        assert entry.options["role_brief"] == "core"

    def test_不传时档位为None走环境变量(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, object]] = []

        def _spy(use_llm: bool, **kwargs: object) -> object:
            seen.append({"use_llm": use_llm, **kwargs})
            return None

        monkeypatch.setattr(routes_module, "build_llm_client_or_none", _spy)
        tid = _create_task(client)
        resp = client.post(
            f"/tasks/{tid}/plan", json={"use_llm": True}, headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 200
        assert seen[0]["role_brief_mode"] is None

    def test_详细度透传到分解器(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        real = Decomposer.decompose

        def _spy(self, *args: object, **kwargs: object) -> object:
            seen.append(str(kwargs.get("detail_level")))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Decomposer, "decompose", _spy)
        tid = _create_task(client)
        resp = client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "advanced": "concise"},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        assert seen == ["concise"]

    def test_选项写入任务条目供执行阶段读取(self, client: TestClient) -> None:
        from agent_builder.api.deps import get_store

        tid = _create_task(client)
        client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "advanced": "detailed", "self_check": True, "temperature": 0.2},
            headers=_LOCAL_HEADERS,
        )
        entry = get_store().get(tid)
        assert entry is not None
        assert entry.options == {
            "model": None,
            "temperature": 0.2,
            "advanced": "detailed",
            "self_check": True,
            "use_llm": False,
            "council": False,
            "role_brief": None,
            # 执行形态与副结构门控（P1 新增；未传 mode 时默认 agentic）。
            "mode": "agentic",
            "sub_arch": False,
        }

    def test_未传时的默认选项(self, client: TestClient) -> None:
        from agent_builder.api.deps import get_store

        tid = _create_task(client)
        client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
        entry = get_store().get(tid)
        assert entry is not None
        assert entry.options["advanced"] == "default"
        assert entry.options["self_check"] is False
        assert entry.options["model"] is None
        assert entry.options["temperature"] is None
        assert entry.options["use_llm"] is False


class TestTaskUsageEndpoint:
    """只读 usage 端点：供 A/B 对照取 P1 成本指标。"""

    def test_未知任务返回404(self, client: TestClient) -> None:
        assert client.get("/tasks/nope/usage").status_code == 404

    def test_无用量返回空对象(self, client: TestClient) -> None:
        tid = _create_task(client)
        resp = client.get(f"/tasks/{tid}/usage")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_回读已落库的用量(self, client: TestClient) -> None:
        tid = _create_task(client)
        entry = get_store().get(tid)
        assert entry is not None
        entry.usage = {"calls": 1, "prompt_tokens": 10, "total_tokens": 14, "mode": "core"}
        resp = client.get(f"/tasks/{tid}/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["calls"] == 1
        assert data["total_tokens"] == 14
        assert data["mode"] == "core"


class TestLLMClientOptions:
    """模型 / 温度在 LLM 客户端层真实生效。"""

    def test_默认温度保持不变(self) -> None:
        assert LLMClient(LLMConfig()).temperature == DEFAULT_TEMPERATURE == 0.3

    def test_温度可覆盖(self) -> None:
        assert LLMClient(LLMConfig(), temperature=0.0).temperature == 0.0
        assert LLMClient(LLMConfig(), temperature=1.0).temperature == 1.0

    def test_模型可覆盖(self) -> None:
        client = LLMClient(LLMConfig(model="deepseek-reasoner"))
        assert client.model == "deepseek-reasoner"

    def test_deps_把覆盖项写进配置(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        @dataclass(slots=True)
        class _FakeConfig:
            api_key: str = ""
            base_url: str = ""
            model: str = ""

            @classmethod
            def from_env(cls) -> _FakeConfig:
                return cls(api_key="k", base_url="http://x", model="deepseek-chat")

        class _FakeClient:
            def __init__(
                self,
                config: object,
                *,
                temperature: float | None = None,
                role_brief: str = "",
                role: str = "",
                usage_sink: object = None,
            ) -> None:
                captured["config"] = config
                captured["temperature"] = temperature
                self.is_available = True

        monkeypatch.setattr(deps_module, "LLMConfig", _FakeConfig)
        monkeypatch.setattr(deps_module, "LLMClient", _FakeClient)

        client = deps_module.get_llm_client(model="gpt-4o", temperature=0.9)
        assert client is not None
        assert captured["config"].model == "gpt-4o"  # type: ignore[attr-defined]
        assert captured["temperature"] == 0.9

    def test_无密钥仍降级(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = LLMConfig(api_key="")
        monkeypatch.setattr(deps_module.LLMConfig, "from_env", classmethod(lambda cls: config))
        assert deps_module.get_llm_client(model="gpt-4o") is None


class TestDecomposerDetailLevel:
    """详细度影响 LLM 拆分提示词，且不改变后处理逻辑。"""

    class _FakeLLM:
        is_available = True

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete_json(self, prompt: str, schema_hint: str = "") -> dict[str, object]:
            self.prompts.append(prompt)
            return {"steps": [{"id": "step-001", "action": "diff_preview", "inputs": {}}]}

    def test_详细模式追加要求(self) -> None:
        fake = self._FakeLLM()
        Decomposer(correlation_id="c").decompose(
            "t1", "需求", llm_client=fake, detail_level="detailed"
        )
        assert "详细模式" in fake.prompts[0]

    def test_精简模式追加要求(self) -> None:
        fake = self._FakeLLM()
        Decomposer(correlation_id="c").decompose(
            "t1", "需求", llm_client=fake, detail_level="concise"
        )
        assert "精简模式" in fake.prompts[0]

    def test_默认模式不追加(self) -> None:
        fake = self._FakeLLM()
        Decomposer(correlation_id="c").decompose("t1", "需求", llm_client=fake)
        assert "详细模式" not in fake.prompts[0]
        assert "精简模式" not in fake.prompts[0]

    def test_非法详细度抛契约异常(self) -> None:
        with pytest.raises(AgentError):
            Decomposer(correlation_id="c").decompose("t1", "需求", detail_level="verbose")


class TestSelfCheckFunction:
    """副结构自检：纯计算的一致性核对。"""

    @staticmethod
    def _plan(task_id: str, order: list[str]) -> Plan:
        return Plan(
            task_id=task_id,
            order=order,
            parallel_groups=[order] if order else [],
            confirmed_by_user=True,
        )

    def test_一致时通过(self) -> None:
        steps = {"s1": Step(id="s1", action="diff_preview", inputs={})}
        report = check_execution_consistency(
            self._plan("t", ["s1"]),
            steps,
            [{"step_id": "s1", "status": "done", "error": None}],
        )
        assert report == {"passed": True, "checked": 1, "issues": []}

    def test_漏执行被检出(self) -> None:
        steps = {
            "s1": Step(id="s1", action="a", inputs={}),
            "s2": Step(id="s2", action="b", inputs={}),
        }
        report = check_execution_consistency(
            self._plan("t", ["s1", "s2"]),
            steps,
            [{"step_id": "s1", "status": "done"}],
        )
        assert report["passed"] is False
        assert report["issues"][0]["kind"] == "missing_execution"
        assert report["issues"][0]["detail"] == ["s2"]

    def test_越界执行被检出(self) -> None:
        steps = {"s1": Step(id="s1", action="a", inputs={})}
        report = check_execution_consistency(
            self._plan("t", ["s1"]),
            steps,
            [{"step_id": "s1", "status": "done"}, {"step_id": "s9", "status": "done"}],
        )
        kinds = [issue["kind"] for issue in report["issues"]]
        assert "unplanned_execution" in kinds

    def test_缺状态被检出(self) -> None:
        steps = {"s1": Step(id="s1", action="a", inputs={})}
        report = check_execution_consistency(
            self._plan("t", ["s1"]), steps, [{"step_id": "s1", "status": ""}]
        )
        assert [issue["kind"] for issue in report["issues"]] == ["missing_status"]

    def test_失败无原因被检出(self) -> None:
        steps = {"s1": Step(id="s1", action="a", inputs={})}
        report = check_execution_consistency(
            self._plan("t", ["s1"]), steps, [{"step_id": "s1", "status": "failed", "error": None}]
        )
        assert [issue["kind"] for issue in report["issues"]] == ["failed_without_error"]

    def test_失败带原因不报错(self) -> None:
        steps = {"s1": Step(id="s1", action="a", inputs={})}
        report = check_execution_consistency(
            self._plan("t", ["s1"]),
            steps,
            [{"step_id": "s1", "status": "failed", "error": "boom"}],
        )
        assert report["passed"] is True


class TestSelfCheckEndpoint:
    """端到端：开关开/关决定 TaskResponse.self_check 是否产出。"""

    @staticmethod
    def _seed_plan(client: TestClient, task_id: str, self_check: bool) -> None:
        """用纯计算工具作为唯一步骤，避免依赖文件系统。"""
        from agent_builder.api.deps import get_store

        client.post(
            f"/tasks/{task_id}/plan",
            json={"use_llm": False, "self_check": self_check},
            headers=_LOCAL_HEADERS,
        )
        entry = get_store().get(task_id)
        assert entry is not None
        steps = {
            "step-001": Step(
                id="step-001",
                action="diff_preview",
                inputs={"old_content": "a", "new_content": "b"},
            )
        }
        plan = Plan(
            task_id=task_id,
            order=["step-001"],
            parallel_groups=[["step-001"]],
            confirmed_by_user=False,
        )
        plan.validate_steps(steps)
        entry.steps = steps
        entry.conductor.task_state.plan = plan

    def test_开启时产出自检报告(self, client: TestClient) -> None:
        tid = _create_task(client)
        self._seed_plan(client, tid, self_check=True)
        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        report = resp.json()["self_check"]
        assert report is not None
        assert report["checked"] == 1
        assert report["passed"] is True
        assert report["issues"] == []

    def test_关闭时为null(self, client: TestClient) -> None:
        tid = _create_task(client)
        self._seed_plan(client, tid, self_check=False)
        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["self_check"] is None

    def test_未执行时自检不产出(self, client: TestClient) -> None:
        # 无步骤 → 不触发执行编排 → 自检也没有对象可核
        tid = _create_task(client)
        client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "self_check": True},
            headers=_LOCAL_HEADERS,
        )
        resp = client.post(f"/tasks/{tid}/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["self_check"] is None


class TestFrontendWiring:
    """前端确实把控件值传上来了（静态检查，防止 UI 与后端脱钩）。"""

    def _read(self, relative: str) -> str:
        return (_PROJECT_ROOT / relative).read_text(encoding="utf-8")

    def test_api层带规划选项参数(self) -> None:
        source = self._read("frontend/js/api.js")
        assert "planTask: (id, useLLM = false, options = {})" in source
        assert "...options" in source

    def test_页面持有六个控件(self) -> None:
        html = self._read("frontend/index.html")
        for element_id in (
            "modelSelect",
            "tempSlider",
            "advancedSelect",
            "selfCheckToggle",
            "subArchToggle",
            "councilToggle",
        ):
            assert f'id="{element_id}"' in html, element_id

    def test_页面不再自称占位(self) -> None:
        assert "待后端字段接入" not in self._read("frontend/index.html")

    def test_app组装并传参(self) -> None:
        source = self._read("frontend/js/app.js")
        assert "function readPlanOptions()" in source
        # 新 IA 仍是「底部栏控件 → readPlanOptions → /plan」的真实透传。
        assert "api.planTask(task.task_id, useLLM, planOptions)" in source
        assert "task.self_check" in source

    def test_app透传评审会与自检开关(self) -> None:
        source = self._read("frontend/js/app.js")
        assert "council: councilToggleEl?.checked || false," in source
        assert "self_check: selfCheckToggleEl?.checked || false," in source
        # 副结构门控同样随 /plan 透传（后端 PlanRequest.sub_arch）
        assert "sub_arch: subArchToggleEl?.checked || false," in source
        assert "options.model = model" in source
        assert "options.temperature = temperature" in source

    def test_副结构开关三态接线(self) -> None:
        source = self._read("frontend/js/app.js")
        html = self._read("frontend/index.html")
        # 三态刷新函数 + 无密钥时用原生 disabled（不靠 pointer-events 伪装）
        assert "function refreshSubArchChip()" in source
        assert "subArchToggleEl.disabled = !hasKey" in source
        assert "classList.toggle('is-active', on && hasKey)" in source
        assert "classList.toggle('is-disabled', !hasKey)" in source
        # 开关变化与密钥状态变化都要刷新（否则开了没反应 / 没密钥也显示可用）
        assert "addEventListener('change', refreshSubArchChip)" in source
        assert "refreshSubArchChip();" in source
        # 后果提示存在且是「状态告知」而非错误弹窗
        assert 'id="subArchHint"' in html
        assert 'role="status"' in html

    def test_一个名字只指一件事(self) -> None:
        """「副结构」只指副架构扩编；一致性核对改叫「执行自检」，不再共用同一个词。"""
        html = self._read("frontend/index.html")
        assert "副结构自检" not in html
        assert "执行自检" in html
        assert 'aria-label="执行自检"' in html
        assert 'aria-label="副结构（允许模型提议新角色）"' in html

