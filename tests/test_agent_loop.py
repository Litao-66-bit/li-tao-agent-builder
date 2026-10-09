"""agentic 循环骨架测试：校验链、静态回退、预算终止、空转、审批挂起、步骤落型。

全部用 ``ScriptedDecider`` + 假 ``execute`` 回调驱动，不碰网络、不碰真实工具，
因此循环的每一步、每一次终止与回退都可精确断言。
"""

from __future__ import annotations

from dataclasses import asdict

from agent_builder.api.agent_loop import (
    MAX_LOOP_STEPS,
    MAX_LOOP_TOKENS,
    MAX_REPEAT_FAILURES,
    MAX_STAGNANT_ROUNDS,
    STEP_ID_PREFIX,
    STOP_BUDGET_STEPS,
    STOP_BUDGET_TOKENS,
    STOP_FINAL,
    STOP_INTERRUPTED,
    STOP_INVALID_DECISION,
    STOP_PENDING_APPROVAL,
    STOP_PROPOSED,
    STOP_STAGNANT,
    LoopBudget,
    LoopOutcome,
    StepOutcome,
    run_agent_loop,
    run_step,
    to_execution_results,
    to_step,
)
from agent_builder.api.artifacts import artifacts_of
from agent_builder.api.deciders import (
    KIND_AGENT,
    KIND_FINAL,
    KIND_PROPOSE,
    Decision,
    LoopContext,
    ProposalDraft,
    ScriptedDecider,
    validate_decision,
)
from agent_builder.api.role_catalog import RoleSpec, build_catalog
from agent_builder.contracts.schemas import Step
from agent_builder.narrate import describe_step

CLOSED = build_catalog(sub_arch=False)
OPENED = build_catalog(sub_arch=True)


def _context(*, allow_propose: bool = False, catalog=None) -> LoopContext:
    """构造决策上下文：默认按门控取目录（提议开启时用含副架构的目录）。"""
    if catalog is None:
        catalog = OPENED if allow_propose else CLOSED
    return LoopContext(
        requirement="把调研结论写成报告",
        catalog=catalog,
        allow_propose=allow_propose,
    )


def _done(summary: str = "已完成", artifacts: list[str] | None = None, tokens: int = 10):
    return StepOutcome(status="done", summary=summary, artifacts=artifacts or [], tokens=tokens)


