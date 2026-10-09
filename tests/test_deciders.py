"""决策器内部测试：提示词模板、JSON 归一化（容错 L2）、LLM 决策器与重试（L4）。

> 校验链 / 静态回退（L3 / L5）的测试在 ``test_agent_loop.py``（循环视角）；
> 本文件聚焦决策器自身：提示词渲染、容错归一化、重试与"绝不伪装成 final"。

全部注入假 LLM 客户端，不碰网络。
"""

from __future__ import annotations

from typing import Any

from agent_builder.api.agent_loop import (
    STOP_FINAL,
    STOP_INVALID_DECISION,
    StepOutcome,
    run_agent_loop,
)
from agent_builder.api.deciders import (
    DECIDE_SCHEMA_HINT,
    DEFAULT_HISTORY_WINDOW,
    KIND_AGENT,
    KIND_FINAL,
    KIND_INVALID,
    LLMRouteDecider,
    LoopContext,
    build_decision_prompt,
    parse_decision,
)
from agent_builder.api.role_catalog import RoleSpec, build_catalog

CLOSED = build_catalog(sub_arch=False)
OPENED = build_catalog(sub_arch=True)


class _FakeLLM:
    """假 LLM 客户端：按脚本返回 payload，并记录收到的提示词。"""

    def __init__(self, payloads: list[Any] | None = None, *, raises: Exception | None = None):
        self.payloads = list(payloads or [])
        self.prompts: list[str] = []
        self.raises = raises

    def complete_json(self, prompt: str, schema_hint: str = "") -> Any:
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        if not self.payloads:
            return {}
        return self.payloads.pop(0)


def _context(**kwargs) -> LoopContext:
    return LoopContext(requirement="把调研结论写成报告", catalog=kwargs.pop("catalog", CLOSED), **kwargs)


class TestParseDecision:
    def test_标准嵌套结构(self) -> None:
        payload = {
            "thought": "先检索",
            "next": {
                "kind": "agent",
                "role": "searcher",
                "action": "web_search",
                "inputs": {"query": "x"},
                "reason": "需要外部资料",
            },
        }
        decision = parse_decision(payload)
        assert decision.kind == KIND_AGENT
        assert decision.role == "searcher"
        assert decision.action == "web_search"
        assert decision.inputs == {"query": "x"}
        assert decision.thought == "先检索"
        assert decision.reason == "需要外部资料"

    def test_读answer作为结论(self) -> None:
        """``answer`` 是给用户看的结论，与 ``thought``（决策理由）分开存放。"""
        decision = parse_decision(
            {
                "thought": "已经够了，可以收尾",
                "next": {"kind": "final", "answer": "本次改动只涉及 3 个文件"},
            }
        )
        assert decision.answer == "本次改动只涉及 3 个文件"
        assert decision.thought == "已经够了，可以收尾"

    def test_final_answer别名也被接受(self) -> None:
        decision = parse_decision({"kind": "final", "next": {"final_answer": "结论"}})
        assert decision.answer == "结论"

    def test_没有answer时不拿thought冒充结论(self) -> None:
        """解析层不替模型猜语义：是否回退由循环决定，免得独白被当成结论。"""
        decision = parse_decision({"kind": "final", "thought": "可以收尾了"})
        assert decision.answer == ""
        assert decision.thought == "可以收尾了"

    def test_容忍扁平写法(self) -> None:
        """没有 next、字段直接放在顶层（模型常见偏差）。"""
        decision = parse_decision(
            {"thought": "t", "kind": "agent", "role": "searcher", "action": "web_search"}
        )
        assert decision.kind == KIND_AGENT
        assert decision.role == "searcher"

    def test_next不是对象时退回顶层(self) -> None:
        decision = parse_decision({"next": "web_search", "role": "searcher", "action": "web_search"})
        assert decision.action == "web_search"

    def test_只有role与action时可推断为agent(self) -> None:
        decision = parse_decision({"role": "searcher", "action": "web_search"})
        assert decision.kind == KIND_AGENT

    def test_只有role没有action时不猜(self) -> None:
        decision = parse_decision({"role": "searcher"})
        assert decision.kind == KIND_INVALID
        assert decision.thought

    def test_空字典判invalid且不伪装成final(self) -> None:
        decision = parse_decision({})
        assert decision.kind == KIND_INVALID
        assert decision.kind != KIND_FINAL

    def test_非字典判invalid(self) -> None:
        assert parse_decision("").kind == KIND_INVALID
        assert parse_decision([1, 2]).kind == KIND_INVALID

    def test_inputs非字典记空(self) -> None:
        decision = parse_decision(
            {"kind": "agent", "role": "searcher", "action": "web_search", "inputs": "bad"}
        )
        assert decision.inputs == {}

    def test_kind大小写与空白归一化(self) -> None:
        decision = parse_decision({"kind": "  FINAL  "})
        assert decision.kind == KIND_FINAL

    def test_未知kind原样保留交给校验链(self) -> None:
        """不在这里替校验链做判断 —— 由 validate_decision 给出人话拒绝原因。"""
        assert parse_decision({"kind": "teleport"}).kind == "teleport"

    def test_超长文本被截断(self) -> None:
        decision = parse_decision({"kind": "final", "thought": "很" * 500})
        assert len(decision.thought) <= 201

    def test_非字符串文本字段被转换(self) -> None:
        decision = parse_decision({"kind": "final", "thought": 123})
        assert decision.thought == "123"


