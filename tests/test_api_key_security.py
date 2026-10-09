"""密钥安全对抗性用例 —— 每条都按「利用路径 → 被阻断」成对断言。

威胁编号（见 docs/reports/api-key-security-audit.md）：
- T1 恶意网页借 CORS 跨域打本机 8000
- T2 敏感端点零鉴权，任意来源可直接读写
- T3 密钥被回显
- T4 密钥落盘
- T5 日志/审计泄露明文
- T6 密钥注入（HTTP 头 / 日志换行）
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import reset_store
from agent_builder.api.schemas import API_KEY_STATUS_FIELDS, ApiKeyStatusResponse
from agent_builder.api.secrets import (
    FINGERPRINT_LEN,
    MAX_KEY_LEN,
    get_api_key_store,
    reset_api_key_store,
)
from agent_builder.api.security import ALLOWED_ORIGINS, CLIENT_HEADER_NAME, CLIENT_HEADER_VALUE
from agent_builder.tools.redact import redact_args

# 本机客户端标识头：敏感端点必须携带。
LOCAL_HEADERS = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SKIP_DIRS = {".venv", ".git", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules"}


def _unique_key() -> str:
    """生成唯一密钥，避免全盘搜索类用例被其它用例残留误伤。"""
    return f"sk-test-{uuid.uuid4().hex}"


def _search_disk_for(needle: str) -> list[str]:
    """在工作副本内全盘搜索密钥串（跳过缓存与虚拟环境）。"""
    hits: list[str] = []
    for path in _PROJECT_ROOT.rglob("*"):
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            if needle in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path))
        except OSError:
            continue
    return hits


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前后重置密钥单例与任务单例。"""
    reset_api_key_store()
    reset_store()
    yield
    reset_api_key_store()
    reset_store()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


class TestCorsBoundary:
    """T1：恶意网页借 CORS 打本机端口。"""

    def test_利用_恶意页面跨站读密钥状态_阻断(self, client: TestClient) -> None:
        # 利用：evil.example 上的脚本 fetch('http://127.0.0.1:8000/settings/api-key')
        # 阻断：来源不在白名单 → 403，且响应不带任何 CORS 放行头（浏览器读不到内容）
        resp = client.get(
            "/settings/api-key",
            headers={"Origin": "http://evil.example", **LOCAL_HEADERS},
        )
        assert resp.status_code == 403
        assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}

    def test_利用_恶意页面跨站写密钥_阻断(self, client: TestClient) -> None:
        # 利用：evil.example 提交 POST /settings/api-key 覆写受害者本机密钥
        resp = client.post(
            "/settings/api-key",
            headers={"Origin": "https://phishing.example", **LOCAL_HEADERS},
            json={"api_key": _unique_key()},
        )
        assert resp.status_code == 403

    def test_利用_恶意页面跨站读工作区文件_阻断(self, client: TestClient) -> None:
        resp = client.get(
            "/workspace/file",
            params={"path": "pyproject.toml"},
            headers={"Origin": "http://evil.example", **LOCAL_HEADERS},
        )
        assert resp.status_code == 403

    def test_利用_跨站预检不返回通配放行(self, client: TestClient) -> None:
        # 利用：evil.example 先发预检探测是否允许跨域携带自定义头
        resp = client.options(
            "/settings/api-key",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": f"content-type,{CLIENT_HEADER_NAME}",
            },
        )
        assert resp.headers.get("access-control-allow-origin") != "*"
        assert resp.headers.get("access-control-allow-credentials") is None

    @pytest.mark.parametrize("origin", ALLOWED_ORIGINS)
    def test_白名单来源预检放行(self, client: TestClient, origin: str) -> None:
        # 正常路径：本机前端预检必须通过，否则前端整块功能失效
        resp = client.options(
            "/settings/api-key",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": f"content-type,{CLIENT_HEADER_NAME}",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == origin
        # allow_credentials=False：不得下发凭据放行头
        assert resp.headers.get("access-control-allow-credentials") is None

    def test_CORS配置不含通配且不共享凭据(self) -> None:
        # 静态断言：防止后续又把 allow_origins 改回 ["*"] 或打开 allow_credentials
        app = create_app()
        cors_opts = {}
        for middleware in app.user_middleware:
            kwargs = getattr(middleware, "kwargs", {}) or {}
            if "allow_origins" in kwargs:
                cors_opts = kwargs
        assert cors_opts, "未找到 CORS 中间件配置"
        assert "*" not in cors_opts["allow_origins"]
        assert set(cors_opts["allow_origins"]) == set(ALLOWED_ORIGINS)
        assert cors_opts["allow_credentials"] is False


class TestLocalClientHeader:
    """T2：敏感端点零鉴权。"""

    def test_利用_无标识头读取密钥状态_阻断(self, client: TestClient) -> None:
        resp = client.get("/settings/api-key")
        assert resp.status_code == 403

    def test_利用_错误标识头被阻断(self, client: TestClient) -> None:
        resp = client.get("/settings/api-key", headers={CLIENT_HEADER_NAME: "evil"})
        assert resp.status_code == 403

    def test_利用_无标识头删除密钥_阻断且密钥仍在(self, client: TestClient) -> None:
        # 利用：直接 DELETE /settings/api-key 抹掉受害者密钥，逼其重录
        key = _unique_key()
        assert (
            client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS).status_code
            == 200
        )
        assert client.delete("/settings/api-key").status_code == 403
        status = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()
        assert status["configured"] is True

    def test_利用_无标识头读工作区文件_阻断(self, client: TestClient) -> None:
        resp = client.get("/workspace/file", params={"path": "pyproject.toml"})
        assert resp.status_code == 403

    def test_利用_无标识头写工作区文件_阻断(self, client: TestClient) -> None:
        # 目标路径不存在：即便守卫失效也只会 404，不会真的写入文件
        resp = client.put(
            "/workspace/file",
            params={"path": "tests/__not_exists__.txt"},
            json={"content": "tampered"},
        )
        assert resp.status_code == 403

    def test_利用_无标识头列文件树_阻断(self, client: TestClient) -> None:
        assert client.get("/workspace/files").status_code == 403

    def test_正常路径_带标识头可读工作区文件(self, client: TestClient) -> None:
        resp = client.get("/workspace/file", params={"path": "pyproject.toml"}, headers=LOCAL_HEADERS)
        assert resp.status_code == 200


