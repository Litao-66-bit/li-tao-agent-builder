"""项目（工作区）端点回归。

覆盖：
- 默认工作区项目始终可用；敏感端点仍需本机标识头；
- 目录边界：相对路径 / 不存在 / 驱动器根 / 系统保护目录 一律 400；
- 新建文件夹：创建、重名、非法名、父目录越界；
- 登记并切换后 /workspace/* 跟随新工作区（含路径穿越仍被拒）；
- 项目列表落盘到可配置路径，重启后可重新加载。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import reset_project_store, reset_store
from agent_builder.api.projects import ProjectStore
from agent_builder.api.security import CLIENT_HEADER_NAME, CLIENT_HEADER_VALUE

LOCAL_HEADERS = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离项目注册表（落盘到 tmp_path/projects.json）与任务 store。"""
    monkeypatch.setenv("AGENT_BUILDER_PROJECTS_FILE", str(tmp_path / "projects.json"))
    reset_store()
    reset_project_store()
    yield TestClient(create_app())
    reset_project_store()
    reset_store()


def _register(client: TestClient, path: Path):
    return client.post("/projects", json={"path": str(path)}, headers=LOCAL_HEADERS)


def _create_folder(client: TestClient, parent: str, name: str):
    return client.post(
        "/projects/create-folder",
        json={"parent_path": parent, "name": name},
        headers=LOCAL_HEADERS,
    )


