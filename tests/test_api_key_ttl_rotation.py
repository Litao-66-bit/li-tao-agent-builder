"""密钥 TTL 与轮换测试。

覆盖三层：
- 存储层（ApiKeyStore + 注入时钟）：到期即失效并清除明文、轮换语义与计数；
- 接口层（/settings/api-key 与 /settings/api-key/rotate）：TTL 校验、轮换约束、失败不改动现状；
- 贯通层：过期后 LLM 客户端工厂不再拿得到密钥；前端控件与后端字段对齐。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_builder.api import deps as deps_module
from agent_builder.api import routes as routes_module
from agent_builder.api import secrets as secrets_module
from agent_builder.api.app import create_app
from agent_builder.api.deps import reset_store
from agent_builder.api.secrets import (
    MAX_TTL_S,
    ApiKeyStore,
    InvalidApiKeyError,
    InvalidTtlError,
    NoRotatableKeyError,
    get_api_key_store,
    reset_api_key_store,
    validate_ttl,
)
from agent_builder.llm.config import LLMConfig

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}

TTL_ONE_HOUR = 3600


class _Clock:
    """可控时钟：注入 ApiKeyStore 以便精确测试过期边界。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _unique_key() -> str:
    return f"sk-test-{uuid.uuid4().hex}"


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


class TestValidateTtl:
    """TTL 校验函数。"""

    def test_不传表示不过期(self) -> None:
        assert validate_ttl(None) is None

    def test_正数接受(self) -> None:
        assert validate_ttl(60) == 60.0
        assert validate_ttl(MAX_TTL_S) == float(MAX_TTL_S)

    @pytest.mark.parametrize("ttl", [0, -1, -0.5])
    def test_非正数被拒(self, ttl: float) -> None:
        with pytest.raises(InvalidTtlError):
            validate_ttl(ttl)

    def test_超上限被拒(self) -> None:
        with pytest.raises(InvalidTtlError):
            validate_ttl(MAX_TTL_S + 1)