class TestBuildDecisionPrompt:
    def test_含目录与硬性规则(self) -> None:
        prompt = build_decision_prompt(_context())

        assert "- searcher（主架构）" in prompt
        assert "禁止选择：" in prompt
        assert "【硬性规则】" in prompt
        for marker in ("只输出一个 JSON 对象", "到点暂停", "不要原样重来", "不得编造"):
            assert marker in prompt, marker
        assert DECIDE_SCHEMA_HINT in prompt
        # 收尾要给「给用户看的结论」，而不是把"可以收尾"这类内心独白当结论。
        assert "必须给出 answer" in prompt
        assert "内部独白" in prompt

    def test_硬性规则约束产出归属与不要通读仓库(self) -> None:
        """卡 2 / 卡 3：零产物不得声称「已构建」，也不要用「翻一遍仓库」当完成。"""
        prompt = build_decision_prompt(_context())

        assert "工作区原有的文件" in prompt  # 既有文件不算本任务产出
        assert "【已产出】" in prompt
        assert "不要通读整个仓库" in prompt
        assert "先把它写出来再验证" in prompt

    def test_硬性规则要求结论可核对与真实实现(self) -> None:
        """卡 15 + 代码质量：结论不得引用没产出的文件；代码要真做事并给运行证据。"""
        prompt = build_decision_prompt(_context())

        assert "必须真的在【已产出】里" in prompt  # 结论只能引用真实产出
        assert "而 main.py 并不存在" in prompt  # 反例直接写进规则
        assert "不是搭空壳" in prompt
        assert "真实运行证据" in prompt
        assert "同步更新调用方" in prompt  # 改接口必须改测试，否则必然全红

    def test_目录里的动作带参数签名(self) -> None:
        """模型不必再猜 inputs（实测：缺 key / scope 猜成 long_term 就栽在这）。"""
        prompt = build_decision_prompt(_context())

        assert "path*" in prompt  # 必填参数标星
        assert "按签名给 inputs" in prompt  # 写法说明

    def test_含需求与进度区(self) -> None:
        context = _context()
        context.budget_note = "3/12 步 · 8200/60000 tokens"
        prompt = build_decision_prompt(context)

        assert "用户需求：把调研结论写成报告" in prompt
        assert "已完成：无（这是第一步）" in prompt
        assert "已产出：无" in prompt
        assert "已用预算：3/12 步 · 8200/60000 tokens" in prompt

    def test_历史窗口只带最近N轮(self) -> None:
        context = _context()
        for index in range(1, 9):
            context.history.append(
                {
                    "round": index,
                    "role": "searcher",
                    "action": "web_search",
                    "status": "done",
                    "summary": f"第{index}轮的独特摘要",
                }
            )
        prompt = build_decision_prompt(context, history_window=DEFAULT_HISTORY_WINDOW)

        assert "第8轮的独特摘要" in prompt
        assert "第3轮的独特摘要" in prompt  # 最近 6 轮 = 第 3..8 轮
        assert "第2轮的独特摘要" not in prompt
        assert "第1轮的独特摘要" not in prompt

    def test_窗口为0时不带历史(self) -> None:
        context = _context()
        context.history.append({"round": 1, "summary": "独一无二的旧摘要"})
        assert "独一无二的旧摘要" not in build_decision_prompt(context, history_window=0)

    def test_重试提示会被追加(self) -> None:
        prompt = build_decision_prompt(_context(), retry_note="上次输出无法解析")
        assert "【注意】上次输出无法解析" in prompt

    def test_观察结果回灌给模型(self) -> None:
        """只给「摘要」等于让模型盲飞：实测它列完目录仍不知道列到了什么。"""
        context = _context()
        context.history.append(
            {
                "round": 1,
                "role": "code_worker",
                "action": "file_list",
                "status": "done",
                "summary": "列出文件完成",
                "observation": "[FILE] hello.py (3 bytes)",
            }
        )
        prompt = build_decision_prompt(context)

        assert "观察结果：" in prompt
        assert "[FILE] hello.py (3 bytes)" in prompt

    def test_观察结果超长被裁剪并写明原文长度(self) -> None:
        context = _context()
        context.history.append({"round": 1, "summary": "s", "observation": "x" * 5000})
        prompt = build_decision_prompt(context)

        assert "…（原文 5000 字 / 共 1 行" in prompt
        # 说明必须**可操作**：告诉模型看的是前几行、下次一段读多大 —— 否则它会直接
        # 要一个 241 行的范围（8.4k 字）再次超窗（实测用户任务复现 run-2）。
        assert "一次不超过 50 行" in prompt
        # 窗口装得下源码结构（实测 800 装不下：签名落在 843 字处），但仍是有界的。
        assert "x" * 3000 not in prompt

    def test_目录跟随门控(self) -> None:
        closed = build_decision_prompt(_context(catalog=CLOSED))
        assert "不得提议" in closed
        assert "auditor" not in closed

    def test_系统纠正写进提示词(self) -> None:
        """循环检测到「连续只读 + 验证仍失败」时，会把纠正文案塞进提示词（不靠模型自觉）。"""
        assert "【系统纠正】" not in build_decision_prompt(_context())

        context = _context()
        context.stall_note = "你已连续 3 轮只做只读动作，下一步必须给出改动"

        prompt = build_decision_prompt(context)

        assert "【系统纠正】你已连续 3 轮只做只读动作，下一步必须给出改动" in prompt

    def test_已有实现时给出复用与修复的明确处置(self) -> None:
        """实测（用户任务 27bd52dc）：工作区已有实现时，模型连读 7 步、**一步没写** ——
        它知道「那不是我的产出」，但没人告诉它**该拿它怎么办**，于是把预算烧在
        「复用还是重写」的犹豫上。规则必须给出处置。"""
        prompt = build_decision_prompt(_context())

        assert "复用它并直接收尾" in prompt
        assert "不要另起一套并行实现" in prompt
        assert "start_line/end_line" in prompt

        opened = build_decision_prompt(_context(catalog=OPENED, allow_propose=True))
        assert "kind=propose" in opened
        assert "auditor" in opened

    def test_目录取自传入的catalog而非重新推导(self) -> None:
        """提示词必须与「当前实际可选目录」一致（允许注入自定义目录）。"""
        custom = (
            RoleSpec(
                name="custom_role",
                architecture="main",
                mission="测试用",
                accepts=frozenset({"web_search"}),
                high_risk_tools=frozenset(),
            ),
        )
        prompt = build_decision_prompt(_context(catalog=custom))
        assert "custom_role" in prompt
        assert "searcher" not in prompt


