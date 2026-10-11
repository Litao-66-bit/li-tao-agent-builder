"""卡 50：「不满意就改」的闭环 —— 在**同一个任务**上追加修改要求。

用户痛点原话："做的不满意也没法改"。此前只能在对话框里再发一句，而那是**新建任务**：
模型从零开始，把工作区里已经做好的东西重做一遍。

本文件守住四件事：

1. 追加指令**不新建任务**（``task_id`` 不变、任务数不变）；
2. 追加指令**与原始需求分开**进提示词（模型看得见"这是在已有产物上改"）；
3. 追加要求**真的执行**（预算另起一轮，不会被上一轮烧光的预算掐死）；
4. 不是所有状态都能追加：终态（delivered / failed）与执行中要如实拒绝并给下一步。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import routes as routes_module
from agent_builder.api.app import create_app
from agent_builder.api.deciders import LoopContext, build_decision_prompt
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.secrets import reset_api_key_store

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


class _FakeLLM:
    """假 LLM：依次吐出决策 payload（不联网）。"""

    def __init__(self, decisions: list[Any]) -> None:
        self.decisions = list(decisions)
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, schema_hint: str = "") -> Any:
        self.prompts.append(prompt)
        return self.decisions.pop(0) if self.decisions else {"kind": "final"}

    def chat(self, messages, **kwargs):
        return "1. 先看一眼工作区\n验证：文件清单"


def _write_step(path: str, content: str) -> dict[str, Any]:
    return {
        "thought": "把内容写进文件",
        "next": {
            "kind": "agent",
            "role": "code_worker",
            "action": "file_write",
            "inputs": {"path": path, "content": content, "overwrite": True},
        },
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """带可用密钥的隔离客户端（agentic 循环要真跑起来）。"""
    monkeypatch.setenv("AGENT_BUILDER_PROJECTS_FILE", str(tmp_path / "projects.json"))
    reset_project_store()
    reset_store()
    reset_api_key_store()
    c = TestClient(create_app())
    resp = c.post("/projects", json={"path": str(tmp_path)}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 200, resp.text
    saved = c.post(
        "/settings/api-key", json={"api_key": "sk-" + "i" * 24}, headers=_LOCAL_HEADERS
    )
    assert saved.status_code == 200, saved.text
    yield c
    reset_project_store()
    reset_store()
    reset_api_key_store()


def _run_to_verifying(
    client: TestClient, monkeypatch, tmp_path, *, name: str = "note.txt"
) -> tuple[str, Any]:
    """跑一个任务到 ``verifying``（收敛），返回 (task_id, 目标路径)。"""
    target = tmp_path / name
    fake = _FakeLLM([_write_step(str(target), "第一版"), {"kind": "final", "answer": "写好了"}])
    monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
    tid = client.post(
        "/tasks", json={"requirement": f"把内容写进 {name}"}, headers=_LOCAL_HEADERS
    ).json()["task_id"]
    client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
    first = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
    assert first["status"] == "verifying", first
    assert target.read_text(encoding="utf-8") == "第一版"
    return tid, target


class TestInstructKeepsTask:
    def test_追加修改不新建任务(self, client: TestClient, monkeypatch, tmp_path) -> None:
        """核心验收：同一任务继续说"改成…"，任务数不变、task_id 不变。"""
        tid, target = _run_to_verifying(client, monkeypatch, tmp_path)
        tasks_before = set(client.get("/tasks", headers=_LOCAL_HEADERS).json())

        fake = _FakeLLM(
            [_write_step(str(target), "第二版"), {"kind": "final", "answer": "改好了"}]
        )
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        body = client.post(
            f"/tasks/{tid}/instruct",
            json={"instruction": "把内容改成第二版"},
            headers=_LOCAL_HEADERS,
        ).json()

        assert body["task_id"] == tid
        assert set(client.get("/tasks", headers=_LOCAL_HEADERS).json()) == tasks_before
        assert target.read_text(encoding="utf-8") == "第二版"
        assert body["status"] == "verifying"

    def test_追加要求被记账(self, client: TestClient, monkeypatch, tmp_path) -> None:
        tid, _target = _run_to_verifying(client, monkeypatch, tmp_path)
        fake = _FakeLLM([{"kind": "final", "answer": "好"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)

        client.post(
            f"/tasks/{tid}/instruct", json={"instruction": "把标题改大"}, headers=_LOCAL_HEADERS
        )
        client.post(
            f"/tasks/{tid}/instruct", json={"instruction": "再加一行说明"}, headers=_LOCAL_HEADERS
        )

        entry = get_store().get(tid)
        assert entry.follow_ups == ["把标题改大", "再加一行说明"], "追加要求要按顺序留痕"
        # 卡 50 验收：还要能被**回看**（响应里带出去，不只存在内存里）。
        detail = client.get(f"/tasks/{tid}", headers=_LOCAL_HEADERS).json()
        assert detail["follow_ups"] == ["把标题改大", "再加一行说明"]

    def test_追加要求进提示词且与原始需求分开(
        self, client: TestClient, monkeypatch, tmp_path
    ) -> None:
        """验收 2：模型必须看得出"这是在已有产物上改"，而不是一条新需求。"""
        tid, _target = _run_to_verifying(client, monkeypatch, tmp_path)
        fake = _FakeLLM([{"kind": "final", "answer": "好"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)

        client.post(
            f"/tasks/{tid}/instruct",
            json={"instruction": "把 result.txt 的标题改成二级标题"},
            headers=_LOCAL_HEADERS,
        )

        prompt = fake.prompts[-1]
        assert "用户需求：" in prompt
        assert "【用户追加的修改要求" in prompt
        assert "把 result.txt 的标题改成二级标题" in prompt
        # 关键：明确要求"在已有产物上改、不要从零重做"
        assert "不要从零重做" in prompt

    def test_追加要求真的执行而不是被预算掐死(
        self, client: TestClient, monkeypatch, tmp_path
    ) -> None:
        """验收 3：跑过一轮的任务（预算已消耗）追加修改后仍必须真的动手。"""
        tid, target = _run_to_verifying(client, monkeypatch, tmp_path)

        # 把上一轮的预算**摆成已用满**：若沿用旧预算，追加的指令会被闸门直接判停。
        entry = get_store().get(tid)
        assert entry.loop_state is not None
        entry.loop_state.budget.steps_used = entry.loop_state.budget.max_steps

        fake = _FakeLLM(
            [_write_step(str(target), "改过了"), {"kind": "final", "answer": "改好了"}]
        )
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        body = client.post(
            f"/tasks/{tid}/instruct",
            json={"instruction": "改成『改过了』"},
            headers=_LOCAL_HEADERS,
        ).json()

        assert target.read_text(encoding="utf-8") == "改过了", "追加修改必须真的执行"
        assert body["loop_state"]["budget"]["steps_used"] >= 1

    def test_可以连续追加多轮(self, client: TestClient, monkeypatch, tmp_path) -> None:
        tid, target = _run_to_verifying(client, monkeypatch, tmp_path)
        for version in ("v2", "v3", "v4"):
            fake = _FakeLLM(
                [_write_step(str(target), version), {"kind": "final", "answer": "好"}]
            )
            # 用工厂函数把 fake 绑死：直接写 lambda 会晚绑定到循环最后一次的值（ruff B023）。
            def _client_factory(_fake: Any = fake):
                return lambda **k: _fake

            monkeypatch.setattr(routes_module, "get_llm_client", _client_factory())
            resp = client.post(
                f"/tasks/{tid}/instruct",
                json={"instruction": f"改成 {version}"},
                headers=_LOCAL_HEADERS,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "verifying"
            assert target.read_text(encoding="utf-8") == version
        assert get_store().get(tid).follow_ups == ["改成 v2", "改成 v3", "改成 v4"]

    def test_连续追加不会撞上系统重试上限(
        self, client: TestClient, monkeypatch, tmp_path
    ) -> None:
        """回归：追加要求是**用户的新干预**，不该消耗契约里的 retry_count。

        实测踩到：retry_count 被 verify_failed 逐步推到 MAX_RETRY，第 3 次追加直接 abort。
        """
        tid, _target = _run_to_verifying(client, monkeypatch, tmp_path)
        fake = _FakeLLM([{"kind": "final", "answer": "好"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)

        for index in range(4):
            resp = client.post(
                f"/tasks/{tid}/instruct",
                json={"instruction": f"第 {index} 次调整"},
                headers=_LOCAL_HEADERS,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] != "failed", f"第 {index + 1} 次追加被判失败了"


class TestInstructGuards:
    def test_空指令被拒(self, client: TestClient, monkeypatch, tmp_path) -> None:
        tid, _ = _run_to_verifying(client, monkeypatch, tmp_path)
        resp = client.post(
            f"/tasks/{tid}/instruct", json={"instruction": "   "}, headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 400
        assert get_store().get(tid).follow_ups == []

    def test_任务不存在返回404(self, client: TestClient) -> None:
        resp = client.post(
            "/tasks/nope/instruct", json={"instruction": "改一下"}, headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 404

    def test_未执行过的任务被拒(self, client: TestClient) -> None:
        tid = client.post(
            "/tasks", json={"requirement": "x"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        resp = client.post(
            f"/tasks/{tid}/instruct", json={"instruction": "改一下"}, headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 409
        assert "还没执行过" in resp.json()["detail"]

    def test_已交付的任务如实拒绝并给下一步(
        self, client: TestClient, monkeypatch, tmp_path
    ) -> None:
        """终态没有回执行的转换 —— 如实说明，不偷偷改契约、不硬塞状态。"""
        tid, _ = _run_to_verifying(client, monkeypatch, tmp_path)
        delivered = client.post(f"/tasks/{tid}/deliver", headers=_LOCAL_HEADERS)
        assert delivered.status_code == 200
        assert delivered.json()["status"] == "delivered"

        resp = client.post(
            f"/tasks/{tid}/instruct", json={"instruction": "再改一版"}, headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 409
        assert "另起一个新任务" in resp.json()["detail"]
        assert get_store().get(tid).follow_ups == [], "被拒的指令不该留痕"

    def test_执行中拒绝(self, client: TestClient, monkeypatch, tmp_path) -> None:
        tid, _ = _run_to_verifying(client, monkeypatch, tmp_path)
        entry = get_store().get(tid)
        entry.running = True  # 模拟后台线程正在跑

        resp = client.post(
            f"/tasks/{tid}/instruct", json={"instruction": "改一下"}, headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 409
        assert "正在执行" in resp.json()["detail"]

    def test_暂停中的任务可以追加(self, client: TestClient, monkeypatch, tmp_path) -> None:
        """到点暂停（interrupted）也是"跑过一轮"，允许继续改。"""
        victim = tmp_path / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        fake = _FakeLLM(
            [
                {
                    "thought": "删掉它",
                    "next": {
                        "kind": "agent",
                        "role": "operator",
                        "action": "file_delete",
                        "inputs": {"path": str(victim)},
                    },
                }
            ]
        )
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = client.post(
            "/tasks", json={"requirement": "删掉 victim.txt"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        client.post(f"/tasks/{tid}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
        paused = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert paused["status"] == "interrupted"

        fake2 = _FakeLLM([{"kind": "final", "answer": "算了不改"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake2)
        body = client.post(
            f"/tasks/{tid}/instruct",
            json={"instruction": "换成把内容改成 hello"},
            headers=_LOCAL_HEADERS,
        ).json()

        assert body["status"] in ("verifying", "executing", "interrupted")
        assert get_store().get(tid).follow_ups == ["换成把内容改成 hello"]


class TestPromptRendering:
    """提示词渲染的纯函数级断言（不依赖整条链路）。"""

    def test_没有追加要求时不出现该段(self) -> None:
        from agent_builder.api.role_catalog import build_catalog

        context = LoopContext(requirement="做个东西", catalog=build_catalog(sub_arch=False))
        prompt = build_decision_prompt(context)

        assert "用户追加的修改要求" not in prompt

    def test_multiple追加要求按顺序渲染(self) -> None:
        from agent_builder.api.role_catalog import build_catalog

        context = LoopContext(
            requirement="做个东西",
            catalog=build_catalog(sub_arch=False),
            follow_ups=["第一条", "第二条"],
        )
        prompt = build_decision_prompt(context)

        assert "1. 第一条" in prompt
        assert "2. 第二条" in prompt
        assert prompt.index("第一条") < prompt.index("第二条")