class TestTtlStore:
    """存储层 TTL 语义。"""

    def test_设置ttl后立即生效(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        store.set_key(_unique_key(), ttl_s=60)
        assert store.expires_at == clock.now + 60
        assert store.remaining_s == 60
        assert store.is_configured is True
        assert store.is_expired is False

    def test_到期前一秒仍可用(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        store.set_key(_unique_key(), ttl_s=60)
        clock.advance(59)
        assert store.remaining_s == 1
        assert store.is_configured is True
        assert store.get() is not None

    def test_到点即失效(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        store.set_key(_unique_key(), ttl_s=60)
        clock.advance(60)
        assert store.is_expired is True
        assert store.is_configured is False
        assert store.get() is None

    def test_到期清除明文但保留派生值(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        key = _unique_key()
        store.set_key(key, ttl_s=10)
        fingerprint = store.fingerprint()
        clock.advance(10)

        # 触发到期清理
        assert store.get() is None
        # 明文已从内存移除（白盒断言：这正是 TTL 的安全意义）
        assert store._api_key is None
        # 派生值保留，便于界面提示「哪把钥匙过期了」
        assert store.masked_hint() == f"{key[:3]}***"
        assert store.fingerprint() == fingerprint
        assert key not in str(store.masked_hint())

    def test_无ttl时永不过期(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        store.set_key(_unique_key())
        clock.advance(365 * 24 * 3600)
        assert store.expires_at is None
        assert store.remaining_s is None
        assert store.is_configured is True

    def test_过期后再设置可恢复(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        store.set_key(_unique_key(), ttl_s=5)
        clock.advance(5)
        assert store.is_configured is False
        store.set_key(_unique_key(), ttl_s=5)
        assert store.is_configured is True
        assert store.is_expired is False

    def test_clear重置过期与轮换元数据(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        store.set_key(_unique_key(), ttl_s=60)
        store.set_key(_unique_key())
        store.rotate(_unique_key())
        assert store.rotation_count == 1
        store.clear()
        assert store.expires_at is None
        assert store.rotation_count == 0
        assert store.rotated_at is None
        assert store.fingerprint() is None


class TestRotationStore:
    """存储层轮换语义。"""

    def test_无密钥时不能轮换(self) -> None:
        store = ApiKeyStore()
        with pytest.raises(NoRotatableKeyError):
            store.rotate(_unique_key())

    def test_轮换返回旧指纹并累计次数(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        old_key = _unique_key()
        store.set_key(old_key)
        old_fingerprint = store.fingerprint()

        new_key = _unique_key()
        previous = store.rotate(new_key)

        assert previous == old_fingerprint
        assert store.fingerprint() != old_fingerprint
        assert store.get() == new_key
        assert store.rotation_count == 1
        assert store.rotated_at == clock.now

    def test_连续轮换累计计数(self) -> None:
        store = ApiKeyStore()
        store.set_key(_unique_key())
        store.rotate(_unique_key())
        store.rotate(_unique_key())
        assert store.rotation_count == 2

    def test_过期后不能轮换(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        store.set_key(_unique_key(), ttl_s=5)
        clock.advance(5)
        with pytest.raises(NoRotatableKeyError):
            store.rotate(_unique_key())

    def test_新密钥非法时旧密钥不受影响(self) -> None:
        store = ApiKeyStore()
        old_key = _unique_key()
        store.set_key(old_key)
        with pytest.raises(InvalidApiKeyError):
            store.rotate("short")
        assert store.get() == old_key
        assert store.rotation_count == 0

    def test_轮换可重置ttl(self) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        store.set_key(_unique_key(), ttl_s=10)
        store.rotate(_unique_key(), ttl_s=100)
        assert store.remaining_s == 100


class TestTtlEndpoint:
    """接口层 TTL。"""

    def test_保存时带ttl(self, client: TestClient) -> None:
        resp = client.post(
            "/settings/api-key",
            json={"api_key": _unique_key(), "ttl_s": TTL_ONE_HOUR},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["expired"] is False
        assert body["expires_at"] is not None
        assert body["expires_at"].endswith("Z")
        assert body["remaining_s"] == TTL_ONE_HOUR

    def test_不传ttl则不过期(self, client: TestClient) -> None:
        resp = client.post(
            "/settings/api-key", json={"api_key": _unique_key()}, headers=LOCAL_HEADERS
        )
        body = resp.json()
        assert body["expires_at"] is None
        assert body["remaining_s"] is None
        assert body["expired"] is False

    @pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_S + 1])
    def test_非法ttl返回422(self, client: TestClient, ttl: int) -> None:
        resp = client.post(
            "/settings/api-key",
            json={"api_key": _unique_key(), "ttl_s": ttl},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 422

    def test_查询响应带过期信息(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(secrets_module, "_store", ApiKeyStore(clock=clock))
        client.post(
            "/settings/api-key",
            json={"api_key": _unique_key(), "ttl_s": 100},
            headers=LOCAL_HEADERS,
        )
        clock.advance(30)
        body = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()
        assert body["configured"] is True
        assert body["remaining_s"] == 70
        assert body["expired"] is False

    def test_过期后状态与可用性(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        monkeypatch.setattr(secrets_module, "_store", store)
        client.post(
            "/settings/api-key",
            json={"api_key": _unique_key(), "ttl_s": 10},
            headers=LOCAL_HEADERS,
        )
        clock.advance(10)

        body = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()
        assert body["configured"] is False
        assert body["expired"] is True
        assert body["expires_at"] is not None
        assert body["masked"] is not None  # 仍能告知是哪把钥匙过期了
        assert get_api_key_store().get() is None

    def test_过期后LLM工厂拿不到密钥(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = _Clock()
        store = ApiKeyStore(clock=clock)
        monkeypatch.setattr(secrets_module, "_store", store)

        captured: dict[str, object] = {}

        class _FakeClient:
            """镜像真实客户端：无密钥即不可用。"""

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
                self.is_available = bool(getattr(config, "api_key", ""))

        # 环境变量侧无密钥，密钥只能来自运行时存储
        monkeypatch.setattr(
            deps_module.LLMConfig, "from_env", classmethod(lambda cls: LLMConfig(api_key=""))
        )
        monkeypatch.setattr(deps_module, "LLMClient", _FakeClient)

        client.post(
            "/settings/api-key",
            json={"api_key": _unique_key(), "ttl_s": 10},
            headers=LOCAL_HEADERS,
        )
        assert deps_module.get_llm_client() is not None  # 未过期：拿得到

        clock.advance(10)
        assert deps_module.get_llm_client() is None  # 已过期：拿不到


class TestRotateEndpoint:
    """接口层轮换。"""

    def test_无密钥时轮换返回400(self, client: TestClient) -> None:
        resp = client.post(
            "/settings/api-key/rotate", json={"api_key": _unique_key()}, headers=LOCAL_HEADERS
        )
        assert resp.status_code == 400
        assert "可轮换" in resp.json()["detail"]

    def test_轮换成功回带旧指纹与新计数(self, client: TestClient) -> None:
        old_key = _unique_key()
        first = client.post(
            "/settings/api-key", json={"api_key": old_key}, headers=LOCAL_HEADERS
        ).json()
        resp = client.post(
            "/settings/api-key/rotate", json={"api_key": _unique_key()}, headers=LOCAL_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["rotated_from_fingerprint"] == first["fingerprint"]
        assert body["fingerprint"] != first["fingerprint"]
        assert body["configured"] is True
        assert body["rotation_count"] == 1
        assert body["rotated_at"] is not None

    def test_轮换后旧密钥被真正替换(self, client: TestClient) -> None:
        old_key = _unique_key()
        client.post("/settings/api-key", json={"api_key": old_key}, headers=LOCAL_HEADERS)
        new_key = _unique_key()
        client.post("/settings/api-key/rotate", json={"api_key": new_key}, headers=LOCAL_HEADERS)
        store = get_api_key_store()
        assert store.get() == new_key
        assert store.get() != old_key

    def test_轮换不改动历史状态(self, client: TestClient) -> None:
        # 轮换响应不应把 rotated_from_fingerprint 泄漏到后续查询
        client.post("/settings/api-key", json={"api_key": _unique_key()}, headers=LOCAL_HEADERS)
        client.post(
            "/settings/api-key/rotate", json={"api_key": _unique_key()}, headers=LOCAL_HEADERS
        )
        body = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()
        assert body["rotated_from_fingerprint"] is None
        assert body["rotation_count"] == 1

    def test_探针失败时不替换现有密钥(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_key = _unique_key()
        client.post("/settings/api-key", json={"api_key": old_key}, headers=LOCAL_HEADERS)
        old_fingerprint = get_api_key_store().fingerprint()
        monkeypatch.setattr(
            routes_module, "probe_api_key", lambda *_a, **_k: (False, "密钥被拒绝（HTTP 401）")
        )
        resp = client.post(
            "/settings/api-key/rotate",
            params={"verify": "true"},
            json={"api_key": _unique_key()},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 400
        assert get_api_key_store().get() == old_key
        assert get_api_key_store().fingerprint() == old_fingerprint
        assert get_api_key_store().rotation_count == 0

    def test_新密钥非法时不替换现有密钥(self, client: TestClient) -> None:
        old_key = _unique_key()
        client.post("/settings/api-key", json={"api_key": old_key}, headers=LOCAL_HEADERS)
        resp = client.post(
            "/settings/api-key/rotate",
            json={"api_key": "short", "ttl_s": -1},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code in (400, 422)
        assert get_api_key_store().get() == old_key
        assert get_api_key_store().rotation_count == 0

    def test_轮换可更新ttl(self, client: TestClient) -> None:
        client.post("/settings/api-key", json={"api_key": _unique_key()}, headers=LOCAL_HEADERS)
        body = client.post(
            "/settings/api-key/rotate",
            json={"api_key": _unique_key(), "ttl_s": 600},
            headers=LOCAL_HEADERS,
        ).json()
        assert body["remaining_s"] == 600
        assert body["expires_at"] is not None

    def test_轮换后过期仍生效(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = _Clock()
        monkeypatch.setattr(secrets_module, "_store", ApiKeyStore(clock=clock))
        client.post(
            "/settings/api-key",
            json={"api_key": _unique_key(), "ttl_s": 100},
            headers=LOCAL_HEADERS,
        )
        client.post(
            "/settings/api-key/rotate",
            json={"api_key": _unique_key(), "ttl_s": 20},
            headers=LOCAL_HEADERS,
        )
        clock.advance(20)
        body = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()
        assert body["configured"] is False
        assert body["expired"] is True

    def test_轮换需要本机标识头(self, client: TestClient) -> None:
        resp = client.post("/settings/api-key/rotate", json={"api_key": _unique_key()})
        assert resp.status_code == 403


class TestFrontendWiring:
    """前端控件与后端字段对齐（静态检查）。"""

    def _read(self, relative: str) -> str:
        return (_PROJECT_ROOT / relative).read_text(encoding="utf-8")

    def test_api层暴露轮换与ttl(self) -> None:
        source = self._read("frontend/js/api.js")
        assert "rotateApiKey" in source
        assert "ttl_s: ttlS" in source
        assert "/settings/api-key/rotate" in source
        # 保存与轮换共用 keyBody，TTL 只按需带上（不传=不过期）。
        assert "function keyBody(apiKey, ttlS)" in source
        assert "setApiKey: (apiKey, ttlS)" in source

    def test_页面持有轮换与到期控件(self) -> None:
        html = self._read("frontend/index.html")
        for element_id in ("apiKeyRotate", "apiKeyExpiry", "apiKeyStateText"):
            assert f'id="{element_id}"' in html, element_id

    def test_app实现轮换流程与到期显示(self) -> None:
        source = self._read("frontend/js/app.js")
        assert "function startApiKeyRotate()" in source
        assert "api.rotateApiKey(" in source
        assert "keyRotateMode" in source
        assert "classList.toggle('danger'" in source
        # 状态栏同步密钥状态（新 IA 的 statusbar）。
        assert "function updateStatusKey(status)" in source

    def test_app无顶层重复声明(self) -> None:
        # 顶层 const/let 重名会让整个脚本语法报错（曾踩过：tempValue 重复声明）
        source = self._read("frontend/js/app.js")
        declared = re.findall(r"^(?:const|let|var) (\w+)\s*=", source, re.MULTILINE)
        duplicates = sorted({name for name in declared if declared.count(name) > 1})
        assert not duplicates, f"app.js 顶层重复声明: {duplicates}"
