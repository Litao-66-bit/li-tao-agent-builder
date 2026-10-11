"""卡 41：工作区级「项目进度」记忆（跨任务、跨重启的后端记忆）。

实测（2026-10-11，用户第二次报"还是没有记忆"）：任务历史只存内存、前端拼的上文
刷新/切项目/重启即失效、提示词里没有工作区现状 —— 三条一起导致"没有记忆"。
"""

from __future__ import annotations

from pathlib import Path

from agent_builder.api.project_log import (
    ProjectLogEntry,
    build_project_context,
    project_log_path,
    read_project_log,
    record_project_log,
    workspace_file_snapshot,
)


def _entry(task_id: str, **kw) -> ProjectLogEntry:
    base = {
        "task_id": task_id,
        "requirement": f"需求-{task_id}",
        "title": f"标题-{task_id}",
        "status": "verifying",
        "conclusion": f"结论-{task_id}",
        "artifacts": [f"outputs/{task_id}.py"],
        "updated_at": f"2026-10-11T00:0{task_id[-1]}:00+00:00",
    }
    base.update(kw)
    return ProjectLogEntry(**base)


class TestProjectLogStorage:
    def test_写入后可读回(self, tmp_path: Path) -> None:
        record_project_log(tmp_path, _entry("t1"))

        rows = read_project_log(tmp_path)

        assert len(rows) == 1
        assert rows[0].task_id == "t1"
        assert rows[0].artifacts == ["outputs/t1.py"]
        assert project_log_path(tmp_path).is_file()
        # 落在工作区内的 .agent-memory 下（已在 .gitignore）
        assert ".agent-memory" in str(project_log_path(tmp_path))

    def test_同任务upsert只留最新(self, tmp_path: Path) -> None:
        record_project_log(tmp_path, _entry("t1", conclusion="旧的"))
        record_project_log(tmp_path, _entry("t1", conclusion="新的"))

        rows = read_project_log(tmp_path)

        assert len(rows) == 1
        assert rows[0].conclusion == "新的"

    def test_读坏文件不致命(self, tmp_path: Path) -> None:
        path = project_log_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("这不是 JSON\n{坏行\n", encoding="utf-8")

        assert read_project_log(tmp_path) == []

    def test_文件不存在返回空表(self, tmp_path: Path) -> None:
        assert read_project_log(tmp_path / "nope") == []

    def test_日志条数有上限(self, tmp_path: Path) -> None:
        from agent_builder.api.project_log import MAX_LOG_ENTRIES

        for i in range(MAX_LOG_ENTRIES + 5):
            record_project_log(
                tmp_path, _entry(f"t{i:02d}", updated_at=f"2026-10-11T00:{i:02d}:00+00:00")
            )

        assert len(read_project_log(tmp_path)) == MAX_LOG_ENTRIES


class TestProjectContext:
    def test_压成提示词片段且最新在前(self, tmp_path: Path) -> None:
        record_project_log(tmp_path, _entry("t1", updated_at="2026-10-11T00:01:00+00:00"))
        record_project_log(tmp_path, _entry("t2", updated_at="2026-10-11T00:02:00+00:00"))

        text = build_project_context(read_project_log(tmp_path))

        assert "需求-t2" in text and "需求-t1" in text
        assert text.index("需求-t2") < text.index("需求-t1"), "最新的要排在最前"
        assert "产出文件：outputs/t2.py" in text

    def test_可以排除自己(self, tmp_path: Path) -> None:
        record_project_log(tmp_path, _entry("t1"))
        record_project_log(tmp_path, _entry("t2"))

        text = build_project_context(read_project_log(tmp_path), exclude_task_id="t2")

        assert "需求-t1" in text
        assert "需求-t2" not in text

    def test_跳过没有任何产出的空记录(self, tmp_path: Path) -> None:
        """什么都没干成的记录不该占据记忆（否则上下文全是"状态：executing"这种噪声）。"""
        record_project_log(tmp_path, _entry("t0", conclusion="", artifacts=[]))
        record_project_log(tmp_path, _entry("t1"))

        text = build_project_context(read_project_log(tmp_path))

        assert "需求-t1" in text
        assert "需求-t0" not in text

    def test_没有历史时返回空串(self, tmp_path: Path) -> None:
        assert build_project_context([]) == ""

    def test_超长截断并注明(self, tmp_path: Path) -> None:
        from agent_builder.api.project_log import MAX_PROJECT_CONTEXT_CHARS

        record_project_log(tmp_path, _entry("t1", conclusion="结" * (MAX_PROJECT_CONTEXT_CHARS + 200)))

        text = build_project_context(read_project_log(tmp_path))

        assert "已截断" in text


class TestWorkspaceSnapshot:
    def test_列出文件与目录且跳过隐藏与缓存(self, tmp_path: Path) -> None:
        (tmp_path / "outputs").mkdir()
        (tmp_path / "outputs" / "a.py").write_text("x = 1", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / ".env").write_text("x", encoding="utf-8")

        text = workspace_file_snapshot(tmp_path)

        assert "outputs/" in text
        assert "outputs/a.py" in text
        assert "a.py (5B)" in text
        assert "__pycache__" not in text
        assert ".git/" not in text
        assert ".env" not in text

    def test_空工作区有明确说法(self, tmp_path: Path) -> None:
        assert "空工作区" in workspace_file_snapshot(tmp_path)

    def test_目录不存在返回空串(self, tmp_path: Path) -> None:
        assert workspace_file_snapshot(tmp_path / "nope") == ""
