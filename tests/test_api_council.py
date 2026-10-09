"""评审会（council）测试：参会者选择 / 议题与密钥校验 / 收敛 / 端点 / 规划阶段自动开会。

行为约定：
- 无可用 LLM 密钥 → 拒绝发起（409），不产出空壳纪要；
- 参会者 = 核心名单 + 关键词补位 + 强制反方（有上限）；
- 多角色独立表态 → 规则式纪要（一致 / 分歧 / 未决 / 建议决议）；
- `/plan` 带 `council=true` 时先开会，纪要随 DecomposeResponse 返回并存回任务。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import routes as routes_module
from agent_builder.api.app import create_app
from agent_builder.api.council import (
    CORE_PARTICIPANTS,
    DEVIL_ADVOCATES,
    MAX_PARTICIPANTS,
    run_council,
    select_participants,
)
from agent_builder.api.deps import reset_store
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.contracts.errors import AgentError

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


class FakeLLM:
    """可脚本化的 LLM 客户端替身：按提示词里的角色名返回预设表态。"""

    is_available = True

    def __init__(self, opinions: dict | None = None, decision: str = "") -> None:
        self.opinions = opinions or {}
        self.decision = decision

    def complete_json(self, prompt: str, schema_hint: str = "") -> dict:
        if "建议决议" in prompt:
            return {"decision": self.decision or "建议按多数意见推进"}
        for role, opinion in self.opinions.items():
            if f"你的角色：{role}" in prompt:
                return opinion
        return {"stance": "support", "content": "同意", "evidence": []}


class UnavailableLLM:
    """未配置密钥的客户端（is_available=False）。"""

    is_available = False

    def complete_json(self, prompt: str, schema_hint: str = "") -> dict:
        return {}


@pytest.fixture(autouse=True)
def _reset_state():
    reset_store()
    reset_api_key_store()
    yield
    reset_store()
    reset_api_key_store()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def _create_task(client: TestClient, requirement: str = "实现评审会功能") -> str:
    resp = client.post("/tasks", json={"requirement": requirement}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 201
    return resp.json()["task_id"]


class TestSelectParticipants:
    def test_默认核心名单含强制反方(self) -> None:
        selected = select_participants("是否上线新功能")
        for role in CORE_PARTICIPANTS:
            assert role in selected
        for role in DEVIL_ADVOCATES:
            assert role in selected

    def test_关键词补位代码角色(self) -> None:
        assert "code_worker" in select_participants("重构 api 接口")

    def test_关键词补位数据角色(self) -> None:
        assert "data_analyst" in select_participants("统计报表口径")

    def test_显式指定优先且仍含反方(self) -> None:
        selected = select_participants("任意议题", ["code_worker"])
        assert "code_worker" in selected
        for role in DEVIL_ADVOCATES:
            assert role in selected

    def test_参会角色数量有上限(self) -> None:
        selected = select_participants("代码 文档 数据 测试 检索 审计 重构")
        assert len(selected) <= MAX_PARTICIPANTS


class TestRunCouncil:
    def test_无可用密钥时拒绝(self) -> None:
        with pytest.raises(AgentError) as exc:
            run_council("t-1", "议题", llm_client=UnavailableLLM())
        assert exc.value.error_name == "E_VALIDATION"
        assert "密钥" in exc.value.info.message

    def test_议题为空被拒(self) -> None:
        with pytest.raises(AgentError) as exc:
            run_council("t-1", "   ", llm_client=FakeLLM())
        assert exc.value.error_name == "E_VALIDATION"

    def test_轮数非法被拒(self) -> None:
        with pytest.raises(AgentError) as exc:
            run_council("t-1", "议题", llm_client=FakeLLM(), rounds=3)
        assert exc.value.error_name == "E_VALIDATION"

    def test_支持与反对并存时产出分歧与未决(self) -> None:
        llm = FakeLLM(
            opinions={
                "proposer": {"stance": "support", "content": "可以推进", "evidence": []},
                "impact_analyzer": {
                    "stance": "oppose",
                    "content": "回归风险高",
                    "evidence": [],
                },
                "fact_checker": {"stance": "oppose", "content": "来源未核验", "evidence": []},
                "summarizer": {"stance": "neutral", "content": "信息不足", "evidence": []},
            }
        )
        minutes = run_council("t-1", "是否上线", llm_client=llm)
        assert minutes["topic"] == "是否上线"
        assert minutes["oppose"]
        assert minutes["disagreements"]
        assert minutes["unresolved"]
        assert minutes["decision_note"]
        assert minutes["rounds"] == 1

    def test_第二轮取最新表态(self) -> None:
        llm = FakeLLM(
            opinions={"proposer": {"stance": "support", "content": "同意", "evidence": []}}
        )
        minutes = run_council("t-1", "议题", llm_client=llm, rounds=2)
        assert minutes["rounds"] == 2

    def test_全部角色无有效表态时拒绝(self) -> None:
        class EmptyLLM:
            is_available = True

            def complete_json(self, prompt: str, schema_hint: str = "") -> dict:
                return {}

        with pytest.raises(AgentError) as exc:
            run_council("t-1", "议题", llm_client=EmptyLLM())
        assert exc.value.error_name == "E_VALIDATION"

    def test_部分角色失败记为缺席(self) -> None:
        class PartialLLM:
            is_available = True

            def complete_json(self, prompt: str, schema_hint: str = "") -> dict:
                if "你的角色：proposer" in prompt:
                    return {"stance": "support", "content": "可以", "evidence": []}
                return {}

        minutes = run_council("t-1", "议题", llm_client=PartialLLM())
        assert minutes["absent"]
        assert minutes["support"]


class TestCouncilEndpoint:
    def test_无密钥返回409(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **kwargs: None)
        tid = _create_task(client)
        resp = client.post(f"/tasks/{tid}/council", json={}, headers=_LOCAL_HEADERS)
        assert resp.status_code == 409
        assert "密钥" in str(resp.json())

    def test_任务不存在返回404(self, client: TestClient) -> None:
        resp = client.post("/tasks/nope/council", json={}, headers=_LOCAL_HEADERS)
        assert resp.status_code == 404

    def test_有密钥时返回纪要并存回任务(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **kwargs: FakeLLM())
        tid = _create_task(client, "是否上线")
        resp = client.post(
            f"/tasks/{tid}/council",
            json={"topic": "是否上线", "rounds": 1},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["topic"] == "是否上线"
        assert body["participants"]
        detail = client.get(f"/tasks/{tid}", headers=_LOCAL_HEADERS).json()
        assert detail["council"]["topic"] == "是否上线"


class TestPlanAutoCouncil:
    def test_规划阶段自动开会并回传纪要(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **kwargs: FakeLLM())
        tid = _create_task(client, "是否上线新功能")
        resp = client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "council": True},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["council"]["topic"] == "是否上线新功能"

    def test_未开启时不开会(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **kwargs: FakeLLM())
        tid = _create_task(client)
        resp = client.post(
            f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 200
        assert resp.json()["council"] is None

    def test_开启但无密钥返回409(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **kwargs: None)
        tid = _create_task(client)
        resp = client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "council": True},
            headers=_LOCAL_HEADERS,
        )
        assert resp.status_code == 409