class TestLLMRouteDecider:
    def test_一次成功(self) -> None:
        fake = _FakeLLM([{"kind": "agent", "role": "searcher", "action": "web_search"}])
        decider = LLMRouteDecider(llm_client=fake)

        decision = decider.decide(_context())

        assert decision.role == "searcher"
        assert decider.calls == 1
        assert decider.retried is False
        assert decider.name == "llm"
        assert len(fake.prompts) == 1

    def test_首次解析失败后重试成功(self) -> None:
        fake = _FakeLLM([{}, {"kind": "final"}])
        decider = LLMRouteDecider(llm_client=fake)

        decision = decider.decide(_context())

        assert decision.kind == KIND_FINAL
        assert decider.calls == 2
        assert decider.retried is True
        assert decider.name == "llm-retry"  # A/B 归因用
        assert "上次输出无法解析" in fake.prompts[1]  # 第二次问话带上了重试提示

    def test_重试提示点名两种常见成因(self) -> None:
        """实测（用户任务 7deadd5a）：一轮已经验证通过（测试 3 项通过 + 沙箱真跑成功），
        却因为**收尾那轮输出无法解析**而整轮作废（``stopped=invalid_decision``）。
        重试提示必须点名两种最常见成因（写成 Markdown 散文 / answer 太长被截断），
        否则模型只会用同样的方式再失败一次。"""
        fake = _FakeLLM([{}, {"kind": "final"}])
        LLMRouteDecider(llm_client=fake).decide(_context())

        note = fake.prompts[1]
        assert "Markdown" in note
        assert "截断" in note
        assert "200 字" in note

    def test_一直失败则返回invalid(self) -> None:
        fake = _FakeLLM([{}, {}])
        decider = LLMRouteDecider(llm_client=fake)  # retries 默认 1

        decision = decider.decide(_context())

        assert decision.kind == KIND_INVALID
        assert decider.calls == 2
        assert decider.last_error

    def test_重试次数可配为0(self) -> None:
        fake = _FakeLLM([{}, {}])
        decider = LLMRouteDecider(llm_client=fake, retries=0)

        assert decider.decide(_context()).kind == KIND_INVALID
        assert decider.calls == 1

    def test_客户端抛错不炸循环(self) -> None:
        decider = LLMRouteDecider(llm_client=_FakeLLM(raises=RuntimeError("boom")), retries=0)

        decision = decider.decide(_context())

        assert decision.kind == KIND_INVALID
        assert "RuntimeError" in decider.last_error

    def test_提示词带预算进度(self) -> None:
        fake = _FakeLLM([{"kind": "final"}])
        decider = LLMRouteDecider(llm_client=fake)
        context = _context()
        context.budget_note = "1/12 步 · 100/60000 tokens"

        decider.decide(context)

        assert "已用预算：1/12 步 · 100/60000 tokens" in fake.prompts[0]


