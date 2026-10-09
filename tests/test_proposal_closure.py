"""副结构提议闭环：提议浮出 → 批准（生成脚手架）/ 拒绝 → 回看。

覆盖三件事：
1. **不再丢弃**：模型 ``kind=propose`` 后任务挂起并带出完整提议（原先该字段被直接丢掉）；
2. **批准只生成、不越权**：脚手架写进工作区 ``proposals/<id>/``，**不改仓库既有文件**，
   且生成物必须是不出工作区、语法合法的 Python；
3. **决定可回看**：批准 / 拒绝都落进提议历史，状态与任务状态一致。

全部注入假 LLM 客户端，不碰网络、不碰真实工具。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import routes as routes_module
from agent_builder.api.app import create_app
from agent_builder.api.deciders import ProposalDraft
from agent_builder.api.deps import reset_project_store, reset_store
from agent_builder.api.proposal_scaffold import (
    SCAFFOLD_ROOT,
    pascal_case,
    plan_scaffold,
    write_scaffold,
)
from agent_builder.api.secrets import reset_api_key_store

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


class _FakeLLM:
    """假 LLM 客户端：依次吐出脚本里的决策 payload。"""

    def __init__(self, decisions: list[Any]) -> None:
        self.decisions = list(decisions)

    def complete_json(self, prompt: str, schema_hint: str = "") -> Any:
        return self.decisions.pop(0) if self.decisions else {"kind": "final"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离客户端：工作区指向 tmp_path（脚手架就写在这里），存储与密钥全部重置。"""
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
    # 让 pytest 不再把刚生成到工作区的文件当测试收集（projects.json 之类）
    for item in Path(tmp_path).rglob("test_*.py"):
        item.unlink()


def _propose_payload(name: str = "data_cleaner") -> dict[str, Any]:
    return {
        "thought": "现有角色都不会清洗数据",
        "next": {
            "kind": "propose",
            "proposal": {
                "target": "role",
                "name": name,
                "mission": "清洗原始数据并输出质量报告",
                "accepts": ["file_read"],
                "risk": "low",
                "rationale": "data_analyst 只做只读查询，不会清洗",
            },
        },
    }


def _run_with_proposal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> dict[str, Any]:
    """建任务 → /plan(agentic + 副结构开启) → /run（模型提议）→ 返回 /run 响应体。"""
    fake = _FakeLLM([payload])
    monkeypatch.setattr(routes_module, "get_llm_client", lambda **k: fake)
    resp = client.post("/tasks", json={"requirement": "清洗一份数据"}, headers=_LOCAL_HEADERS)
    tid = resp.json()["task_id"]
    client.post(
        f"/tasks/{tid}/plan",
        json={"use_llm": False, "mode": "agentic", "sub_arch": True},
        headers=_LOCAL_HEADERS,
    )
    body = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()
    return body