class TestNoEcho:
    """T3：密钥被回显。"""

    def test_保存响应不含明文(self, client: TestClient) -> None:
        key = _unique_key()
        resp = client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        assert resp.status_code == 200
        assert key not in resp.text
        assert set(resp.json()) == set(API_KEY_STATUS_FIELDS)
        assert key not in resp.json()["masked"]

    def test_查询响应不含明文(self, client: TestClient) -> None:
        key = _unique_key()
        client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        resp = client.get("/settings/api-key", headers=LOCAL_HEADERS)
        assert resp.status_code == 200
        assert key not in resp.text

    def test_删除响应不含明文(self, client: TestClient) -> None:
        key = _unique_key()
        client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        resp = client.delete("/settings/api-key", headers=LOCAL_HEADERS)
        assert resp.status_code == 200
        assert key not in resp.text
        body = resp.json()
        assert body["configured"] is False
        assert body["masked"] is None
        assert body["fingerprint"] is None
        assert body["expires_at"] is None
        assert body["rotation_count"] == 0
        assert set(body) == set(API_KEY_STATUS_FIELDS)

    def test_响应模型不存在明文字段(self) -> None:
        # 结构性断言：状态响应体只允许这些非敏感字段
        assert set(ApiKeyStatusResponse.model_fields) == set(API_KEY_STATUS_FIELDS)
        assert not {"api_key", "key", "raw", "secret"} & set(ApiKeyStatusResponse.model_fields)

    def test_明文只存在于进程内取值口(self, client: TestClient) -> None:
        # get() 仅供进程内调用；HTTP 层不得有任何出口把它序列化出去
        key = _unique_key()
        client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        assert get_api_key_store().get() == key
        assert key not in client.get("/openapi.json").text