class TestInvalidNeverBecomesFinal:
    """最关键的安全断言：模型没答出来 ≠ 任务已完成。"""

    def test_解析全程失败时循环停下而不是收尾(self) -> None:
        fake = _FakeLLM([{}, {}])  # 两次都空
        outcome = run_agent_loop(
            context=_context(),
            decider=LLMRouteDecider(llm_client=fake),
            execute=lambda step: StepOutcome(status="done", summary="不该被执行"),
        )

        assert outcome.stopped_reason == STOP_INVALID_DECISION
        assert outcome.stopped_reason != STOP_FINAL
        assert outcome.rounds == []
        assert outcome.fallback_reasons

    def test_模型明确final时正常收尾(self) -> None:
        fake = _FakeLLM([{"kind": "final", "thought": "已完成"}])
        outcome = run_agent_loop(
            context=_context(),
            decider=LLMRouteDecider(llm_client=fake),
            execute=lambda step: StepOutcome(status="done"),
        )
        assert outcome.stopped_reason == STOP_FINAL
        assert outcome.decided_by == "llm"

    def test_决策能落成可执行步骤(self) -> None:
        fake = _FakeLLM(
            [
                {"kind": "agent", "role": "searcher", "action": "web_search", "inputs": {"query": "x"}},
                {"kind": "final"},
            ]
        )
        seen: list[str] = []

        def _execute(step):
            seen.append(step.id)
            return StepOutcome(status="done", summary="搜索到 3 条结果", tokens=12)

        outcome = run_agent_loop(
            context=_context(), decider=LLMRouteDecider(llm_client=fake), execute=_execute
        )

        assert seen == ["loop-001"]
        assert outcome.decided_by == "llm"
        assert outcome.rounds[0].decision.role == "searcher"
