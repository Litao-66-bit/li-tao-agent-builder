"""卡 47：密钥不该因后端重启而静默失效 —— 以及**落盘是显式可选项**。

三条必须同时成立的约束：

1. **默认不落盘**：不开开关时行为与今天完全一致（``secrets.py`` 的既有安全约束，
   对抗性用例在 ``test_api_key_security.py::TestNotPersisted`` 里守着）；
2. **显式开启后重启可用**：``/settings/api-key/persist``（或环境变量）打开后，
   密钥写入 0600 文件，**新进程**（单例重建 = 重启）能自动读回；
3. **绝不泄露**：响应/日志/文件回显路径都不得出现明文 —— 文件里有明文是落盘的必然代价，
   但"能读取明文的接口"一个都不许有。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import reset_project_store, reset_store
from agent_builder.api.routes import load_api_key_on_startup
from agent_builder.api.secret_store import (
    PERSIST_ENV,
    load_secret,
    persist_enabled,
    save_secret,
    secret_path,
)
from agent_builder.api.secrets import get_api_key_store, reset_api_key_store

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


@pytest.fixture(autouse=True)
def _isolate_secret_dir(tmp_path, monkeypatch):
    """密钥落点隔离到临时目录 + 默认关闭落盘（与生产默认一致）。"""
    monkeypatch.setenv("AGENT_BUILDER_SECRET_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv(PERSIST_ENV, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    reset_api_key_store()
    reset_store()
    reset_project_store()
    yield
    reset_api_key_store()
    reset_store()
    reset_project_store()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _set_key(client: TestClient, key: str) -> dict:
    resp = client.post("/settings/api-key", json={"api_key": key}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _restart() -> None:
    """模拟进程重启：只丢内存单例，磁盘与工作区都留着。"""
    reset_api_key_store()


def _restart_without_context_manager(client: TestClient) -> dict:
    """模拟"新进程启动"并返回密钥状态。

    ⚠️ ``TestClient`` 只在 ``with`` 块（或直接构造第一例）里跑 startup 事件 ——
    所以这里**显式调用** ``load_api_key_on_startup()``，
    它正是 ``create_app`` 的 startup 钩子干的那件事。这样测的是**装载逻辑本身**，
    不依赖 TestClient 的生命周期细节。
    """
    reset_api_key_store()
    source = load_api_key_on_startup()
    status = client.get("/settings/api-key", headers=_LOCAL_HEADERS).json()
    status["_loaded_source"] = source
    return status


class TestDefaultIsMemoryOnly:
    def test_默认不落盘(self, client: TestClient) -> None:
        key = "sk-" + "a" * 24
        body = _set_key(client, key)
        assert body["persisted"] is False
        assert body["source"] == "runtime"
        assert not secret_path().exists(), "默认不许落盘（既有安全约束）"

    def test_默认下重启即失效(self, client: TestClient) -> None:
        _set_key(client, "sk-" + "b" * 24)
        _restart()
        # 新进程：没有环境变量、没有文件 → 如实报 none
        fresh = TestClient(create_app())
        status = fresh.get("/settings/api-key", headers=_LOCAL_HEADERS).json()
        assert status["configured"] is False
        assert status["source"] == "none"

    def test_响应里没有明文字段(self, client: TestClient) -> None:
        key = "sk-" + "c" * 24
        body = _set_key(client, key)
        assert key not in json.dumps(body)


class TestExplicitPersistence:
    def test_开启后写文件且权限收紧(self, client: TestClient) -> None:
        key = "sk-" + "d" * 24
        _set_key(client, key)
        resp = client.post(
            "/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["persisted"] is True

        path = secret_path()
        assert path.exists()
        stored = load_secret()
        assert stored is not None and stored.api_key == key
        if os.name != "nt":
            mode = path.stat().st_mode & 0o777
            assert mode == 0o600, f"权限必须是 0600，实际 {oct(mode)}"

    def test_开启后重启仍可用(self, client: TestClient) -> None:
        key = "sk-" + "e" * 24
        _set_key(client, key)
        client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)

        status = _restart_without_context_manager(client)
        assert status["configured"] is True, "显式落盘后重启必须仍可用（卡 47 的核心）"
        assert status["source"] == "file"
        assert status["persisted"] is True
        assert get_api_key_store().get() == key

    def test_环境变量优先于文件(self, client: TestClient, monkeypatch) -> None:
        _set_key(client, "sk-" + "f" * 24)
        client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)
        env_key = "sk-" + "g" * 24
        monkeypatch.setenv("DEEPSEEK_API_KEY", env_key)

        status = _restart_without_context_manager(client)
        assert status["source"] == "env"
        assert get_api_key_store().get() == env_key, "环境变量应当覆盖文件"

    def test_环境变量也可单独生效(self, monkeypatch) -> None:
        env_key = "sk-" + "h" * 24
        monkeypatch.setenv("DEEPSEEK_API_KEY", env_key)
        reset_api_key_store()
        assert load_api_key_on_startup() == "env"
        assert get_api_key_store().get() == env_key

    def test_关闭落盘会删掉文件(self, client: TestClient) -> None:
        _set_key(client, "sk-" + "i" * 24)
        client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)
        assert secret_path().exists()

        resp = client.post(
            "/settings/api-key/persist?persist=false", headers=_LOCAL_HEADERS
        )
        assert resp.status_code == 200
        assert not secret_path().exists()
        assert resp.json()["persisted"] is False
        # 内存里的密钥保留：本进程内继续可用（关的是"记住"，不是"当前钥匙"）
        assert resp.json()["configured"] is True

    def test_没有密钥时不许落盘(self, client: TestClient) -> None:
        resp = client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)
        assert resp.status_code == 400
        assert not secret_path().exists(), "不许写一个空文件出来"

    def test_删除密钥会连落盘副本一起删(self, client: TestClient) -> None:
        _set_key(client, "sk-" + "j" * 24)
        client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)
        assert secret_path().exists()

        resp = client.delete("/settings/api-key", headers=_LOCAL_HEADERS)
        assert resp.status_code == 200
        assert not secret_path().exists()
        # 重启后不应"复活"
        _restart()
        fresh = TestClient(create_app())
        assert fresh.get("/settings/api-key", headers=_LOCAL_HEADERS).json()["configured"] is False

    def test_环境变量开关也能打开落盘(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setenv(PERSIST_ENV, "1")
        assert persist_enabled() is True
        _set_key(client, "sk-" + "k" * 24)
        assert secret_path().exists(), "开关打开时保存密钥就该顺带落盘"

    def test_落盘文件读坏不影响启动(self, client: TestClient) -> None:
        path = secret_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{不是 JSON", encoding="utf-8")
        fresh = TestClient(create_app())
        status = fresh.get("/settings/api-key", headers=_LOCAL_HEADERS).json()
        assert status["configured"] is False
        assert status["source"] == "none"


class TestNoPlaintextLeak:
    def test_落盘日志不含明文(self, caplog: pytest.LogCaptureFixture, tmp_path) -> None:
        key = "sk-" + "l" * 24
        with caplog.at_level(logging.INFO, logger="agent_builder.api.secret_store"):
            assert save_secret(key, fingerprint="abcd1234") is True
        assert caplog.records, "至少要有一条落盘日志"
        assert key not in caplog.text
        assert "abcd1234" not in caplog.text  # 指纹也不写（日志只需知道"写到哪了"）

    def test_启动装载日志不含明文(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        key = "sk-" + "m" * 24
        _set_key(client, key)
        client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)
        _restart()

        with caplog.at_level(logging.INFO, logger="agent_builder.api.routes"):
            fresh = TestClient(create_app())
            fresh.get("/health")
        assert key not in caplog.text

    def test_状态响应字段全集仍是非敏感集(self, client: TestClient) -> None:
        from agent_builder.api.schemas import API_KEY_STATUS_FIELDS

        body = _set_key(client, "sk-" + "n" * 24)
        assert set(body) == set(API_KEY_STATUS_FIELDS)

    def test_密钥文件不含指纹以外的派生信息(self, client: TestClient) -> None:
        key = "sk-" + "o" * 24
        _set_key(client, key)
        client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)
        payload = json.loads(secret_path().read_text(encoding="utf-8"))
        assert set(payload) <= {"api_key", "fingerprint"}


class TestAtomicWrite:
    def test_不留临时文件(self, client: TestClient, tmp_path) -> None:
        _set_key(client, "sk-" + "p" * 24)
        client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)
        leftovers = list(Path(secret_path().parent).glob("*.tmp"))
        assert leftovers == [], f"原子写不该留下临时文件：{leftovers}"

    def test_重复落盘只留一个文件(self, client: TestClient) -> None:
        for index in range(3):
            _set_key(client, "sk-" + str(index) * 24)
            client.post("/settings/api-key/persist?persist=true", headers=_LOCAL_HEADERS)
        files = list(Path(secret_path().parent).glob("*.json"))
        assert len(files) == 1
        assert load_secret().api_key == "sk-" + "2" * 24