class TestNotPersisted:
    """T4：密钥落盘。"""

    def test_模拟重启后密钥失效(self, client: TestClient) -> None:
        key = _unique_key()
        client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        assert client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["configured"] is True
        # 单例重建＝进程重启：内存态丢弃
        reset_api_key_store()
        assert client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["configured"] is False

    def test_写入密钥后全盘搜索无命中(self, client: TestClient) -> None:
        key = _unique_key()
        client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        assert _search_disk_for(key) == []


class TestLogAndAuditRedaction:
    """T5：日志/审计泄露明文。"""

    def test_密钥更新日志只记掩码与指纹(self, caplog: pytest.LogCaptureFixture) -> None:
        key = _unique_key()
        store = get_api_key_store()
        with caplog.at_level(logging.INFO, logger="agent_builder.api.secrets"):
            store.set_key(key)
        assert caplog.records, "应至少有一条密钥更新日志"
        assert key not in caplog.text
        assert store.masked_hint() in caplog.text

    def test_清除日志不含明文(self, caplog: pytest.LogCaptureFixture) -> None:
        key = _unique_key()
        store = get_api_key_store()
        store.set_key(key)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="agent_builder.api.secrets"):
            store.clear()
        assert key not in caplog.text

    def test_审计参数经脱敏后不含明文(self) -> None:
        # 门卫审计走 redact_args：键名含 api/key 即整体打码
        key = _unique_key()
        redacted = redact_args({"api_key": key, "path": "/tmp/x"})
        assert key not in json.dumps(redacted, ensure_ascii=False)
        assert redacted["path"] == "/tmp/x"  # 非敏感字段不受影响


class TestInputValidation:
    """T6：密钥注入（HTTP 头 / 日志换行）与格式越界。"""

    @pytest.mark.parametrize(
        "bad_key",
        [
            "",  # 空
            "   ",  # 纯空白
            "short",  # 长度不足
            "a" * (MAX_KEY_LEN + 1),  # 超长
            "sk-abc def",  # 含空格
            "sk-abc\ndef",  # 含换行（日志/头注入）
            "sk-abc\r\nX-Injected: 1",  # CRLF 注入
            "sk-abc\tdef",  # 含制表符
            "sk-abc\x00def",  # 含 NUL
        ],
    )
    def test_利用_非法密钥被拒且不回显(self, client: TestClient, bad_key: str) -> None:
        resp = client.post("/settings/api-key", json={"api_key": bad_key}, headers=LOCAL_HEADERS)
        assert resp.status_code == 400
        if bad_key.strip():
            # 服务端错误信息只含原因，不得回带提交内容
            assert bad_key.strip() not in resp.text
        assert client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["configured"] is False

    def test_利用_CRLF注入不得污染响应头(self, client: TestClient) -> None:
        payload = "sk-abc\r\nX-Injected: 1\r\n\r\n"
        resp = client.post("/settings/api-key", json={"api_key": payload}, headers=LOCAL_HEADERS)
        assert resp.status_code == 400
        assert "x-injected" not in {k.lower() for k in resp.headers}

    def test_正常路径_首尾空白裁剪后接受(self, client: TestClient) -> None:
        key = _unique_key()
        resp = client.post(
            "/settings/api-key", json={"api_key": f"  {key}  "}, headers=LOCAL_HEADERS
        )
        assert resp.status_code == 200
        assert get_api_key_store().get() == key

    def test_正常路径_长度边界值接受(self, client: TestClient) -> None:
        key = "k" * MAX_KEY_LEN
        resp = client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        assert resp.status_code == 200

    def test_缺少api_key字段(self, client: TestClient) -> None:
        resp = client.post("/settings/api-key", json={}, headers=LOCAL_HEADERS)
        assert resp.status_code == 422