class TestProjectBasics:
    def test_默认工作区项目始终可用(self, client: TestClient) -> None:
        resp = client.get("/projects", headers=LOCAL_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_project_id"] == ProjectStore.DEFAULT_ID
        assert data["workspace_dir"]
        assert ProjectStore.DEFAULT_ID in [item["project_id"] for item in data["projects"]]

    def test_敏感端点仍需本机标识头(self, client: TestClient) -> None:
        assert client.get("/projects").status_code == 403
        assert client.post("/projects", json={"path": "x"}).status_code == 403
        assert client.post("/projects/pick-directory").status_code == 403
        assert (
            client.post(
                "/projects/create-folder", json={"parent_path": "x", "name": "y"}
            ).status_code
            == 403
        )
        assert client.post("/projects/default/open").status_code == 403


class TestWorkspaceBoundary:
    def test_相对路径被拒(self, client: TestClient) -> None:
        resp = client.post("/projects", json={"path": "relative/dir"}, headers=LOCAL_HEADERS)
        assert resp.status_code == 400

    def test_不存在目录被拒(self, client: TestClient, tmp_path: Path) -> None:
        assert _register(client, tmp_path / "not-created").status_code == 400

    def test_驱动器根被拒(self, client: TestClient) -> None:
        anchor = Path.cwd().anchor  # Windows: "C:\\"；POSIX: "/"
        resp = client.post("/projects", json={"path": anchor}, headers=LOCAL_HEADERS)
        assert resp.status_code == 400

    @pytest.mark.skipif(os.name != "nt", reason="仅 Windows 有 SystemRoot 保护目录")
    def test_系统保护目录被拒(self, client: TestClient) -> None:
        system_root = os.environ.get("SystemRoot")
        assert system_root
        resp = client.post("/projects", json={"path": system_root}, headers=LOCAL_HEADERS)
        assert resp.status_code == 400


class TestPickDirectory:
    def test_取消返回cancelled(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agent_builder.api.routes.pick_directory", lambda initial_dir=None: None)
        resp = client.post("/projects/pick-directory", headers=LOCAL_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True

    def test_选中目录返回绝对路径(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        picked = tmp_path / "picked"
        picked.mkdir()
        monkeypatch.setattr(
            "agent_builder.api.routes.pick_directory", lambda initial_dir=None: str(picked)
        )
        resp = client.post("/projects/pick-directory", headers=LOCAL_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["cancelled"] is False
        assert Path(body["path"]) == picked.resolve()

    def test_对话框不可用返回501(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(initial_dir=None):
            raise RuntimeError("no gui")

        monkeypatch.setattr("agent_builder.api.routes.pick_directory", _boom)
        assert client.post("/projects/pick-directory", headers=LOCAL_HEADERS).status_code == 501


class TestCreateFolder:
    def test_正常创建(self, client: TestClient, tmp_path: Path) -> None:
        resp = _create_folder(client, str(tmp_path), "my-workspace")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cancelled"] is False
        assert body["name"] == "my-workspace"
        assert (tmp_path / "my-workspace").is_dir()

    def test_重名被拒(self, client: TestClient, tmp_path: Path) -> None:
        (tmp_path / "dup").mkdir()
        assert _create_folder(client, str(tmp_path), "dup").status_code == 400

    @pytest.mark.parametrize("bad_name", ["", "   ", ".hidden", "a/b", "a:b", "x" * 65])
    def test_非法名被拒(self, client: TestClient, tmp_path: Path, bad_name: str) -> None:
        assert _create_folder(client, str(tmp_path), bad_name).status_code == 400

    def test_父目录越界被拒(self, client: TestClient, tmp_path: Path) -> None:
        assert _create_folder(client, str(tmp_path / "nope"), "x").status_code == 400


class TestSwitchWorkspace:
    def test_登记切换后文件树跟随(self, client: TestClient, tmp_path: Path) -> None:
        workspace = tmp_path / "ws-a"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("hi", encoding="utf-8")

        project = _register(client, workspace).json()
        assert project["path"] == str(workspace.resolve())

        listing = client.get("/projects", headers=LOCAL_HEADERS).json()
        assert listing["current_project_id"] == project["project_id"]
        assert Path(listing["workspace_dir"]) == workspace.resolve()

        files = client.get("/workspace/files", headers=LOCAL_HEADERS).json()
        assert "hello.txt" in [node["name"] for node in files]

        read = client.get(
            "/workspace/file", params={"path": "hello.txt"}, headers=LOCAL_HEADERS
        )
        assert read.status_code == 200
        assert read.json()["content"] == "hi"

        # 切回默认工作区后，新工作区的文件不再可见。
        assert (
            client.post(
                f"/projects/{ProjectStore.DEFAULT_ID}/open", headers=LOCAL_HEADERS
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/workspace/file", params={"path": "hello.txt"}, headers=LOCAL_HEADERS
            ).status_code
            == 404
        )

    def test_切换不存在的项目返回404(self, client: TestClient) -> None:
        assert client.post("/projects/nonexistent/open", headers=LOCAL_HEADERS).status_code == 404

    def test_重复登记同路径复用同一项目(self, client: TestClient, tmp_path: Path) -> None:
        workspace = tmp_path / "ws-b"
        workspace.mkdir()
        first = _register(client, workspace).json()
        second = _register(client, workspace).json()
        assert first["project_id"] == second["project_id"]

    def test_路径穿越在当前工作区下仍被拒(self, client: TestClient, tmp_path: Path) -> None:
        workspace = tmp_path / "ws-c"
        workspace.mkdir()
        _register(client, workspace)
        resp = client.get(
            "/workspace/file", params={"path": "../secret.txt"}, headers=LOCAL_HEADERS
        )
        assert resp.status_code == 400


class TestLargeFilePreview:
    """大文件也能预览：去掉旧的 20MB 硬上限，任意大小只读截断预览。

    回归：旧实现 ``> MAX_PREVIEW_BYTES（20MB）`` 直接 413 —— 用户永远看不到大文件；
    现在始终只读前 2MB（``MAX_EDITABLE_BYTES``），GB 级文件也能秒开。
    """

    def test_超过旧上限的文件仍可只读截断预览(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws-huge"
        workspace.mkdir()
        # ~21MB 纯文本：> 旧预览上限（20MB），且是合法 UTF-8（无 NUL）。
        (workspace / "huge.txt").write_text(("x" * 1023 + "\n") * 21_000, encoding="utf-8")
        _register(client, workspace)

        resp = client.get("/workspace/file", params={"path": "huge.txt"}, headers=LOCAL_HEADERS)

        assert resp.status_code == 200  # 不再 413
        body = resp.json()
        assert body["truncated"] is True              # 前端据此禁止编辑
        assert body["size"] > 20 * 1024 * 1024        # 真实大小如实回传
        # 只回前 2MB（不是整份），否则会把响应体撑爆。
        assert len(body["content"].encode("utf-8")) <= 2 * 1024 * 1024


class TestPersistence:
    def test_项目落盘并可被重新加载(self, tmp_path: Path) -> None:
        data_file = tmp_path / "projects.json"
        store = ProjectStore(data_file)
        workspace = tmp_path / "ws-persist"
        workspace.mkdir()
        created = store.register(workspace)
        assert data_file.exists()

        reloaded = ProjectStore(data_file)
        current = reloaded.current()
        assert current is not None
        assert current.project_id == created.project_id
        assert [item.project_id for item in reloaded.list_projects()].count(
            created.project_id
        ) == 1
