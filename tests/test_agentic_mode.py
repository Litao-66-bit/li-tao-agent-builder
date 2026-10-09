"""agentic 模式端到端：``/plan(mode=agentic)`` → ``/run`` 走模型循环。

覆盖（全部注入假 LLM 客户端，不碰网络、不碰真实模型）：
- 模型决策多步 → 落 ``loop-NNN`` 步骤 + 执行结果 + ``loop_state`` → 收敛转 verifying；
- 高风险动作「到点暂停」→ ``interrupted`` + ``pending_approval``，未写盘；
- ``/resume`` 放行后**原样重放那一步**（同一编号），写盘并转 verifying；
- 无可用密钥 → 自动回退固定工作流（行为与今天一致）；
- 决策非法 → 回退静态映射（``decided_by=static``）；
- **解析全程失败 → 停下而绝不伪装成 final**（最重要的安全断言）；
- ③ 规划阶段去工作流化：``/plan(agentic)`` 不再预先跑 Decomposer 出 DAG，
  ``/run`` 可直接带着原始需求进循环；
- ① 结论区：收敛那轮的 ``thought`` 落成 ``final_answer`` / ``conclusion``（不伪装成结论）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import routes as routes_module
from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.secrets import reset_api_key_store

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


class _FakeLLM:
    """假 LLM 客户端：``complete_json`` 依次吐出脚本里的决策 payload。"""

    def __init__(self, decisions: list[Any]) -> None:
        self.decisions = list(decisions)
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, schema_hint: str = "") -> Any:
        self.prompts.append(prompt)
        if not self.decisions:
            return {"kind": "final"}
        return self.decisions.pop(0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离客户端：项目注册表落临时文件，工作区切到 tmp_path。"""
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