class TestFingerprint:
    """非可逆指纹：仅供「是不是同一把钥匙」核对。"""

    def test_同钥匙同指纹_异钥匙异指纹(self, client: TestClient) -> None:
        key_a, key_b = _unique_key(), _unique_key()
        client.post("/settings/api-key", json={"api_key": key_a}, headers=LOCAL_HEADERS)
        fp_a1 = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["fingerprint"]
        client.post("/settings/api-key", json={"api_key": key_a}, headers=LOCAL_HEADERS)
        fp_a2 = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["fingerprint"]
        client.post("/settings/api-key", json={"api_key": key_b}, headers=LOCAL_HEADERS)
        fp_b = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["fingerprint"]
        assert fp_a1 == fp_a2
        assert fp_a1 != fp_b

    def test_指纹长度与形态(self, client: TestClient) -> None:
        key = _unique_key()
        client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        fingerprint = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["fingerprint"]
        assert len(fingerprint) == FINGERPRINT_LEN
        int(fingerprint, 16)  # 必须是十六进制
        assert fingerprint == hashlib.sha256(key.encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]

    def test_指纹不可反推明文(self, client: TestClient) -> None:
        key = _unique_key()
        client.post("/settings/api-key", json={"api_key": key}, headers=LOCAL_HEADERS)
        body = client.get("/settings/api-key", headers=LOCAL_HEADERS).text
        assert key not in body
        assert key[:8] not in body  # 指纹不是明文前缀

    def test_未配置时指纹为空(self, client: TestClient) -> None:
        assert client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["fingerprint"] is None


class TestVerifyProbe:
    """可选的真实连通性校验（verify=true）：失败一律不落库、不改变现状。"""

    @staticmethod
    def _patch_probe(monkeypatch: pytest.MonkeyPatch, result: tuple[bool, str]) -> None:
        from agent_builder.api import routes as routes_module

        monkeypatch.setattr(routes_module, "probe_api_key", lambda *_a, **_k: result)

    def test_探针失败不落库(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_probe(monkeypatch, (False, "密钥被拒绝（HTTP 401）"))
        resp = client.post(
            "/settings/api-key",
            params={"verify": "true"},
            json={"api_key": _unique_key()},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 400
        assert "连通性校验失败" in resp.text
        assert client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["configured"] is False

    def test_探针失败不覆盖已存密钥(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_key = _unique_key()
        client.post("/settings/api-key", json={"api_key": old_key}, headers=LOCAL_HEADERS)
        self._patch_probe(monkeypatch, (False, "无法连接服务（timed out）"))
        resp = client.post(
            "/settings/api-key",
            params={"verify": "true"},
            json={"api_key": _unique_key()},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 400
        fingerprint = client.get("/settings/api-key", headers=LOCAL_HEADERS).json()["fingerprint"]
        assert fingerprint == hashlib.sha256(old_key.encode("utf-8")).hexdigest()[:FINGERPRINT_LEN]

    def test_探针失败信息不含明文(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _unique_key()
        self._patch_probe(monkeypatch, (False, "密钥被拒绝（HTTP 401）"))
        resp = client.post(
            "/settings/api-key",
            params={"verify": "true"},
            json={"api_key": key},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 400
        assert key not in resp.text

    def test_探针通过则落库(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_probe(monkeypatch, (True, "连通性校验通过"))
        resp = client.post(
            "/settings/api-key",
            params={"verify": "true"},
            json={"api_key": _unique_key()},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["configured"] is True

    def test_默认不触发探针(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def _fake(key: str, **_kwargs: object) -> tuple[bool, str]:
            calls.append(key)
            return True, ""

        from agent_builder.api import routes as routes_module

        monkeypatch.setattr(routes_module, "probe_api_key", _fake)
        resp = client.post("/settings/api-key", json={"api_key": _unique_key()}, headers=LOCAL_HEADERS)
        assert resp.status_code == 200
        assert calls == []


class TestWorkspacePathTraversal:
    """相邻控制：路径穿越（T2 的写入面）。"""

    @pytest.mark.parametrize(
        "bad_path",
        ["../.env", "../../secret.txt", "..\\..\\secret.txt", "/etc/passwd", ".git/config", "a:b.txt"],
    )
    def test_利用_路径穿越被拒(self, client: TestClient, bad_path: str) -> None:
        resp = client.get("/workspace/file", params={"path": bad_path}, headers=LOCAL_HEADERS)
        assert resp.status_code == 400

    def test_利用_穿越写被拒(self, client: TestClient) -> None:
        resp = client.put(
            "/workspace/file",
            params={"path": "../escaped.txt"},
            json={"content": "x"},
            headers=LOCAL_HEADERS,
        )
        assert resp.status_code == 400