def _search() -> Decision:
    return Decision(kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": "x"})


class TestToStep:
    """轮次落成 Step：必须带人读标题与描述，否则前端只能回退英文 action。"""

    def test_轮次步骤带人读标题与描述(self) -> None:
        step = to_step(_search(), round_index=1)
        assert step.id == f"{STEP_ID_PREFIX}-001"
        assert step.action == "web_search"  # 英文 action 只留在数据里
        assert step.title and step.title != step.action  # 展示用的是中文动作名
        assert step.description  # 输入已人性化（不是 key=value）
        assert "=" not in step.description  # ③ 类机器输入的形态

    def test_与固定工作流共用同一套叙述(self) -> None:
        """agentic 与固定工作流必须用同一个 describe_step，否则两条路的人话会漂移。"""
        expected_title, expected_desc = describe_step("web_search", {"query": "x"})
        step = to_step(_search(), round_index=2)
        assert (step.title, step.description) == (expected_title, expected_desc)

    def test_决策落成步骤并写assignee(self) -> None:
        step = to_step(_search(), round_index=3)
        assert step.id == f"{STEP_ID_PREFIX}-003"
        assert step.action == "web_search"
        assert step.assignee == "searcher"
        assert step.inputs == {"query": "x"}


class TestArtifacts:
    """产物提取 —— 前端「查看产物」入口的唯一依据（空列表 = 不显示按钮）。"""

    def test_只读动作的inputs_path不算产物(self) -> None:
        """「列出文件（文件：.）」的 path 是目录/被访问对象，绝不能当产物。"""
        step = Step(id="loop-001", action="file_list", inputs={"path": "."})
        assert artifacts_of(step, "列出了 tests 下的文件") == []

    def test_写动作可用inputs_path兜底(self) -> None:
        step = Step(id="loop-002", action="file_write", inputs={"path": "a.md"})
        assert artifacts_of(step, "wrote a.md (3 chars)") == ["a.md"]

    def test_结构化结果优先取files_changed(self) -> None:
        step = Step(id="loop-003", action="code_gen", inputs={"path": "b.md"})
        assert artifacts_of(step, {"files_changed": ["x.md", "y.md"]}) == ["x.md", "y.md"]

    def test_轮次结果带出产物字段(self) -> None:
        outcome = _run([_search(), _finish()], execute=lambda step: _done(artifacts=["a.md"]))
        rows = to_execution_results(outcome)
        assert rows[0]["artifacts"] == ["a.md"]

    def test_只读动作经角色派发后不算产物(self) -> None:
        """回归：agentic + 角色派发时 ``result`` 是角色结果（dict，含 files_changed），
        只读动作**同样不能**算产物 —— 否则前端给「列出文件」渲染「查看产物」，
        点开必然报「路径不是文件: tests」。

        此前 ``artifacts_of`` 的单测只覆盖「纯工具」路径（``result`` 是 str），漏了这条。
        """
        from agent_builder.roles.code_worker import CodeWorker

        step = Step(id="loop-001", action="file_list", inputs={"path": "tests"})
        outcome = CodeWorker(correlation_id="c-test").execute(
            step, executor_fn=lambda s: "列出了 tests 下的文件"
        )
        assert outcome.files_changed == []  # 角色自己就不该报"产出了 tests"
        assert artifacts_of(step, asdict(outcome)) == []

    def test_写动作经角色派发后仍是产物(self) -> None:
        """反向守住：写动作的产物不能被上面的收口一起干掉。"""
        from agent_builder.roles.code_worker import CodeWorker

        step = Step(id="loop-002", action="file_write", inputs={"path": "a.md"})
        outcome = CodeWorker(correlation_id="c-test").execute(step, executor_fn=lambda s: "ok")
        assert artifacts_of(step, asdict(outcome)) == ["a.md"]

    def test_没产出时产物为空(self) -> None:
        outcome = _run([_search(), _finish()])
        assert to_execution_results(outcome)[0]["artifacts"] == []


def _finish() -> Decision:
    return Decision(kind=KIND_FINAL, thought="已完成")


def _draft(name: str = "data_cleaner") -> ProposalDraft:
    """合法扩编草案：名字未占用、承接的 file_read 是已注册工具。"""
    return ProposalDraft(
        target="role",
        name=name,
        mission="清洗原始数据并输出质量报告",
        accepts=("file_read",),
        risk="low",
        rationale="现有 data_analyst 只做只读查询，不会清洗落盘",
    )


def _propose(name: str = "data_cleaner") -> Decision:
    return Decision(kind=KIND_PROPOSE, thought="需要清洗能力", proposal=_draft(name))


def _run(script, *, execute=None, budget=None, context=None, allow_propose=False) -> LoopOutcome:
    return run_agent_loop(
        context=context if context is not None else _context(allow_propose=allow_propose),
        decider=ScriptedDecider(script=script),
        execute=execute if execute is not None else (lambda step: _done()),
        budget=budget,
    )


class TestValidateDecision:
    def test_合法决策通过(self) -> None:
        assert validate_decision(_search(), catalog=CLOSED) is None

    def test_未知决策类型被拒(self) -> None:
        problem = validate_decision(Decision(kind="teleport"), catalog=CLOSED)
        assert problem is not None and "未知决策类型" in problem

    def test_流程控制角色不在目录内(self) -> None:
        decision = Decision(kind=KIND_AGENT, role="conductor", action="web_search")
        problem = validate_decision(decision, catalog=CLOSED)
        assert problem is not None and "conductor" in problem

    def test_角色内行为不必是已注册工具(self) -> None:
        """``summarize`` 不是注册工具，但属 summarizer 的角色内行为 → 放行。"""
        decision = Decision(kind=KIND_AGENT, role="summarizer", action="summarize")
        assert validate_decision(decision, catalog=CLOSED) is None

    def test_角色不接受该动作被拒(self) -> None:
        decision = Decision(kind=KIND_AGENT, role="searcher", action="file_write")
        problem = validate_decision(decision, catalog=CLOSED)
        assert problem is not None and "不接受动作" in problem

    def test_目录与权限矩阵不一致时被兜底拦下(self) -> None:
        """防御性兜底：若目录把「未授权工具」挂到某角色上，校验阶段就拦下。

        真实目录不会出现这种组合 —— 由 ``tests/test_role_catalog.py`` 的
        ``test_已注册工具一律不越权`` 守住；这里只验证兜底分支本身有效。
        """
        fake = (
            RoleSpec(
                name="searcher",
                architecture="main",
                mission="（测试用假角色）",
                accepts=frozenset({"git_commit"}),
                high_risk_tools=frozenset(),
            ),
        )
        decision = Decision(kind=KIND_AGENT, role="searcher", action="git_commit")
        problem = validate_decision(decision, catalog=fake)
        assert problem is not None and "未被授权" in problem

    def test_副结构未开启时禁止提议(self) -> None:
        problem = validate_decision(Decision(kind=KIND_PROPOSE), catalog=CLOSED)
        assert problem is not None and "副结构未开启" in problem

    def test_副结构开启时允许提议(self) -> None:
        assert (
            validate_decision(_propose(), catalog=OPENED, allow_propose=True) is None
        )

    def test_提议不得编造角色名或动作(self) -> None:
        cases = {
            "已存在角色": _draft("searcher"),
            "名字不合规": _draft("Data Cleaner"),
            "动作不存在": ProposalDraft(
                target="role", name="data_cleaner", mission="x", accepts=("no_such_action",)
            ),
            "缺用途": ProposalDraft(target="role", name="data_cleaner", accepts=("file_read",)),
            "缺受理动作": ProposalDraft(target="role", name="data_cleaner", mission="x"),
            "不支持的工具类": ProposalDraft(
                target="tool", name="data_cleaner", mission="x", accepts=("file_read",)
            ),
        }
        for label, draft in cases.items():
            decision = Decision(kind=KIND_PROPOSE, proposal=draft)
            problem = validate_decision(decision, catalog=OPENED, allow_propose=True)
            assert problem is not None, label


class TestLoopHappyPath:
    def test_决策到完成(self) -> None:
        outcome = _run([_search(), _finish()])

        assert outcome.stopped_reason == STOP_FINAL
        assert outcome.decided_by == "scripted"
        assert [r.round_index for r in outcome.rounds] == [1]
        assert outcome.rounds[0].decision.role == "searcher"
        assert outcome.fallback_reasons == []
        assert outcome.pending_decision is None
        # 收敛那轮的 thought 必须落进 final_answer（「结论区」的唯一来源）——
        # 以前它在收敛时被直接丢弃，用户跑完只看得到工具卡。
        assert outcome.final_answer == "已完成"

    def test_结论优先用answer而不是决策理由(self) -> None:
        """回归：结论区此前把模型决策理由（"可以收尾"这类内心独白）端给了用户。"""
        outcome = _run(
            [
                _search(),
                Decision(
                    kind=KIND_FINAL,
                    thought="够了，可以收尾",
                    answer="本次只查了 1 处引用，未发现冲突",
                ),
            ]
        )
        assert outcome.stopped_reason == STOP_FINAL
        assert outcome.final_answer == "本次只查了 1 处引用，未发现冲突"

    def test_非收敛终止时不产生结论(self) -> None:
        """没走到 final（预算耗尽 / 挂起 / 非法）→ 不编造结论。"""
        outcome = _run([_search(), _search(), _search()], budget=LoopBudget(max_steps=1))
        assert outcome.stopped_reason == STOP_BUDGET_STEPS
        assert outcome.final_answer == ""

    def test_历史与产物累积(self) -> None:
        context = _context()
        _run(
            [Decision(kind=KIND_AGENT, role="code_worker", action="file_write"), _finish()],
            execute=lambda step: _done("已写入 a.md", artifacts=["a.md"]),
            context=context,
        )
        assert context.history[0]["role"] == "code_worker"
        assert context.history[0]["status"] == "done"
        assert context.artifacts == ["a.md"]


class TestLoopTermination:
    def test_步数预算耗尽(self) -> None:
        budget = LoopBudget(max_steps=2)
        outcome = _run([_search(), _search(), _search(), _finish()], budget=budget)

        assert outcome.stopped_reason == STOP_BUDGET_STEPS
        assert len(outcome.rounds) == 2
        assert budget.steps_used == 2

    def test_token预算耗尽(self) -> None:
        budget = LoopBudget(max_tokens=15)
        outcome = _run([_search(), _search(), _search(), _finish()], budget=budget)

        assert outcome.stopped_reason == STOP_BUDGET_TOKENS
        assert len(outcome.rounds) == 2
        assert budget.tokens_used == 20

    def test_连续无进展判空转(self) -> None:
        """空转只统计**重复症状**：首见症状算"世界变了"（= 进展），所以要撞满
        ``MAX_STAGNANT_ROUNDS`` 次重复症状才会停下 —— 即总轮数 = 1 + MAX_STAGNANT_ROUNDS。

        每轮用**不同参数**（否则先被"同动作同参数反复失败"的重复闸门按 2 次停下，
        测的就不是空转计数器了）。
        """

        def _failed(step):
            return StepOutcome(status="failed", summary="网络超时", tokens=0)

        def _query(text: str) -> Decision:
            return Decision(kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": text})

        budget = LoopBudget()
        outcome = _run(
            [_query("a"), _query("b"), _query("c"), _query("d"), _finish()],
            execute=_failed,
            budget=budget,
        )

        assert outcome.stopped_reason == STOP_STAGNANT
        assert len(outcome.rounds) == MAX_STAGNANT_ROUNDS + 1
        assert budget.stagnant_rounds == MAX_STAGNANT_ROUNDS

    def test_有产物即算进展并清零空转计数(self) -> None:
        calls = {"n": 0}

        def _execute(step):
            calls["n"] += 1
            if calls["n"] == 1:
                return StepOutcome(status="failed", summary="失败", tokens=0)
            return _done(artifacts=["b.md"])

        outcome = _run([_search(), _search(), _search(), _finish()], execute=_execute)
        # 第 1 轮无进展（计数 1），第 2 轮有产物 → 清零；因此不会触发空转终止，
        # 一直跑到脚本给出 final（共 3 轮）。
        assert outcome.stopped_reason == STOP_FINAL
        assert len(outcome.rounds) == 3

    def test_默认预算常量(self) -> None:
        budget = LoopBudget()
        assert budget.max_steps == MAX_LOOP_STEPS
        assert budget.max_tokens == MAX_LOOP_TOKENS
        assert budget.max_stagnant_rounds == MAX_STAGNANT_ROUNDS

    def test_步数预算够走完写代码_运行_排错(self) -> None:
        """回归：12 步会在「已建包 + 已写实现 + 已写测试 + 跑挂两次 + 正按报错对齐接口」
        时被硬切（差一步收尾）。agentic 不预分解 DAG，这条主链路得自己走完。"""
        assert MAX_LOOP_STEPS >= 20


class TestLoopInterrupt:
    """用户中断：``should_stop`` 回调让**同步阻塞**的循环在轮次边界退出。

    ``/run``、``/resume`` 跑完才返回，用户点「⏸ 中断」时状态机已把任务翻成
    ``interrupted`` —— 循环据此在下一个轮次边界停下，不再决策 / 不再执行工具。
    """

    def test_中断后不再执行后续工具(self) -> None:
        executed: list[str] = []

        def _execute(step):
            executed.append(step.action)
            return _done()

        checks = {"n": 0}

        def _should_stop() -> bool:
            # 第 1 次检查放行（跑完第 1 轮），第 2 次检查（下一轮开头）返回 True。
            checks["n"] += 1
            return checks["n"] > 1

        outcome = run_agent_loop(
            context=_context(),
            decider=ScriptedDecider(script=[_search(), _search(), _finish()]),
            execute=_execute,
            should_stop=_should_stop,
        )

        assert outcome.stopped_reason == STOP_INTERRUPTED
        assert len(outcome.rounds) == 1       # 只跑完第 1 轮
        assert executed == ["web_search"]     # 第 2 轮的工具没有执行
        assert outcome.pending_decision is None

    def test_一开始就被中断则一轮都不跑(self) -> None:
        outcome = run_agent_loop(
            context=_context(),
            decider=ScriptedDecider(script=[_finish()]),
            execute=lambda step: _done(),
            should_stop=lambda: True,
        )

        assert outcome.stopped_reason == STOP_INTERRUPTED
        assert outcome.rounds == []

    def test_不传回调时行为不变(self) -> None:
        outcome = _run([_search(), _finish()])
        assert outcome.stopped_reason == STOP_FINAL


class TestObservationFeedback:
    """工具原始返回必须回灌进循环上下文（模型不能只看到一句空摘要）。"""

    def test_观察结果进轮次与历史(self) -> None:
        def _execute(step):
            return StepOutcome(
                status="done",
                summary="列出文件完成",
                tokens=1,
                observation="[FILE] hello.py (3 bytes)",
            )

        context = _context()
        outcome = run_agent_loop(
            context=context,
            decider=ScriptedDecider(script=[_search(), _finish()]),
            execute=_execute,
        )

        assert outcome.rounds[0].observation == "[FILE] hello.py (3 bytes)"
        assert context.history[0]["observation"] == "[FILE] hello.py (3 bytes)"

    def test_超长观察结果被截断且写明原文长度(self) -> None:
        """裁剪必须写明原文长度：只说"已截断"时模型不知道漏了多少，会反复重读。"""

        def _execute(step):
            return StepOutcome(status="done", summary="读取文件完成", observation="x" * 5000)

        context = _context()
        run_agent_loop(
            context=context,
            decider=ScriptedDecider(script=[_search(), _finish()]),
            execute=_execute,
        )

        observation = context.history[0]["observation"]
        assert observation.startswith("x" * 2000)
        # 说明里必须给**可操作信息**：看的是哪几行、还剩多少行、下次一段读多大。
        # 实测（用户任务复现 run-2）：只给「已截断」时，模型直接要 start_line=65/end_line=306
        # （241 行 / 8.4k 字），再次超窗被截断，又陷入读不全的死循环。
        assert "原文 5000 字 / 共 1 行" in observation
        assert "此处只显示前 2000 字" in observation
        assert "start_line/end_line" in observation
        assert "一次不超过 50 行" in observation

    def test_超长代码观察附全文符号轮廓(self) -> None:
        """实测（用户任务 cea128b8）：模型连续 4 次读同一个 6.6k 字文件，思考里明写
        「需要看到 ResearchAgent.__init__ 的真实签名」—— 那段在 2000 字窗口之外，
        于是它只能重读 → 被截断 → 再重读，8 步零产物 stagnant。截断时必须附
        **全文符号轮廓**（行号 + 定义），模型才能用 start_line/end_line 精确取用。"""
        code = (
            "import os\n"
            + ("# filler\n" * 400)
            + "class ResearchAgent:\n"
            + "    def __init__(self, kb):\n"
            + "        self.kb = kb\n"
            + "    def build_report(self):\n"
            + "        return self.kb\n"
        )
        assert len(code) > 2000  # 符号确实落在窗口之外

        def _execute(step):
            return StepOutcome(status="done", summary="读取文件完成", observation=code)

        context = _context()
        run_agent_loop(
            context=context,
            decider=ScriptedDecider(script=[_search(), _finish()]),
            execute=_execute,
        )

        observation = context.history[0]["observation"]
        assert "…（原文" in observation  # 仍然写明原文长度
        assert "全文符号轮廓" in observation  # 附加了轮廓
        assert "class ResearchAgent:" in observation
        assert "def build_report" in observation

    def test_失败时把工具输出带进观察结果(self) -> None:
        """失败也要让模型看见真实输出（sandbox_run 会把 stdout/stderr 带在错误里）。"""
        from agent_builder.contracts.errors import AgentError

        def _boom(step):
            raise AgentError(
                "E_TOOL",
                "sandbox_run: 命令退出码 1\n'python' is not recognized",
                source="tool.sandbox_run",
            )

        outcome = run_step(to_step(_search(), round_index=1), _boom)

        assert outcome.status == "failed"
        assert outcome.observation == "sandbox_run: 命令退出码 1\n'python' is not recognized"

    def test_没有观察结果时历史不带空键(self) -> None:
        context = _context()
        run_agent_loop(
            context=context,
            decider=ScriptedDecider(script=[_search(), _finish()]),
            execute=lambda step: _done(),
        )
        assert context.history[0]["observation"] == ""


class TestStallNudge:
    """连续只读 + 存在未解决的失败 → **系统纠正**（不靠模型自觉）。

    实测（用户任务 a20291c1）：模型第 2 步拿到测试失败，接着连读 6 轮、思考里已把根因说得
    完全正确，却始终不动手；第 9 步又跑同一个 test_run → 重复失败闸门掐停，9 步 0 产物。
    """

    def test_连读三轮且有未解决失败时给出纠正(self) -> None:
        seen: list[str] = []

        class _RecordingDecider:
            name = "recording"

            def decide(self, context):
                seen.append(context.stall_note)
                if len(seen) == 1:  # 第 1 轮：验证既有实现（失败）
                    return Decision(
                        kind=KIND_AGENT,
                        role="test_runner",
                        action="test_run",
                        inputs={"target": "t.py"},
                    )
                if len(seen) <= 4:  # 第 2-4 轮：只读（**换不同文件**，否则会撞"紧接着重复同一动作"闸门）
                    return Decision(
                        kind=KIND_AGENT,
                        role="code_worker",
                        action="file_read",
                        inputs={"path": f"src{len(seen)}.py"},
                    )
                return _finish()

        def _execute(step):
            if step.action == "test_run":
                return StepOutcome(
                    status="failed",
                    summary="测试未通过：失败 1 项",
                    error="TypeError: rank_papers() got an unexpected keyword argument 'topic'",
                    tokens=0,
                )
            return StepOutcome(status="done", summary="读取文件完成", observation="x", tokens=0)

        run_agent_loop(context=_context(), decider=_RecordingDecider(), execute=_execute)

        # 只读轮数不足时不打扰（第 1-4 次决策分别对应 0/1/2/3 轮已完成）
        assert seen[0] == ""
        assert seen[1] == ""
        assert seen[2] == ""
        # 连续第 3 轮只读之后（第 5 次决策）必须给出硬性纠正
        assert "连续 3 轮只做只读动作" in seen[4]
        assert "file_edit" in seen[4]
        assert "不要" in seen[4]

    def test_没有失败时不纠正(self) -> None:
        """只读本身不是问题（查代码是正常动作）—— 只有"验证失败着"才是空转。"""
        seen: list[str] = []

        class _RecordingDecider:
            name = "recording"

            def decide(self, context):
                seen.append(context.stall_note)
                if len(seen) <= 4:
                    return Decision(
                        kind=KIND_AGENT,
                        role="code_worker",
                        action="file_read",
                        inputs={"path": f"src{len(seen)}.py"},
                    )
                return _finish()

        def _execute(step):
            return StepOutcome(status="done", summary="读取文件完成", observation="x", tokens=0)

        run_agent_loop(context=_context(), decider=_RecordingDecider(), execute=_execute)

        assert all(note == "" for note in seen)


class TestStagnationCountsNewSymptom:
    """实测（用户任务 02bd9686）：R6 成功改掉一个 TypeError → R9 跑出**新的** NameError
    （它自己引入的）→ R10 想补 import 但 old_string 不匹配。若把这两轮都算"无产出"，
    整轮会在第 10 步被空转闸门掐死，而预算还剩 10 步。**症状变了 = 世界变了 = 进展。**
    """

    def test_症状变化时不算空转(self) -> None:
        outcomes = [
            StepOutcome(status="failed", summary="失败 1 项", error="TypeError: asdict", tokens=0),
            StepOutcome(status="failed", summary="失败 1 项", error="NameError: is_dataclass", tokens=0),
        ]
        calls = {"n": 0}

        def _execute(step):
            idx = calls["n"]
            calls["n"] += 1
            if idx < len(outcomes):
                return outcomes[idx]
            return StepOutcome(status="done", summary="ok", tokens=0)

        outcome = _run(
            [
                Decision(
                    kind=KIND_AGENT,
                    role="test_runner",
                    action="test_run",
                    inputs={"target": "a.py"},
                ),
                Decision(
                    kind=KIND_AGENT,
                    role="test_runner",
                    action="test_run",
                    inputs={"target": "b.py"},
                ),
                _finish(),
            ],
            execute=_execute,
        )

        assert outcome.stopped_reason == STOP_FINAL

    def test_症状不变仍然算空转(self) -> None:
        """反证：同样的失败反复撞，还是要停下（否则就是无限重试）。

        首次出现某症状算进展（世界变了），所以同症状连撞**三次**才会把计数推满 2。
        """
        calls = {"n": 0}

        def _execute(step):
            calls["n"] += 1
            return StepOutcome(status="failed", summary="失败 1 项", error="TypeError: same", tokens=0)

        outcome = _run(
            [
                Decision(
                    kind=KIND_AGENT,
                    role="test_runner",
                    action="test_run",
                    inputs={"target": "a.py"},
                ),
                Decision(
                    kind=KIND_AGENT,
                    role="test_runner",
                    action="test_run",
                    inputs={"target": "b.py"},
                ),
                Decision(
                    kind=KIND_AGENT,
                    role="test_runner",
                    action="test_run",
                    inputs={"target": "c.py"},
                ),
                _finish(),
            ],
            execute=_execute,
        )

        assert outcome.stopped_reason == STOP_STAGNANT


class TestRepeatFailureStops:
    """同一个「动作 + 参数」反复失败 → 判空转停下。

    只用"连续无进展"不够：实测模型在一个失败的 ``file_read`` 之间夹了一次成功的
    ``file_list``，空转计数被清零 —— 同一路径连撞 5 次，直到预算耗尽也没换做法。
    """

    def test_同一动作同一参数反复失败即停(self) -> None:
        def _execute(step):
            return StepOutcome(status="failed", summary="读取文件失败：目标不存在", tokens=0)

        outcome = _run([_search(), _search(), _search(), _finish()], execute=_execute)

        assert outcome.stopped_reason == STOP_STAGNANT
        assert len(outcome.rounds) == MAX_REPEAT_FAILURES

    def test_失败症状变了就不算重复失败(self) -> None:
        """实测（质量探针 run-1）：R3 的 ``test_run`` 因 ``ImportError``（通过 0 项）失败 →
        模型据此重写了测试文件 → R7 对**同一 target** 再跑，症状变成「通过 2 项、失败 4 项」。
        明明在收敛，旧口径却把"同动作同参数又失败一次"直接判 ``stagnant`` 掐死。症状变了
        说明世界变了，必须给它下一轮。"""
        symptoms = [
            "测试未通过：失败 0 项、错误 1 项（通过 0 项）",
            None,  # 中间一次成功（有产物 → 空转清零）
            "测试未通过：失败 4 项、错误 1 项（通过 2 项）",
        ]
        calls = {"n": 0}

        def _execute(step):
            idx = calls["n"]
            calls["n"] += 1
            reason = symptoms[idx] if idx < len(symptoms) else None
            if reason is None:
                return _done(artifacts=["notes.md"])
            return StepOutcome(
                status="failed", summary=f"运行测试失败：{reason}", error=reason, tokens=0
            )

        outcome = _run([_search(), _search(), _search(), _finish()], execute=_execute)

        assert outcome.stopped_reason == STOP_FINAL

    def test_同头不同尾的失败不算重复(self) -> None:
        """实测（用户任务复现 run-2）：``test_run`` 的失败原文**开头永远是**
        「测试未通过：失败 6 项、错误 0 项（通过 0 项）；<一长串测试名>」，
        真正区分病因的异常信息在**尾部**（``Paper(year=...)`` vs ``no attribute 'survey'``）。
        指纹只取开头时，两种完全不同的病因被判成同一症状 → 误杀。"""
        head = "测试未通过：失败 6 项、错误 0 项（通过 0 项）；" + "；".join(
            f"tests/test_x.py::test_case_{i}" for i in range(12)
        )
        symptoms = [
            head + "；TypeError: Paper.__init__() got an unexpected keyword argument 'year'",
            None,  # 中间一次成功（有产物 → 空转清零）
            head + "；AttributeError: 'PaperSurveyAgent' object has no attribute 'survey'",
        ]
        calls = {"n": 0}

        def _execute(step):
            idx = calls["n"]
            calls["n"] += 1
            reason = symptoms[idx] if idx < len(symptoms) else None
            if reason is None:
                return _done(artifacts=["notes.md"])
            return StepOutcome(status="failed", summary=reason[:60], error=reason, tokens=0)

        outcome = _run([_search(), _search(), _search(), _finish()], execute=_execute)

        assert outcome.stopped_reason == STOP_FINAL

    def test_参数不同就不算重复(self) -> None:
        """只有「同一动作 + 同一参数」才累计；换过参数不算重复。"""

        def _execute(step):
            if step.action == "web_search":
                return StepOutcome(status="failed", summary="搜不到", tokens=0)
            return _done(artifacts=["notes.md"])  # 有产物 → 连续空转清零

        script = [
            Decision(kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": "a"}),
            Decision(
                kind=KIND_AGENT, role="code_worker", action="file_list", inputs={"path": "."}
            ),
            Decision(kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": "b"}),
            Decision(
                kind=KIND_AGENT, role="code_worker", action="file_list", inputs={"path": "."}
            ),
            _finish(),
        ]
        outcome = _run(script, execute=_execute)

        assert outcome.stopped_reason == STOP_FINAL
        assert len(outcome.rounds) == 4

    def test_中途成功也拦不住同一动作的第N次失败(self) -> None:
        """夹一次成功的动作不会把"重复失败"洗白（这正是实测踩到的坑）。"""

        def _execute(step):
            if step.action == "web_search":
                return StepOutcome(status="failed", summary="读取文件失败", tokens=0)
            return _done()

        script = [
            _search(),
            Decision(
                kind=KIND_AGENT, role="code_worker", action="file_list", inputs={"path": "."}
            ),
            _search(),
            _finish(),
        ]
        outcome = _run(script, execute=_execute)

        assert outcome.stopped_reason == STOP_STAGNANT
        assert len(outcome.rounds) == 3


class TestRepeatSuccessStagnation:
    """紧接着重复「同一动作 + 同一参数」的**成功**动作 → 不算进展。

    光看"成功"不够（``progressed = status == "done"``）：实测（run-6）模型连续 5 次
    ``file_read`` 同一个 4.5k 的 ``agent.py``，每次都算成功 → 空转计数被反复清零、
    空转检测失效，14 步里 6 步白烧在重读上；它早就诊断出「实现与测试签名不匹配」，
    却没有轮次去改。重复取回**同一份**东西没有带来新信息。
    """

    def test_同一动作同一参数连续成功不算进展(self) -> None:
        outcome = _run([_search(), _search(), _search(), _finish()], execute=lambda step: _done())

        assert outcome.stopped_reason == STOP_STAGNANT
        assert len(outcome.rounds) == MAX_STAGNANT_ROUNDS + 1

    def test_参数不同不算重复(self) -> None:
        """换过参数就不算重复 —— 交替读两个文件不该被判成空转。"""
        script = [
            Decision(kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": "a"}),
            Decision(kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": "b"}),
            Decision(kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": "a"}),
            Decision(kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": "b"}),
            _finish(),
        ]
        outcome = _run(script, execute=lambda step: _done())

        assert outcome.stopped_reason == STOP_FINAL
        assert len(outcome.rounds) == 4

    def test_重复动作但产出新产物仍算进展(self) -> None:
        """反复重写同一个文件仍是在干活（有产物），不该被当成空转停下。"""
        outcome = _run(
            [_search(), _search(), _search(), _finish()],
            execute=lambda step: _done(artifacts=["agents/x.py"]),
        )

        assert outcome.stopped_reason == STOP_FINAL
        assert len(outcome.rounds) == 3


class TestLoopApprovalGate:
    def test_待放行时停下并保留该步(self) -> None:
        decision = Decision(kind=KIND_AGENT, role="code_worker", action="file_write")

        def _execute(step):
            if step.action == "file_write":
                return StepOutcome(status="pending_approval", summary="等待你放行：写入文件")
            return _done()

        outcome = _run([_search(), decision, _finish()], execute=_execute)

        assert outcome.stopped_reason == STOP_PENDING_APPROVAL
        assert outcome.pending_decision is not None
        assert outcome.pending_decision.action == "file_write"
        assert len(outcome.rounds) == 2

    def test_挂起轮不计入预算与空转(self) -> None:
        """回归：审批门拦下的那轮既不算一步也不算空转。

        否则「失败一轮 → 挂起 → 放行后一次失败」会被凑成两轮无进展，
        明明只失败一次却判 stagnant 停下（实机复现）。
        注：空转只统计**重复症状**（首见症状算"世界变了"，见 TestStagnationCountsNewSymptom），
        所以这里用两次同症状失败凑出计数 1，再用挂起轮验证它不会被推成 2。
        """
        decision = Decision(kind=KIND_AGENT, role="code_worker", action="file_write")

        def _execute(step):
            if step.action == "file_write":
                return StepOutcome(status="pending_approval", summary="等待你放行：写入文件")
            return StepOutcome(status="failed", summary="网络超时", error="TimeoutError: 网络超时", tokens=0)

        budget = LoopBudget()
        outcome = _run(
            [
                _search(),
                Decision(
                    kind=KIND_AGENT, role="searcher", action="web_search", inputs={"query": "b"}
                ),
                decision,
                _finish(),
            ],
            execute=_execute,
            budget=budget,
        )

        assert outcome.stopped_reason == STOP_PENDING_APPROVAL
        assert len(outcome.rounds) == 3
        # 两次同症状失败 → 1（首次算进展）；挂起那轮不计 —— 若计入就是 2，会误触发终止。
        assert budget.stagnant_rounds == 1
        assert budget.steps_used == 2


class TestLoopFallback:
    def test_非法角色回退静态映射(self) -> None:
        bad = Decision(kind=KIND_AGENT, role="nobody", action="web_search", inputs={"query": "x"})
        outcome = _run([bad, _finish()])

        assert outcome.stopped_reason == STOP_FINAL
        assert outcome.decided_by == "static"
        assert outcome.rounds[0].decision.role == "searcher"
        assert outcome.rounds[0].decision.inputs == {"query": "x"}
        assert outcome.fallback_reasons and "静态映射回退" in outcome.fallback_reasons[0]

    def test_无法回退时判非法决策且不执行(self) -> None:
        bad = Decision(kind=KIND_AGENT, role="nobody", action="no_such_action")
        executed: list[str] = []
        outcome = _run([bad, _finish()], execute=lambda step: executed.append(step.id) or _done())

        assert outcome.stopped_reason == STOP_INVALID_DECISION
        assert outcome.rounds == []
        assert executed == []
        assert outcome.fallback_reasons

    def test_副结构未开启时的提议被拒并终止(self) -> None:
        outcome = _run([Decision(kind=KIND_PROPOSE, thought="想要一个新角色")])
        assert outcome.stopped_reason == STOP_INVALID_DECISION
        assert "副结构未开启" in outcome.fallback_reasons[0]

    def test_开启时缺少草案字段仍被拒(self) -> None:
        """开启门控只说明"允许提议"，提议本身必须带合法草案（不得空口提议）。"""
        outcome = _run([Decision(kind=KIND_PROPOSE, thought="想要一个新角色")], allow_propose=True)
        assert outcome.stopped_reason == STOP_INVALID_DECISION
        assert "proposal" in outcome.fallback_reasons[0]

    def test_副结构开启时的提议浮出水面而不被执行(self) -> None:
        executed: list[str] = []
        outcome = _run(
            [_propose()],
            execute=lambda step: executed.append(step.id) or _done(),
            allow_propose=True,
        )

        assert outcome.stopped_reason == STOP_PROPOSED
        assert outcome.proposal is not None
        assert outcome.proposal.proposal is not None
        assert outcome.proposal.proposal.name == "data_cleaner"
        assert outcome.rounds == []
        assert executed == []