def _plan_agentic(client: TestClient, requirement: str = "跑一步看看") -> str:
    """建任务 + ``/plan(mode=agentic)``（不依赖 LLM 拆分，agentic 不需要预先有计划）。"""
    resp = client.post("/tasks", json={"requirement": requirement}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 201, resp.text
    tid = resp.json()["task_id"]
    resp = client.post(
        f"/tasks/{tid}/plan",
        json={"use_llm": False, "mode": "agentic"},
        headers=_LOCAL_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return tid


def _diff_preview() -> dict[str, Any]:
    """离线可执行的一步：diff_preview 是纯计算，operator 有权使用。"""
    return {
        "thought": "先预览差异",
        "next": {
            "kind": "agent",
            "role": "operator",
            "action": "diff_preview",
            "inputs": {"old_content": "a", "new_content": "b"},
        },
    }


def _fail_read() -> dict[str, Any]:
    """离线执行但**必然失败**的一步：对不存在的路径用 file_read。"""
    return {
        "thought": "先读一下目录",
        "next": {
            "kind": "agent",
            "role": "code_worker",
            "action": "file_read",
            "inputs": {"path": "no_such_dir"},
        },
    }


def _file_write(path: str) -> dict[str, Any]:
    """高风险一步（需放行）：file_write 属 code_worker 的高风险工具。"""
    return {
        "thought": "把结论落盘",
        "next": {
            "kind": "agent",
            "role": "code_worker",
            "action": "file_write",
            "inputs": {"path": path, "content": "hello"},
        },
    }


class TestAgenticRun:
    def test_模型决策一步后收敛(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeLLM([_diff_preview(), {"kind": "final", "thought": "够了"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client)

        resp = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["status"] == "verifying"  # 全 done → 转验证
        row = body["execution_results"][0]
        assert row["step_id"] == "loop-001"
        assert row["action"] == "diff_preview"
        assert row["status"] == "done"
        assert row["summary"]  # 人话摘要
        assert row["thought"] == "先预览差异"  # 决策理由
        # 步骤明细写进 steps（回看用）+ 循环上下文可回传
        assert body["steps"]["loop-001"]["action"] == "diff_preview"
        assert body["loop_state"]["stopped_reason"] == "final"
        assert body["loop_state"]["decided_by"] == "llm"
        assert len(body["loop_state"]["rounds"]) == 1
        # 决策提示词里带上了角色目录（门控关闭 → 不含副架构）
        assert "可用角色" in fake.prompts[0]
        assert "auditor" not in fake.prompts[0]

    def test_提示词按副结构开关注入(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeLLM([{"kind": "final"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        resp = client.post("/tasks", json={"requirement": "看看"}, headers=_LOCAL_HEADERS)
        tid = resp.json()["task_id"]
        client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "mode": "agentic", "sub_arch": True},
            headers=_LOCAL_HEADERS,
        )

        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)

        assert "auditor" in fake.prompts[0]  # 开启后目录里出现副架构
        assert "kind=propose" in fake.prompts[0]


class TestAgenticApprovalGate:
    def test_高风险动作到点暂停且未写盘(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        target = tmp_path / "out.txt"
        fake = _FakeLLM([_file_write(str(target)), {"kind": "final"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client, "写文件")

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["status"] == "interrupted"
        assert body["pending_approval"]["tools"] == ["file_write"]
        assert body["pending_approval"]["steps"][0]["step_id"] == "loop-001"
        assert body["execution_results"][0]["status"] == "pending_approval"
        assert body["loop_state"]["pending_decision"]["action"] == "file_write"
        assert not target.exists()  # 未获放行 → 未写盘

    def test_放行后原样重放并写盘(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        target = tmp_path / "out.txt"
        fake = _FakeLLM([_file_write(str(target)), {"kind": "final"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client, "写文件")
        client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)
        prompts_before = len(fake.prompts)

        body = client.post(
            f"/tasks/{tid}/resume",
            json={"approved_tools": ["file_write"]},
            headers=_LOCAL_HEADERS,
        ).json()

        assert body["status"] == "verifying"
        assert body["pending_approval"] is None
        assert target.read_text(encoding="utf-8") == "hello"
        # 重放那一步**不消耗新决策**（只多问一次，用于决定再下一步 → 脚本给 final）。
        # 反证：若 resume 是重新问模型而不是重放，脚本第 2 项是 final，就不会执行 file_write、
        # 文件也不会被写出来 —— 上面 write 断言已经把这条钉住。
        assert len(fake.prompts) == prompts_before + 1
        rows = [r for r in body["execution_results"] if r["step_id"] == "loop-001"]
        assert len(rows) == 1
        assert rows[0]["status"] == "done"
        assert rows[0]["action"] == "file_write"
        assert body["loop_state"]["pending_decision"] is None

    def test_放行后不继承空转计数(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """回归：放行 = 一次新的干预，空转计数不跨放行继承。

        此前预算连 ``stagnant_rounds`` 一起续跑，于是「失败一轮 → 挂起 → 放行后写入
        又失败」被凑满 ``MAX_STAGNANT_ROUNDS``，明明才失败两次就提前停下。
        """
        target = tmp_path / "exists.txt"
        target.write_text("old", encoding="utf-8")  # 已存在 → 未授权覆盖 → 放行后仍失败
        fake = _FakeLLM(
            [_fail_read(), _file_write(str(target)), {"kind": "final", "answer": "算了"}]
        )
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client, "写一个已存在的文件")

        first = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert first["pending_approval"]["tools"] == ["file_write"]
        # 首见症状的失败**不算空转**（症状变了 = 世界变了 = 进展，见 agent_loop 的注释）；
        # 关键是它没有被推满 2，也没有跨放行继承。
        assert first["loop_state"]["budget"]["stagnant_rounds"] == 0

        body = client.post(
            f"/tasks/{tid}/resume",
            json={"approved_tools": ["file_write"]},
            headers=_LOCAL_HEADERS,
        ).json()

        # 放行后这次写入失败（累计第 2 次），但空转计数已随放行清零 → 不判空转，继续收尾。
        assert body["loop_state"]["stopped_reason"] == "final"
        assert body["status"] == "verifying"


class TestAgenticFallbacks:
    def test_无密钥回退固定工作流(self, client: TestClient) -> None:
        """默认 agentic 但没有可用密钥 → 回退 run_plan（行为与今天一致，不报错）。"""
        from agent_builder.contracts.schemas import Plan, Step

        tid = _plan_agentic(client)
        entry = get_store().get(tid)
        assert entry is not None
        steps = {
            "step-001": Step(
                id="step-001",
                action="diff_preview",
                inputs={"old_content": "a", "new_content": "b"},
            )
        }
        plan = Plan(
            task_id=tid, order=["step-001"], parallel_groups=[["step-001"]], confirmed_by_user=False
        )
        plan.validate_steps(steps)
        entry.steps = steps
        entry.conductor.task_state.plan = plan

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["execution_results"][0]["step_id"] == "step-001"  # 固定工作流编号
        assert body["loop_state"] is None  # 没走循环

    def test_决策非法时回退静态映射(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeLLM(
            [
                {"kind": "agent", "role": "nobody", "action": "file_list", "inputs": {}},
                {"kind": "final"},
            ]
        )
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client)

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["decided_by"] == "static"
        assert body["loop_state"]["rounds"][0]["decision"]["role"] == "code_worker"

    def test_解析全程失败时不伪装成完成(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型两次都没给出可解析决策 → 停下并保留失败原因，**绝不**判成已完成。"""
        fake = _FakeLLM([{}, {}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client)

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["status"] == "executing"  # 不是 verifying（= 没收尾）
        assert body["execution_results"] == []
        assert body["loop_state"]["stopped_reason"] == "invalid_decision"
        assert body["loop_state"]["rounds"] == []


class TestAgenticFinish:
    """收敛即收尾：模型说 ``final`` 就该进入验证阶段，不因中间有失败而永远卡住。"""

    def test_模型收敛但中间有失败也收尾(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归：此前只在**所有**结果都 done 时才转 verifying —— 于是"模型已收尾、但中途
        有失败步骤"的任务永远停在 executing（顶栏一直「执行中」，没有任何终态）。"""
        fake = _FakeLLM([_fail_read(), {"kind": "final", "thought": "读不到就算了，先收尾"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client, "读一个不存在的目录")

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["stopped_reason"] == "final"
        assert [r["status"] for r in body["execution_results"]] == ["failed"]
        assert body["status"] == "verifying"  # 收尾，而不是卡在 executing
        # 只读动作（哪怕走了角色派发）不产出文件 → 前端不长出「查看产物」
        assert body["execution_results"][0]["artifacts"] == []

    def test_非收敛终止仍留在执行中(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """反向守住：预算耗尽 / 空转等**非收敛**终止不能被当成收尾。"""
        fake = _FakeLLM([_fail_read()] * 12)  # 一直失败 → 触发空转终止
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client, "一直失败")

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["stopped_reason"] == "stagnant"
        assert body["status"] == "executing"  # 交给用户决策，不假装完成
        # 卡 3：steps 状态必须与执行结果对齐（此前跑完还全是 pending，两边对不上）。
        assert {s["status"] for s in body["steps"].values()} == {"failed"}
        # 卡 5：列表也要能看出"非收敛停下"，否则左栏写「执行中」、详情写「已停下」。
        rows = {row["task_id"]: row for row in client.get("/task-summaries").json()}
        assert rows[tid]["stopped_reason"] == "stagnant"


class TestAgenticRequiresModel:
    """agentic 没有可用模型时**不能静默空转**。

    后端重启会清空内存里的密钥；此时 ``/run`` 会落到固定工作流分支，而 agentic 的计划
    是空的（没有预先分解的 steps）→ 以前直接 ``return``：任务停在 EXECUTING 且什么都没
    执行，之后每次 ``/run`` 都 409「非法状态转换：executing + plan_accepted」，任务被彻底卡死。
    """

    def test_无密钥时报清楚原因且不推进状态(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeLLM([{"kind": "final", "thought": "无需动作"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client, "随便做点什么")

        # 执行阶段没有可用模型（等价于后端重启把密钥弄丢了）。
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: None)
        resp = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS)

        assert resp.status_code == 409
        assert resp.json()["detail"]["error_name"] == "E_MODEL"
        # 关键：任务**没有**被推进到 executing —— 存好密钥再 /run 就能恢复。
        assert client.get(f"/tasks/{tid}").json()["status"] == "awaiting_confirm"


class TestAgenticNoDag:
    """③ 规划阶段去工作流化：agentic 下不再预先跑 Decomposer 出 DAG。"""

    def _create(self, client: TestClient, requirement: str = "随便做点什么") -> str:
        resp = client.post("/tasks", json={"requirement": requirement}, headers=_LOCAL_HEADERS)
        assert resp.status_code == 201, resp.text
        return resp.json()["task_id"]

    def test_plan不再产出预分解的DAG(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeLLM([{"kind": "final", "thought": "无需动作"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = self._create(client)

        body = client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "mode": "agentic"},
            headers=_LOCAL_HEADERS,
        ).json()

        assert body["mode"] == "agentic"  # 形态回显，前端据此不再期待 steps
        assert body["steps"] == {}  # 没有预分解的 DAG
        assert body["order"] == []
        assert body["pending_questions"] == []
        assert body["status"] == "awaiting_confirm"
        # 「不先跑分解」要能被证伪：一次模型调用都没花在分解上。
        assert fake.prompts == []

    def test_run可直接从planning直跑(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不先 /plan 也能直跑：/run 内部补一次 plan_ready（状态机契约不变）。"""
        fake = _FakeLLM([_diff_preview(), {"kind": "final", "thought": "做完了"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = self._create(client, "跑一步")

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["status"] == "verifying"
        assert body["execution_results"][0]["step_id"] == "loop-001"
        assert body["conclusion"] == "做完了"

    def test_无密钥时仍走固定工作流分解(self, client: TestClient) -> None:
        """无密钥 → agentic 会回退固定工作流，这时**必须**保留预分解（否则没步骤可跑）。"""
        tid = self._create(client)
        body = client.post(
            f"/tasks/{tid}/plan", json={"use_llm": False, "mode": "agentic"}, headers=_LOCAL_HEADERS
        ).json()
        # 无密钥下分解器给出的是「待确认问题」而不是步骤（既有行为，未被本轮改动）。
        assert body["pending_questions"]


class TestConclusion:
    """① 结论区：收敛那轮的 ``answer`` 落成 final_answer → 响应与回看都带 conclusion。

    模型没给 ``answer`` 时回退 ``thought``（决策理由），宁可给理由也不编造结论。
    """

    def test_收尾用answer当结论而不是内心独白(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归：结论区曾把模型决策理由（"已经够了，可以收尾"）端给用户。"""
        fake = _FakeLLM(
            [
                _diff_preview(),
                {
                    "thought": "已经够了，可以收尾",
                    "next": {"kind": "final", "answer": "本次只预览了差异，未改动任何文件"},
                },
            ]
        )
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client)

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["final_answer"] == "本次只预览了差异，未改动任何文件"
        assert body["conclusion"] == "本次只预览了差异，未改动任何文件"
        assert "可以收尾" not in body["conclusion"]

    def test_收敛后带出结论(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeLLM([_diff_preview(), {"kind": "final", "thought": "已预览完差异，无需改动"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client)

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["final_answer"] == "已预览完差异，无需改动"
        assert body["conclusion"] == "已预览完差异，无需改动"
        # 回看走的是同一份数据 → 刷新后结论不丢。
        detail = client.get(f"/tasks/{tid}", headers=_LOCAL_HEADERS).json()
        assert detail["conclusion"] == "已预览完差异，无需改动"

    def test_挂起时结论只如实说在等放行(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        fake = _FakeLLM([_file_write(str(tmp_path / "out.txt")), {"kind": "final"}])
        monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
        tid = _plan_agentic(client, "写文件")

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()

        assert body["loop_state"]["final_answer"] == ""
        # 没收敛 → 只能如实说"在等你放行"，绝不写成"已完成"。
        assert body["conclusion"].startswith("执行暂停")
        assert "等待你放行" in body["conclusion"]