class TestProposalSurfaces:
    def test_提议挂起并带出完整草案(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        body = _run_with_proposal(client, monkeypatch, _propose_payload())

        assert body["status"] == "interrupted"
        assert body["loop_state"]["stopped_reason"] == "proposed"
        view = body["pending_proposal"]
        assert view["accepts"] == ["file_read"]
        assert "data_analyst" in view["rationale"]
        record = view["proposal"]
        assert record["status"] == "proposed"
        assert record["target_module"] == "agent_builder/roles/data_cleaner.py"
        assert record["risk"] == "low"
        assert record["version"]  # 模板版本，便于回看
        assert len(record["impacted_files"]) == 5
        assert record["diff_preview"].startswith('"""')
        assert len(body["proposals"]) == 1
        # 提议阶段绝不落盘
        assert not (tmp_path / SCAFFOLD_ROOT).exists()

    def test_提议名字与现役角色重名时不被接受(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重名 = 编造，校验链拦下 → 循环判非法决策，绝不会挂一个坏提议给用户。"""
        body = _run_with_proposal(client, monkeypatch, _propose_payload(name="searcher"))

        assert body["pending_proposal"] is None
        assert body["proposals"] == []
        assert body["loop_state"]["stopped_reason"] == "invalid_decision"


class TestProposalApproval:
    def test_批准写入脚手架且不改仓库既有文件(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        body = _run_with_proposal(client, monkeypatch, _propose_payload())
        task_id = body["task_id"]

        resp = client.post(f"/tasks/{task_id}/proposal/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        after = resp.json()

        assert after["pending_proposal"] is None
        assert after["status"] == "executing"  # 决定完成 → 从挂起拉回
        record = after["proposals"][0]
        assert record["status"] == "approved"
        assert record["approved_by"] == "user"
        written = [Path(p).name for p in record["impacted_files"]]
        assert sorted(written) == [
            "README.md",
            "data_cleaner.py",
            "execution-protocols.addon.md",
            "permissions.addon.py",
            "test_roles_data_cleaner.py",
        ]
        root = tmp_path / SCAFFOLD_ROOT / f"{task_id}-p1"
        assert (root / "agent_builder/roles/data_cleaner.py").exists()
        assert (root / "tools/permissions.addon.py").exists()
        # 生成物必须是合法 Python，否则并入时会直接炸
        for path in root.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"))

    def test_拒绝只记录决定不写文件(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        body = _run_with_proposal(client, monkeypatch, _propose_payload())
        task_id = body["task_id"]

        resp = client.post(f"/tasks/{task_id}/proposal/reject", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200, resp.text
        after = resp.json()

        assert after["pending_proposal"] is None
        assert after["proposals"][0]["status"] == "rejected"
        assert not (tmp_path / SCAFFOLD_ROOT).exists()

    def test_没有待定提议时决定被拒(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resp = client.post("/tasks", json={"requirement": "x"}, headers=_LOCAL_HEADERS)
        task_id = resp.json()["task_id"]
        resp = client.post(f"/tasks/{task_id}/proposal/approve", headers=_LOCAL_HEADERS)
        assert resp.status_code == 409
        assert "没有待决定" in resp.text

    def test_决定后提议不再挂起可回看(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = _run_with_proposal(client, monkeypatch, _propose_payload())
        task_id = body["task_id"]
        client.post(f"/tasks/{task_id}/proposal/reject", headers=_LOCAL_HEADERS)

        again = client.get(f"/tasks/{task_id}", headers=_LOCAL_HEADERS).json()
        assert again["pending_proposal"] is None
        assert len(again["proposals"]) == 1
        assert again["proposals"][0]["approved_by"] == "user"


class TestScaffold:
    def _draft(self) -> ProposalDraft:
        return ProposalDraft(
            target="role",
            name="data_cleaner",
            mission="清洗原始数据并输出质量报告",
            accepts=("file_read", "file_write"),
            risk="mid",
            rationale="现有角色都不做清洗",
        )

    def test_计划生成五个文件(self) -> None:
        files = plan_scaffold(self._draft(), "t-1-p1")
        assert len(files) == 5
        assert all(path.startswith(f"{SCAFFOLD_ROOT}/t-1-p1/") for path in files)

    def test_角色模块是合法Python且不越层(self) -> None:
        module = plan_scaffold(self._draft(), "t-1-p1")[
            f"{SCAFFOLD_ROOT}/t-1-p1/agent_builder/roles/data_cleaner.py"
        ]
        ast.parse(module)
        # roles 层禁止 import api / tools / llm；工具调用一律经注入的 executor_fn
        assert "from agent_builder.tools" not in module
        assert "from agent_builder.api" not in module
        assert "agent_builder.llm" not in module
        assert "ACCEPTS" in module and '"file_write"' in module
        assert pascal_case("data_cleaner") == "DataCleaner"

    def test_写入落在工作区内(self, tmp_path) -> None:
        files = plan_scaffold(self._draft(), "t-1-p1")
        result = write_scaffold(tmp_path, files)
        assert result.error is None
        assert len(result.files) == 5
        assert (tmp_path / SCAFFOLD_ROOT / "t-1-p1/README.md").exists()

    def test_拒绝越界路径(self, tmp_path) -> None:
        """沙箱校验：生成路径不得跳出工作区（与工具门卫同口径）。

        跨平台坑（实测 CI run #67）：``C:/Windows/evil.py`` 在 Windows 是**绝对路径**（越界 ✓），
        但在 POSIX 上只是一个名为 ``C:`` 的**相对目录**（并未越界 ✗）—— 这条断言只在 Windows
        成立，于是 ubuntu-latest 上必然失败，而 Windows 本地一直绿。
        越界样本要按平台给：POSIX 用 ``/`` 开头的绝对路径，Windows 再补盘符样本。
        """
        bad_paths = ["../escape.py", "/etc/passwd"]
        if os.name == "nt":
            bad_paths.append("C:/Windows/evil.py")
        for bad in bad_paths:
            result = write_scaffold(tmp_path, {bad: "x"})
            assert result.files == []
            assert result.error is not None
            assert "越界" in result.error
        assert not (tmp_path.parent / "escape.py").exists()
