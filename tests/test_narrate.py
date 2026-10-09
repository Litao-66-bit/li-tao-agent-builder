"""人读文本（narrate）测试：动作中文名 / 步骤描述 / 结果摘要 / 异常人话化。

覆盖：
- 纯函数：已知与未知 action、参数人话化、结构化结果提炼、异常前缀剥离；
- 接线：分解器写 ``Step.title`` / ``Step.description``；
  执行编排器的 ``execution_results[*].summary`` 不再外泄英文 action / JSON / 异常原文。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.deps import get_store, reset_project_store, reset_store
from agent_builder.api.secrets import reset_api_key_store
from agent_builder.contracts.schemas import Plan, Step
from agent_builder.narrate import (
    NO_ARTIFACT_NOTE,
    action_title,
    conclude,
    describe_inputs,
    describe_step,
    humanize_error,
    summarize_result,
)
from agent_builder.roles.decomposer import Decomposer

_LOCAL_HEADERS = {"X-Agent-Builder-Client": "web"}


# ── 动作中文名 ──────────────────────────────────────────────────


class TestActionTitle:
    def test_已知动作翻成中文(self):
        assert action_title("file_write") == "写入文件"
        assert action_title("web_fetch") == "抓取网页"

    def test_未知动作原样返回不编造(self):
        assert action_title("no_such_action") == "no_such_action"


# ── 步骤描述 ────────────────────────────────────────────────────


class TestDescribeStep:
    def test_标题与参数人话(self):
        title, description = describe_step("file_write", {"path": "a.md", "content": "x" * 30})
        assert title == "写入文件"
        assert "文件：a.md" in description
        assert "内容 30 字" in description
        assert "key=" not in description and "path=" not in description

    def test_无参数时描述等于标题(self):
        assert describe_step("file_list") == ("列出文件", "列出文件")

    def test_输入不是字典时不猜(self):
        assert describe_inputs("not-a-dict") == []
        assert describe_inputs(None) == []

    def test_长文本只说字数不倒原文(self):
        parts = describe_inputs({"content": "秘密" * 100})
        assert parts == ["内容 200 字"]
        assert "秘密" not in "".join(parts)

    def test_未知键如实回显(self):
        assert describe_inputs({"custom_key": "v"}) == ["custom_key：v"]

    def test_覆盖标记只在为真时提示(self):
        assert describe_inputs({"path": "a.md", "overwrite": False}) == ["文件：a.md"]
        assert "允许覆盖" in describe_inputs({"path": "a.md", "overwrite": True})

    def test_列目录的path标成目录(self):
        """file_list 的 path 是目录 —— 标成「文件」会误导（实测模型和人都看错）。"""
        assert describe_inputs({"path": "."}, action="file_list") == ["目录：."]
        # 不给 action 时行为不变（读写类仍是「文件」）。
        assert describe_inputs({"path": "."}) == ["文件：."]
        assert describe_step("file_list", {"path": "."}) == ("列出文件", "列出文件（目录：.）")


# ── 异常人话化 ──────────────────────────────────────────────────


class TestHumanizeError:
    def test_剥离异常与工具名前缀(self):
        raw = "RuntimeError: AgentError: web_fetch: HTTP 错误: 404 Not Found"
        assert humanize_error(raw) == "目标返回 HTTP 404（页面不存在）"

    def test_剥离错误码与工具名前缀(self):
        """``E_VALIDATION: file_read: …`` 里的错误码与英文工具名都要剥掉。"""
        raw = "E_VALIDATION: file_read: 路径不是文件: tests"
        assert humanize_error(raw) == "路径不是文件: tests"

    def test_错误码在前异常名在后也剥干净(self):
        """剥前缀后仍要走匹配：越权类错误收敛成统一人话，不漏原始错误码。"""
        raw = "AgentError: E_PERMISSION: file_write: 越权写入"
        assert humanize_error(raw) == "未获授权，已被拦截"

    def test_服务端异常给出人话(self):
        assert humanize_error("AgentError: HTTP 错误: 500 Internal Server Error") == (
            "目标返回 HTTP 500（对方服务异常）"
        )

    def test_命令缺失判为环境问题(self):
        raw = "[stderr] 'python' is not recognized as an internal or external command"
        assert humanize_error(raw) == "本机缺少所需命令（环境问题）"

    def test_未授权(self):
        assert humanize_error("PermissionError: 未授权：file_write") == "未获授权，已被拦截"

    def test_空错误不编造原因(self):
        assert humanize_error(None) == "原因未知"
        assert humanize_error("") == "原因未知"

    def test_深处关键词不得劫持真实原因(self):
        """实测（质量探针 run-1）：失败原文是**一条 622 字的单行**——真实原因（失败 4 项）
        在前 100 字，而第 441 字处嵌着某个用例抛出的 ``PermissionError``（DSH 沙箱下
        临时目录被拒）。全文匹配会让深处那个词把整条原因劫持成「未获授权，已被拦截」，
        真实原因被彻底盖掉。"""
        filler = "".join(f"tests/test_x.py::test_case_{i}；" for i in range(8))
        raw = (
            "RuntimeError: 测试未通过：失败 4 项、错误 1 项（通过 2 项）；"
            + filler
            + "PermissionError: [WinError 5] 拒绝访问。: 'C:\\Temp\\pytest-of-x'"
        )
        assert raw.index("PermissionError") > 120  # 关键词确实在窗口之外

        out = humanize_error(raw)

        assert out != "未获授权，已被拦截"
        assert "失败 4 项" in out


# ── 结果摘要 ────────────────────────────────────────────────────


class TestSummarizeResult:
    def test_状态分支(self):
        assert summarize_result(action="file_write", status="pending") == "尚未执行"
        assert summarize_result(action="file_write", status="skipped") == "已跳过（依赖步骤未完成）"
        assert (
            summarize_result(action="file_write", status="pending_approval")
            == "等待你放行：写入文件"
        )

    def test_结构化结果不再倾倒JSON(self):
        payload = {
            "step_id": "step-008",
            "status": "done",
            "files_changed": ["research_agent_report.md"],
            "change_desc": "新增两章",
        }
        summary = summarize_result(action="file_write", status="done", result=payload)
        assert summary == "已写入 research_agent_report.md：新增两章"
        assert "{" not in summary and "step_id" not in summary

    def test_工具字符串结果(self):
        summary = summarize_result(
            action="file_write",
            status="done",
            result="wrote a.md (1200 chars)",
            inputs={"path": "a.md"},
        )
        assert summary == "已写入 a.md（1200 字）"

    def test_只读动作不说已写入(self):
        """只读动作同样带 files_changed，但绝不能写成「已写入」。"""
        payload = {
            "status": "done",
            "files_changed": ["tests"],
            "change_desc": "列出了 `tests` 目录下的文件列表。",
        }
        summary = summarize_result(action="file_list", status="done", result=payload)
        assert summary == "列出了 `tests` 目录下的文件列表。"
        assert "已写入" not in summary

    def test_只读动作缺描述时回退中文动作名(self):
        payload = {"status": "done", "files_changed": ["tests"]}
        summary = summarize_result(action="file_list", status="done", result=payload)
        assert summary == "列出文件：tests"
        assert "已写入" not in summary

    def test_无可用字段的字典不倾倒原文(self):
        summary = summarize_result(action="web_search", status="done", result={"status": "done"})
        assert summary == "搜索资料完成"

    def test_失败摘要含原因与重试次数(self):
        summary = summarize_result(
            action="web_fetch",
            status="failed",
            error="RuntimeError: AgentError: web_fetch: HTTP 错误: 404 Not Found",
            retries=2,
        )
        assert summary == "抓取网页失败：目标返回 HTTP 404（页面不存在）（已重试 2 次）"

    def test_质量报告是合法产出不是失败(self):
        summary = summarize_result(
            action="data_query", status="done", result={"status": "quality_report"}
        )
        assert summary == "数据样本不足，只出具质量报告（未下结论）"

    def test_长文本结果只说字数(self):
        summary = summarize_result(action="web_fetch", status="done", result="正文" * 700)
        assert summary == "抓取网页完成（1.4k 字）"

    def test_搜索代码按命中条数说而不是字数(self):
        """实机踩过：200 条匹配被报成「198.1k 字」，对用户毫无信息量。"""
        result = "\n".join(f"a.py:{i}:import os" for i in range(1, 4))
        summary = summarize_result(action="code_search", status="done", result=result)
        assert summary == "搜索代码完成（命中 3 条）"

    def test_搜索结果被截断时点明已截断(self):
        result = "a.py:1:import os\n…[已截断，仅显示前 200 条匹配]"
        summary = summarize_result(action="code_search", status="done", result=result)
        assert summary == "搜索代码完成（命中 1 条，已截断）"

    def test_报告只在内存汇总时如实说明未落盘(self):
        """实机踩过：结论区写「最终调研报告已由 summarizer 产出」，工作区里却没有报告。"""
        payload = {
            "status": "done",
            "sections": [{"title": "结论", "content": "..."}],
            "conclusions": ["a", "b"],
            "pending_items": ["补引用"],
            "missing_items": [],
        }
        summary = summarize_result(action="report", status="done", result=payload)
        assert summary == "已汇总报告（2 条结论，1 项未完成；仅内存汇总，未落盘）"

    def test_测试计数全零不说测试通过(self):
        """计数全 0 = 这轮没核对到用例 → 不能报「测试通过 0 项」（实机踩过的假绿）。"""
        summary = summarize_result(
            action="test_run",
            status="done",
            result={"status": "done", "passed": 0, "failed": 0, "error": 0},
        )
        assert summary == "运行测试完成"
        assert "测试通过" not in summary

    def test_真实计数照说测试通过N项(self):
        summary = summarize_result(
            action="test_run", status="done", result={"status": "done", "passed": 12}
        )
        assert summary == "测试通过 12 项"


# ── 结论区：跑完一句人话结论 ────────────────────────────────────


class TestConclude:
    """``conclude`` 是「结论区」的唯一来源：优先用模型结论，否则如实汇总。"""

    def test_模型结论优先(self):
        assert conclude(final_answer="已经把调研结论写成报告", execution_results=[]) == (
            "已经把调研结论写成报告"
        )

    def test_没有模型结论时按执行结果如实汇总(self):
        results = [
            {"status": "done", "artifacts": ["a.md"]},
            {"status": "done", "artifacts": []},
        ]
        assert conclude(execution_results=results) == "执行完成：2 步全部成功，产出 a.md"

    def test_有失败时说清成功与失败数(self):
        results = [{"status": "done", "artifacts": []}, {"status": "failed", "artifacts": []}]
        assert conclude(execution_results=results) == "执行了 2 步：成功 1 步、失败 1 步"

    def test_待放行时说清在等谁(self):
        results = [{"status": "pending_approval", "artifacts": []}]
        assert conclude(execution_results=results) == "执行暂停：1 步中 1 步等待你放行（已完成 0 步）"

    def test_没有依据时不给结论(self):
        """没跑、没产出 → 空串（宁可没有结论，也不写"任务已完成"这种空话）。"""
        assert conclude() == ""
        assert conclude(final_answer="   ", execution_results=[]) == ""
        assert conclude(execution_results=[{"status": "done"}]) == (
            "执行完成：1 步全部成功"
        )

    def test_用户中断时说已中断而非执行完成(self):
        """实机复现：顶栏写着「已暂停」，结论却写「执行完成」，自相矛盾。"""
        results = [{"status": "done", "artifacts": []}, {"status": "done", "artifacts": []}]
        assert conclude(execution_results=results, status="interrupted") == (
            "已中断：共 2 步，已完成 2 步"
        )

    def test_中断且有失败时一并说清(self):
        results = [{"status": "done", "artifacts": []}, {"status": "failed", "artifacts": []}]
        assert conclude(execution_results=results, status="interrupted") == (
            "已中断：共 2 步，已完成 1 步、失败 1 步"
        )

    def test_待放行优先于中断措辞(self):
        """「等待放行」是更具体的原因，不能被泛化的「已中断」盖掉。"""
        results = [{"status": "pending_approval", "artifacts": []}]
        assert conclude(execution_results=results, status="interrupted") == (
            "执行暂停：1 步中 1 步等待你放行（已完成 0 步）"
        )

    def test_非收敛停下不说执行完成(self):
        """预算耗尽 / 空转 / 决策非法 → 顶栏「已停下」，结论也不能说「执行完成」。"""
        results = [{"status": "done", "artifacts": []}]
        assert conclude(execution_results=results, status="executing", stopped=True) == (
            "已停下：1 步均成功，但任务未收尾"
        )
        # 收敛（stopped=False）/ 老调用方没传 status → 措辞不变（向后兼容）。
        assert conclude(execution_results=results, status="executing") == (
            "执行完成：1 步全部成功"
        )
        assert conclude(execution_results=results, status="verifying") == (
            "执行完成：1 步全部成功"
        )

    def test_非收敛停下且有失败时也说已停下(self):
        """实机复现：顶栏「已停下」+ 停下卡都在，结论却只写「执行了 11 步…」。"""
        results = [
            {"status": "done", "artifacts": []},
            {"status": "done", "artifacts": []},
            {"status": "failed", "artifacts": []},
        ]
        assert conclude(execution_results=results, status="executing", stopped=True) == (
            "已停下：共 3 步，成功 2 步、失败 1 步"
        )
        # 非停下时措辞不变（向后兼容）。
        assert conclude(execution_results=results, status="executing") == (
            "执行了 3 步：成功 2 步、失败 1 步"
        )

    def test_零产物时给模型结论补事实说明(self):
        """实机复现：10 步零产物，模型结论却写「已完成…构建与验证」。"""
        results = [{"status": "done", "artifacts": []}]
        assert conclude(final_answer="已完成调研论文 agent 的构建", execution_results=results) == (
            f"已完成调研论文 agent 的构建{NO_ARTIFACT_NOTE}"
        )

    def test_没有声称交付时不加事实说明(self):
        """如实的「无需改动」不是谎话，不该被加说明（避免误伤正常的无产出任务）。"""
        results = [{"status": "done", "artifacts": []}]
        for answer in ("本次只预览了差异，未改动任何文件", "已预览完差异，无需改动"):
            assert conclude(final_answer=answer, execution_results=results) == answer

    def test_结论引用未产出的文件时点明(self):
        """实机复现：产出了 agent.py / test_agent.py，结论却写「用法：
        python -m research_agent.main」—— main.py 根本不存在。"""
        results = [
            {"status": "done", "artifacts": ["research_agent/agent.py"]},
            {"status": "done", "artifacts": ["research_agent/test_agent.py"]},
        ]
        answer = '用法：python -m research_agent.main "主题"'
        assert conclude(final_answer=answer, execution_results=results) == (
            '用法：python -m research_agent.main "主题"'
            "（说明：本任务未产出 research_agent/main.py；"
            "实际产出 research_agent/agent.py、research_agent/test_agent.py）"
        )

    def test_结论引用已产出的文件不加说明(self):
        results = [{"status": "done", "artifacts": ["research_agent/agent.py"]}]
        answer = "已写出 research_agent/agent.py，可直接运行"
        assert conclude(final_answer=answer, execution_results=results) == answer

    def test_产出目录之外的引用不报警(self):
        """「参考了 docs/HANDOVER.md」是正常引用，不该被当成编造。"""
        results = [{"status": "done", "artifacts": ["research_agent/agent.py"]}]
        answer = "先参考了 docs/HANDOVER.md，再写出研究代码"
        assert conclude(final_answer=answer, execution_results=results) == answer

    def test_执行类动作集合与评估侧保持同步(self):
        """narrate 自持一份执行类动作以保持零依赖，但不能和评估侧悄悄漂移。"""
        from agent_builder.evaluation.tool_report import EXEC_TOOLS
        from agent_builder.narrate import _EXEC_ACTIONS

        assert _EXEC_ACTIONS == EXEC_TOOLS

    def test_声称验证通过时附上后端核对证据(self):
        """「验证通过」可能只是代码内的自证 —— 真实运行证据由后端说。"""
        results = [
            {"action": "file_write", "status": "done", "artifacts": ["a.py"]},
            {"action": "test_run", "status": "done", "artifacts": []},
            {"action": "test_run", "status": "failed", "artifacts": []},
        ]
        answer = "已写出 a.py，验证通过"
        assert conclude(final_answer=answer, execution_results=results) == (
            "已写出 a.py，验证通过（系统核对：真实运行 2 次，成功 1 次、失败 1 次）"
        )

    def test_没有真实运行时点明验证只是自述(self):
        results = [{"action": "file_write", "status": "done", "artifacts": ["a.py"]}]
        answer = "已写出 a.py，测试全部通过"
        assert conclude(final_answer=answer, execution_results=results) == (
            "已写出 a.py，测试全部通过"
            "（系统核对：本次未真实运行任何测试或命令，「验证」仅为代码内的自述）"
        )

    def test_如实汇报未通过时不加系统事实行(self):
        """「测试未通过」是如实汇报，不是验证声明，不该被加事实行。"""
        results = [{"action": "test_run", "status": "failed", "artifacts": []}]
        answer = "已跑测试，测试未通过"
        assert conclude(final_answer=answer, execution_results=results) == answer

    def test_同句出现验证与通过即算声明(self):
        """实机（首次收敛那轮）：结论写成「验证证据：1) pytest 运行 tests/…，5 项全部通过」，
        「验证」与「通过」中间隔着路径和逗号 —— 只卡 6 字间距会漏判、事实行附不上。"""
        results = [{"action": "test_run", "status": "done", "artifacts": []}]
        answer = "验证证据：1) pytest 运行 tests/test_research_agent.py，5 项全部通过"
        assert conclude(final_answer=answer, execution_results=results) == (
            answer + "（系统核对：真实运行 1 次，成功 1 次）"
        )

    def test_有产物时不加事实说明(self):
        results = [{"status": "done", "artifacts": ["agents/research_agent.py"]}]
        assert conclude(final_answer="已交付代码", execution_results=results) == "已交付代码"

    def test_没传执行结果时不补事实说明(self):
        """无从判断是否产出 → 不补，避免冤枉纯问答类任务（老调用方行为不变）。"""
        assert conclude(final_answer="结论") == "结论"

    def test_结论引用的文件确实产出时不报假警报(self):
        """实测（用户任务第 4 次复现）：结论写「pytest 运行 paper_survey_agent/test_agent.py，
        7 项全部通过」，而这个文件**就在产物清单里**，却被告知「本任务未产出」。
        根因：产物名在**比对之前**就被截断（``…/test_agent.py``），全路径自然对不上。
        假警报比不说更伤信任 —— 逻辑必须用完整名，短化只发生在展示处。"""
        results = [
            {
                "action": "file_write",
                "status": "done",
                "artifacts": ["paper_survey_agent/test_agent.py"],
            }
        ]
        answer = "pytest 运行 paper_survey_agent/test_agent.py，7 项全部通过"

        out = conclude(final_answer=answer, execution_results=results)

        assert "未产出" not in out

    def test_兜底结论里的产物名保留文件名(self):
        """实测（质量探针 run-1）：兜底文案写成「产出 _sample_backup/quality_probe/…、
        _sample_backup/quality_probe/…」—— 同一目录下三个不同文件被截成同样的省略号，
        等于没说。路径短化必须保留**文件名**（有信息量的是尾部）。"""
        results = [
            {
                "action": "file_write",
                "status": "done",
                "artifacts": ["_sample_backup/quality_probe/research_agent.py"],
            }
        ]
        out = conclude(execution_results=results, status="executing", stopped=True)
        assert "research_agent.py" in out
        assert "_sample_backup/quality_probe/…" not in out


# ── 接线：分解器写 title / description ─────────────────────────


class TestDecomposerLabels:
    def test_步骤带人读标题与描述(self):
        d = Decomposer(correlation_id="c-test")
        result = d.decompose(
            "t-001",
            "写文件",
            raw_steps=[
                {"id": "s1", "action": "file_write", "inputs": {"path": "a.md", "content": "hi"}}
            ],
        )
        step = result.steps["s1"]
        assert step.title == "写入文件"
        assert "文件：a.md" in step.description
        # 也随 to_dict 出现在 /plan 响应里。
        assert result.to_dict()["steps"]["s1"]["title"] == "写入文件"


# ── 接线：执行结果带 summary ────────────────────────────────────


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


def _seed_plan(client: TestClient, task_id: str, steps: dict[str, Step]) -> None:
    """把指定步骤注入任务（先走一遍 /plan 满足 AWAITING_CONFIRM 前置状态）。"""
    client.post(f"/tasks/{task_id}/plan", json={"use_llm": False}, headers=_LOCAL_HEADERS)
    entry = get_store().get(task_id)
    order = list(steps.keys())
    plan = Plan(task_id=task_id, order=order, parallel_groups=[order], confirmed_by_user=False)
    plan.validate_steps(steps)
    entry.steps = steps
    entry.conductor.task_state.plan = plan


def _new_task(client: TestClient, requirement: str) -> str:
    resp = client.post("/tasks", json={"requirement": requirement}, headers=_LOCAL_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()["task_id"]


class TestExecutionSummaryWiring:
    def test_成功步骤的人话摘要(self, client: TestClient):
        tid = _new_task(client, "预览差异")
        _seed_plan(
            client,
            tid,
            {
                "step-001": Step(
                    id="step-001",
                    action="diff_preview",
                    inputs={"old_content": "a", "new_content": "b"},
                )
            },
        )
        row = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()[
            "execution_results"
        ][0]
        assert row["status"] == "done"
        assert row["summary"].startswith("预览变更完成（")  # 人话
        assert "diff_preview" not in row["summary"]  # 不再暴露英文 action
        assert "{" not in row["summary"]  # 不再倾倒 JSON
        assert row["result"] is not None  # 原始数据保留

    def test_失败步骤不再透传异常原文(self, client: TestClient):
        tid = _new_task(client, "执行不存在的工具")
        _seed_plan(
            client,
            tid,
            {"step-001": Step(id="step-001", action="no_such_action", inputs={})},
        )
        row = client.post(f"/tasks/{tid}/run", headers=_LOCAL_HEADERS).json()[
            "execution_results"
        ][0]
        assert row["status"] == "failed"
        assert row["summary"] == "no_such_action失败：该动作没有对应工具，无法执行"
        assert "RuntimeError" not in row["summary"]
        assert row["error"]  # 原始异常仍保留（供「查看原始数据」）
