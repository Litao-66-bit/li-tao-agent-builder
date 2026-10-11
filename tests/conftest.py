"""全局测试隔离。

卡 41 之后，任务收尾会往"当前工作区"写**项目记忆**（``.agent-memory/project_log.jsonl``）。
而 ``default_workspace_dir()`` 在没有覆盖时会回退到**产品仓库根** —— 于是任何跑过
``/run`` / ``/abort`` / ``/approve`` 的测试都会往仓库里写运行时文件（实测踩到过：
仓库根出现 ``.agent-memory/project_log.jsonl``）。

这里把**默认工作区**统一指到临时目录，保证测试不碰仓库、也不互相串味。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_workspace(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """把默认工作区隔离到临时目录（每个测试一个）。"""
    workspace = tmp_path_factory.mktemp("default-workspace")
    monkeypatch.setenv("AGENT_BUILDER_DEFAULT_WORKSPACE", str(workspace))
    yield workspace
