"""执行形态 A/B（P3）跑批脚本的不变量回归。

跑批本身要真后端 + 密钥，无法进单测；但**指标抽取、聚合、报告渲染、工作区守卫**
都是纯函数 / 纯文件操作，必须钉死，否则「报告里的数字」会悄悄漂移。

对应口径：P0 质量（完成率 / 失败步骤 / 返工）→ P1 成本（token / 循环轮数）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_builder.evaluation.mode_ab import (
    COMPLETED_STATUSES,
    MARKER,
    OUTPUT_DIR,
    TASKS,
    assert_workspace_safe,
    cleanup_workspace,
    extract_metrics,
    prepare_workspace,
    render_summary,
    summarize_arm,
)


def _run(
    *,
    arm: str = "agentic",
    task: str = "R1-research-paper-agent",
    rep: int = 1,
    completed: int = 1,
    failed_steps: int = 0,
    rework_count: int = 0,
    loop_rounds: int = 1,
    tokens: int = 100,
    task_status: str = "verifying",
    error: str | None = None,
) -> dict:
    """构造一条与 run_once 同形状的汇总记录。"""
    return {
        "task": task,
        "mode": arm,
        "rep": rep,
        "completed": completed,
        "failed_steps": failed_steps,
        "rework_count": rework_count,
        "loop_rounds": loop_rounds,
        "tokens": tokens,
        "task_status": task_status,
        "loop_stopped_reason": "",
        "error": error,
    }


class TestTaskSet:
    """任务集：4 个真实需求，首条是「调研论文 agent」主任务。"""

    def test_共四个任务且id唯一(self) -> None:
        assert len(TASKS) == 4
        ids = [t["id"] for t in TASKS]
        assert len(set(ids)) == len(ids)

    def test_首条是调研论文agent(self) -> None:
        assert TASKS[0]["id"] == "R1-research-paper-agent"
        assert "调研论文" in TASKS[0]["requirement"]
        assert "agent" in TASKS[0]["requirement"]

    def test_所有需求都写在独占目录内(self) -> None:
        """产物必须落在 ``_mode_ab_out/``，跑完整目录删除才安全。"""
        for task in TASKS:
            assert OUTPUT_DIR in task["requirement"], task["id"]


class TestExtractMetrics:
    """指标抽取：从 TaskResponse + /usage 得到可比数字。"""

    def test_走到verifying算完成(self) -> None:
        metrics = extract_metrics(
            "agentic",
            {"status": "verifying", "execution_results": []},
            {"total_tokens": 0},
        )
        assert metrics["completed"] == 1
        assert metrics["task_status"] == "verifying"

    def test_停在executing不算完成(self) -> None:
        metrics = extract_metrics(
            "agentic",
            {"status": "executing", "execution_results": []},
            {"total_tokens": 0},
        )
        assert metrics["completed"] == 0

    def test_统计步数失败与返工(self) -> None:
        final = {
            "status": "verifying",
            "execution_results": [
                {"status": "done", "retries": 0},
                {"status": "done", "retries": 1},
                {"status": "failed", "retries": 2},
            ],
        }
        metrics = extract_metrics("workflow", final, {"total_tokens": 42})
        assert metrics["step_count"] == 3
        assert metrics["done_steps"] == 2
        assert metrics["failed_steps"] == 1
        assert metrics["rework_count"] == 3  # 0 + 1 + 2

    def test_agentic无循环即判定回退(self) -> None:
        """agentic 档却没 loop_state → 无密钥回退固定工作流，该 run 不能算 agentic。"""
        metrics = extract_metrics("agentic", {"status": "verifying"}, {"total_tokens": 1})
        assert metrics["agentic_fell_back"] is True
        assert metrics["loop_rounds"] == 0

    def test_agentic有循环且不回退(self) -> None:
        final = {"status": "verifying", "loop_state": {"rounds": [{"round": 1}, {"round": 2}]}}
        metrics = extract_metrics("agentic", final, {"total_tokens": 1})
        assert metrics["agentic_fell_back"] is False
        assert metrics["loop_rounds"] == 2

    def test_workflow不判回退(self) -> None:
        metrics = extract_metrics("workflow", {"status": "verifying"}, {"total_tokens": 1})
        assert metrics["agentic_fell_back"] is False

    def test_token字段兼容两种形态(self) -> None:
        assert extract_metrics("agentic", {}, {"total_tokens": 7})["tokens"] == 7
        assert extract_metrics("agentic", {}, {"total": 9})["tokens"] == 9
        assert extract_metrics("agentic", {}, {})["tokens"] == 0

    def test_完成状态集合(self) -> None:
        assert "verifying" in COMPLETED_STATUSES


class TestSummarizeArm:
    """按档聚合：完成率 / 失败 / 返工 / 成本。"""

    def test_聚合均值与合计(self) -> None:
        runs = [
            _run(arm="agentic", completed=1, tokens=100, loop_rounds=2),
            _run(arm="agentic", rep=2, completed=0, failed_steps=1, rework_count=1, tokens=300),
        ]
        summary = summarize_arm(runs, "agentic")
        assert summary["runs"] == 2
        assert summary["completion_rate"] == pytest.approx(0.5)
        assert summary["failed_steps"] == 1
        assert summary["rework"] == 1
        assert summary["tokens_median"] == 200
        assert summary["tokens_mean"] == 200

    def test_排除报错运行(self) -> None:
        runs = [
            _run(arm="agentic", completed=1),
            _run(arm="agentic", rep=2, completed=0, error="boom"),
        ]
        summary = summarize_arm(runs, "agentic")
        assert summary["runs"] == 1
        assert summary["completion_rate"] == 1.0

    def test_空列表不炸(self) -> None:
        summary = summarize_arm([], "workflow")
        assert summary["runs"] == 0
        assert summary["completion_rate"] == 0.0


class TestRenderSummary:
    """报告：P0 质量在前、P1 成本在后，并给出质量优先的判定。"""

    def test_质量章节在成本章节之前(self) -> None:
        runs = [_run(arm="workflow"), _run(arm="agentic")]
        text = render_summary(runs, ["workflow", "agentic"])
        assert "## P0 质量" in text
        assert "## P1 成本" in text
        assert text.index("P0 质量") < text.index("P1 成本")

    def test_含判定与逐任务明细(self) -> None:
        runs = [
            _run(arm="workflow", completed=0, failed_steps=2),
            _run(arm="agentic", completed=1, failed_steps=0),
        ]
        text = render_summary(runs, ["workflow", "agentic"])
        assert "## 判定" in text
        assert "## 逐任务明细" in text
        # 完成率有差 → 按质量优先，倾向完成率更高的一档。
        assert "倾向 `agentic`" in text
        assert "R1-research-paper-agent" in text

    def test_完成率持平则看失败与返工(self) -> None:
        runs = [
            _run(arm="workflow", completed=1, failed_steps=3),
            _run(arm="agentic", completed=1, failed_steps=0),
        ]
        text = render_summary(runs, ["workflow", "agentic"])
        assert "倾向 `agentic`" in text

    def test_空运行也能渲染(self) -> None:
        text = render_summary([], ["workflow", "agentic"])
        assert "# 执行形态 A/B" in text


class TestWorkspaceGuard:
    """工作区守卫：独占目录 + 标记文件，跑完整目录删除，绝不碰用户内容。"""

    def test_准备后带标记且写入素材(self, tmp_path: Path) -> None:
        prepare_workspace(tmp_path)
        owned = tmp_path / OUTPUT_DIR
        assert (owned / MARKER).is_file()
        assert (owned / "notes" / "literature.md").is_file()

    def test_清理删除独占目录(self, tmp_path: Path) -> None:
        prepare_workspace(tmp_path)
        cleanup_workspace(tmp_path)
        assert not (tmp_path / OUTPUT_DIR).exists()

    def test_无标记的同名目录会被拒绝(self, tmp_path: Path) -> None:
        owned = tmp_path / OUTPUT_DIR
        owned.mkdir()
        (owned / "用户的文件.txt").write_text("别删我", encoding="utf-8")
        with pytest.raises(SystemExit):
            assert_workspace_safe(tmp_path)

    def test_带标记的同名目录放行(self, tmp_path: Path) -> None:
        prepare_workspace(tmp_path)
        assert_workspace_safe(tmp_path)  # 不抛

    def test_无标记不清理(self, tmp_path: Path) -> None:
        owned = tmp_path / OUTPUT_DIR
        owned.mkdir()
        (owned / "keep.txt").write_text("x", encoding="utf-8")
        cleanup_workspace(tmp_path)
        assert (owned / "keep.txt").is_file()  # 不是我们的目录 → 不动
