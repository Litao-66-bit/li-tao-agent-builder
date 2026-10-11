"""卡 45：`GET /capabilities` 真实能力清单 + 任务级能力开关**真生效**。

守住的契约（对应卡 45 的三条验收标准）：

1. 面板里的每一项都能在 `/capabilities` 里找到来源 —— 不存在"没有出处的开关"；
2. 关掉"联网搜索"后，agentic 任务的**能力目录**里不出现 `web_search` / `web_fetch`，
   且**执行时**调用它们会被门卫以"未列入角色白名单"拒绝（不是"看得见、调了才失败"）；
3. 插件区不再有写死的假条目（后端恒回空数组 + 说明）。

另有一条**安全**断言：该端点会枚举本机可用工具与角色，必须挂本机标识头。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.role_catalog import build_catalog
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.contracts.schemas import Plan, Step
from agent_builder.tools.permissions import (
    CAPABILITY_GROUPS,
    DEFAULT_CAPABILITIES,
    filter_role_perms,
    get_default_role_perms,
    tools_of_capabilities,
)
from agent_builder.tools.registry import registry

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


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


def _catalog_actions(specs) -> set[str]:
    return {action for spec in specs for action in spec.accepts}


class TestCapabilitiesEndpoint:
    def test_无本机标识头被阻断(self, client: TestClient) -> None:
        """内部信息（枚举本机工具/角色）→ 与 /projects、/workspace/* 同档保护。"""
        assert client.get("/capabilities").status_code == 403

    def test_带标识头返回真实清单(self, client: TestClient) -> None:
        resp = client.get("/capabilities", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["workspace"]
        assert sorted(body["default_capabilities"]) == sorted(DEFAULT_CAPABILITIES)
        groups = {item["group"]: item for item in body["capability_groups"]}
        assert set(groups) == set(CAPABILITY_GROUPS)
        for name, item in groups.items():
            assert item["label"], name
            assert item["note"], name
            assert sorted(item["tools"]) == sorted(CAPABILITY_GROUPS[name]), name

    def test_清单里每个工具都真实注册(self, client: TestClient) -> None:
        """不许出现"面板上有、其实不存在"的工具（验收 1 的机器化版本）。"""
        body = client.get("/capabilities", headers=_LOCAL_HEADERS).json()
        registered = set(registry.list_tools())
        used: set[str] = set()
        for item in body["capability_groups"]:
            used |= set(item["tools"])
        assert used <= registered, f"未注册的工具: {sorted(used - registered)}"

    def test_每个角色白名单里的工具也都注册(self, client: TestClient) -> None:
        body = client.get("/capabilities", headers=_LOCAL_HEADERS).json()
        registered = set(registry.list_tools())
        for role, tools in body["roles"].items():
            assert set(tools) <= registered, f"{role} 引用了未注册工具"

    def test_高危工具只在删除提交回滚上(self, client: TestClient) -> None:
        """卡 44 的口径也要能被界面查证：写文件不在高危里。"""
        body = client.get("/capabilities", headers=_LOCAL_HEADERS).json()
        assert sorted(body["high_risk_tools"]) == ["file_delete", "git_commit", "rollback"]

    def test_能力组都给出了高危项与实授角色(self, client: TestClient) -> None:
        body = client.get("/capabilities", headers=_LOCAL_HEADERS).json()
        groups = {item["group"]: item for item in body["capability_groups"]}
        assert groups["git"]["high_risk_tools"] == ["git_commit", "rollback"]
        assert groups["network"]["high_risk_tools"] == []
        assert groups["network"]["roles"], "联网组必须给出实授角色，否则界面说不清关掉了什么"

    def test_插件区如实为空(self, client: TestClient) -> None:
        """验收 3：不再有写死的假插件条目。"""
        body = client.get("/capabilities", headers=_LOCAL_HEADERS).json()
        assert body["plugins"] == []
        assert "插件系统" in body["plugins_note"]


class TestCapabilityGroupMapping:
    def test_关闭一组只剔除该组工具(self) -> None:
        all_tools = set(registry.list_tools())
        kept = tools_of_capabilities({"file"}, all_tools=all_tools)
        assert "web_search" not in kept
        assert "sandbox_run" not in kept
        assert "file_write" in kept
        # 基础设施不受开关管辖（关掉联网不该顺带打断审计/记忆记账）。
        assert "audit_log" in kept
        assert "memory_write" in kept

    def test_空集合等于只留基础设施(self) -> None:
        kept = tools_of_capabilities(set(), all_tools=set(registry.list_tools()))
        assert "file_read" not in kept
        assert "web_search" not in kept
        assert "audit_log" in kept

    def test_none等于全部启用(self) -> None:
        all_tools = set(registry.list_tools())
        assert tools_of_capabilities(None, all_tools=all_tools) == all_tools

    def test_角色白名单与高危项同步收窄(self) -> None:
        narrowed = filter_role_perms(get_default_role_perms(), {"file"})
        operator = narrowed["operator"]
        assert "web_search" not in operator.allowed_tools
        assert "sandbox_run" not in operator.allowed_tools
        assert "file_write" in operator.allowed_tools
        # 高危项不得指向已被关掉的工具（RolePerm 只保证 high_risk ⊆ allowed）。
        for perm in narrowed.values():
            assert set(perm.high_risk_tools) <= set(perm.allowed_tools), perm.role

    def test_关闭联网后目录里不再出现联网动作(self) -> None:
        """验收 2 的第一半：模型**看不见**被关掉的动作（不会"调了才失败"）。"""
        without_net = build_catalog(sub_arch=False, capabilities={"file", "shell", "git"})
        actions = _catalog_actions(without_net)
        assert "web_search" not in actions
        assert "web_fetch" not in actions
        # 只做联网的 searcher 没有别的动作可做 → 整个角色不进目录。
        assert "searcher" not in {spec.name for spec in without_net}

    def test_默认口径与既有行为一致(self) -> None:
        assert build_catalog(sub_arch=False) == build_catalog(
            sub_arch=False, capabilities=None
        )
        full = build_catalog(sub_arch=False, capabilities=DEFAULT_CAPABILITIES)
        assert _catalog_actions(full) == _catalog_actions(build_catalog(sub_arch=False))


class TestCapabilityToggleTakesEffect:
    """真实执行链路：关掉"命令执行"后，``sandbox_run`` 步骤**必须**被门卫拒绝。"""

    def _task_with_step(self, client: TestClient, tmp_path, caps) -> str:
        tid = client.post(
            "/tasks", json={"requirement": "跑一条命令"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        plan_body = {"use_llm": False}
        if caps is not None:
            plan_body["capabilities"] = caps
        client.post(f"/tasks/{tid}/plan", json=plan_body, headers=_LOCAL_HEADERS)
        entry = get_store().get(tid)
        assert entry is not None
        steps = {
            "step-001": Step(
                id="step-001",
                action="sandbox_run",
                inputs={"command": "echo hi", "path": str(tmp_path)},
            )
        }
        plan = Plan(
            task_id=tid,
            order=["step-001"],
            parallel_groups=[["step-001"]],
            confirmed_by_user=False,
        )
        plan.validate_steps(steps)
        entry.steps = steps
        entry.conductor.task_state.plan = plan
        return tid

    def test_关闭命令执行后sandbox_run被拒(self, client: TestClient, tmp_path) -> None:
        tid = self._task_with_step(client, tmp_path, ["file", "network", "git"])
        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        row = body["execution_results"][0]
        assert row["status"] == "failed"
        # 明确说"不在白名单"（而不是含糊的"失败"）—— 用户才知道是开关导致的。
        assert "白名单" in row["error"]
        assert row["retries"] == 0, "确定性失败不该重试"

    def test_不传能力开关时命令执行照旧可用(self, client: TestClient, tmp_path) -> None:
        """既有行为不变：不传 capabilities = 全部启用。"""
        tid = self._task_with_step(client, tmp_path, None)
        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert body["execution_results"][0]["status"] == "done"

    def test_显式全开会显式启用(self, client: TestClient, tmp_path) -> None:
        tid = self._task_with_step(client, tmp_path, sorted(DEFAULT_CAPABILITIES))
        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert body["execution_results"][0]["status"] == "done"

    def test_关闭文件能力后写文件被拒(self, client: TestClient, tmp_path) -> None:
        """能力开关对**写盘**同样生效（这是用户最可能去关的那一组）。"""
        tid = client.post(
            "/tasks", json={"requirement": "写文件"}, headers=_LOCAL_HEADERS
        ).json()["task_id"]
        client.post(
            f"/tasks/{tid}/plan",
            json={"use_llm": False, "capabilities": ["network", "shell", "git"]},
            headers=_LOCAL_HEADERS,
        )
        entry = get_store().get(tid)
        assert entry is not None
        steps = {
            "step-001": Step(
                id="step-001",
                action="file_write",
                inputs={"path": str(tmp_path / "x.txt"), "content": "hi", "overwrite": True},
            )
        }
        plan = Plan(
            task_id=tid,
            order=["step-001"],
            parallel_groups=[["step-001"]],
            confirmed_by_user=False,
        )
        plan.validate_steps(steps)
        entry.steps = steps
        entry.conductor.task_state.plan = plan

        body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
        assert body["execution_results"][0]["status"] == "failed"
        assert not (tmp_path / "x.txt").exists()
