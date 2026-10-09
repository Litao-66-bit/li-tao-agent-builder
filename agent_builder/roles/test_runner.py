"""测试执行者（TestRunner）—— 主架构・验证层。

职责：按验收标准写/跑用例（沙箱内）+ 权限边界检查 + 输出测试报告。

边界声明：
- 不修改被测代码（只读+跑测试）
- 失败 → 附上失败原因与日志打回执行层
- 测试环境异常 → 重试 1 次，仍异常则标记"环境失败"并上报
- **计数只认真实输出**：passed/failed/error 从工具返回的 pytest 输出解析；
  拿不到可核对的结果（target 不存在 / 一条用例都没跑到）一律判"环境失败"，
  绝不默认 done —— done + "测试通过 0 项" 会把"没测到"伪装成"测试通过"。

执行协议：
1. 按验收标准写/跑用例（沙箱内）
2. 权限边界检查：路径、命令白名单是否被踩
3. 输出测试报告（passed/failed + 可复现日志）
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 验证类 action 集合。
TEST_ACTIONS: frozenset[str] = frozenset({
    "test_run",
    "sandbox_run",
})

# 环境异常最大重试次数。
MAX_RETRIES = 1

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]

# pytest 汇总行里的计数，如 "39 passed in 0.52s" / "1 failed, 2 passed"。
_PYTEST_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|xfailed|xpassed|skipped|deselected|warning|warnings)\b"
)
# pytest 明确「一条用例都没跑到」（exit code 5）。
_NO_TESTS_RE = re.compile(r"no tests ran", re.IGNORECASE)
# 工具侧的输出截断标记（见 tools/impl/test_run.py 的 MAX_OUTPUT_CHARS）。
_TRUNCATED_MARK = "…[已截断"
# `-v` 明细行的结果标记：仅在汇总行丢失（输出被截断）时降级使用。
_VERBOSE_MARK_RE = re.compile(r"::\S+\s+(PASSED|FAILED|ERROR)\b")
# pytest「short test summary info」里的失败行（形如
# ``FAILED tests/x.py::test_a - ModuleNotFoundError: ...``）：这是模型改代码
# 最需要的线索，失败时必须带出去，否则只剩一句「角色执行未通过」。
_PYTEST_FAILURE_LINE_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$", re.MULTILINE
)
# pytest 在 FAILURES / ERRORS 段里把断言/异常写成 ``E   xxx`` 行。集合期报错
# （import 失败等）的汇总行**不带原因**，这几行是唯一的真因来源。
_PYTEST_ERROR_LINE_RE = re.compile(r"^E\s+(.+)$", re.MULTILINE)

# 失败原因里最多带上多少条用例（够定位即可，不把输出整段倒给模型）。
MAX_FAILURE_LINES = 5

# 汇总行没给原因时，最多再带几条 "E   ..." 行。
MAX_ERROR_LINES = 3

# 单条失败原因的长度上限。
MAX_FAILURE_REASON_CHARS = 160

# 无法从输出核对测试结果时的说明（宁可判「无法核对」，也不默认 done）。
UNVERIFIABLE_MSG = (
    "未能从测试输出中解析出可核对的结果（可能一条用例都没跑到）；"
    "请确认 target 指向真实存在的测试文件或目录"
)


@dataclass(slots=True)
class TestCase:
    """单条测试用例。"""

    name: str
    status: str  # passed | failed | error | skipped
    log: str = ""


@dataclass(slots=True)
class TestReport:
    """测试报告。"""

    step_id: str
    status: str  # done | failed | rejected | pending | env_failure
    cases: list[TestCase] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    error: int = 0  # 环境错误数
    repro_log: str = ""  # 可复现日志
    error_msg: str | None = None


def _parse_pytest_counts(output: str) -> tuple[int, int, int] | None:
    """从 pytest 输出里解析 ``(passed, failed, error)``；解析不到返回 None。

    口径（对应实机 bug「test_run 永远报『测试通过 0 项』」）：
    pytest 的计数只存在于**它自己的输出**里，所以必须从输出解析，而不是让模型
    在 inputs 里自填。

    1. 优先取**汇总行**（pytest 最后一行，如 ``39 passed in 0.52s``）；
    2. 输出被截断（汇总行随之丢失）时，降级为统计 ``-v`` 明细行的
       ``PASSED`` / ``FAILED`` / ``ERROR`` 标记；
    3. ``no tests ran``／空输出／无任何计数 → None（调用方判「无法核对」）。
    """
    if not isinstance(output, str) or not output.strip():
        return None
    # 去掉工具追加的截断说明行，避免它干扰汇总行扫描。
    lines = [line for line in output.splitlines() if _TRUNCATED_MARK not in line]
    if any(_NO_TESTS_RE.search(line) for line in lines):
        return None
    for line in reversed(lines):
        hits = _PYTEST_COUNT_RE.findall(line)
        if not hits:
            continue
        passed = failed = error = 0
        for number, kind in hits:
            if kind == "passed":
                passed += int(number)
            elif kind == "failed":
                failed += int(number)
            elif kind.startswith("error"):
                error += int(number)
        return passed, failed, error
    marks = _VERBOSE_MARK_RE.findall(output)
    if marks:
        return marks.count("PASSED"), marks.count("FAILED"), marks.count("ERROR")
    return None


def _failure_detail(output: str, *, passed: int, failed: int, error: int) -> str:
    """拼失败原因：计数 + pytest 汇总里的失败行（用例名 → 原因）。

    模型要"自己改代码"就必须看到**为什么失败**；只回一句「角色执行未通过」
    等于让它盲改（实机踩过：跑了 12 步才发现接口不匹配）。
    """
    head = f"测试未通过：失败 {failed} 项、错误 {error} 项（通过 {passed} 项）"
    items: list[str] = []
    for node, reason in _PYTEST_FAILURE_LINE_RE.findall(output or ""):
        if len(items) >= MAX_FAILURE_LINES:
            break
        reason = (reason or "").strip()
        if len(reason) > MAX_FAILURE_REASON_CHARS:
            reason = reason[:MAX_FAILURE_REASON_CHARS] + "…"
        items.append(f"{node} → {reason}" if reason else str(node))
    # 汇总行没带原因（集合期报错就是这样）→ 从 "E   ..." 行里取真因，
    # 否则模型只知道"哪个文件错了"，仍然改不动。
    if not any("→" in item for item in items):
        seen: list[str] = []
        for line in _PYTEST_ERROR_LINE_RE.findall(output or ""):
            detail = line.strip()
            if not detail or detail in seen:
                continue
            if len(detail) > MAX_FAILURE_REASON_CHARS:
                detail = detail[:MAX_FAILURE_REASON_CHARS] + "…"
            seen.append(detail)
            if len(seen) >= MAX_ERROR_LINES:
                break
        if seen:
            items.append(" / ".join(seen))
    if not items:
        return head
    return f"{head}；" + "；".join(items)


@dataclass(slots=True)
class TestRunner:
    """测试执行者角色：跑用例 + 权限边界检查 + 测试报告。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        max_retries: 环境异常重试次数。
    """

    __test__ = False  # pytest 不要收集此类作为测试类（类名以 Test 开头）

    correlation_id: str = "c-unknown"
    max_retries: int = MAX_RETRIES

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> TestReport:
        """执行验证类步骤。

        Args:
            step: 验证类步骤（action 必须在 TEST_ACTIONS 中）。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            TestReport：测试报告（passed/failed + 可复现日志）。
        """
        # 1. 校验是否验证类。
        if step.action not in TEST_ACTIONS:
            return TestReport(
                step_id=step.id,
                status="rejected",
                error_msg=f"任务超出验证范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return TestReport(
                step_id=step.id,
                status="pending",
            )

        # 3. 执行测试（环境异常 → 重试 1 次）。
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                raw_output = executor_fn(step)
                report = self._build_report(step, raw_output)
                if report is not None:
                    return report
                # 跑过工具却拿不到可核对的结果（例如 target 不存在、一条用例都没跑到）
                # → 判「无法核对」。绝不默认 done：那会把「没测到」说成「测试通过」。
                return TestReport(
                    step_id=step.id,
                    status="env_failure",
                    error_msg=UNVERIFIABLE_MSG,
                )
            except PermissionError as exc:
                # 权限不足 → 标记环境失败（不重试）。
                return TestReport(
                    step_id=step.id,
                    status="env_failure",
                    error_msg=f"权限不足: {exc}",
                )
            except OSError as exc:
                # 环境异常 → 重试 1 次。
                last_error = f"环境异常: {type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    continue
                return TestReport(
                    step_id=step.id,
                    status="env_failure",
                    error_msg=last_error,
                )
            except Exception as exc:  # noqa: BLE001  执行者需捕获所有执行异常
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    continue
                return TestReport(
                    step_id=step.id,
                    status="failed",
                    error_msg=last_error,
                )
        return TestReport(
            step_id=step.id,
            status="env_failure",
            error_msg=last_error,
        )

    def _build_report(self, step: Step, output: Any) -> TestReport | None:
        """把**真实工具输出**（必要时叠加上显式 ``test_cases``）转成测试报告。

        优先级（口径：真实输出是唯一事实来源）：
        1. 从 ``output`` 解析出的 pytest 计数 —— 真实跑了什么就报什么；
        2. 步骤显式声明的 ``test_cases``（历史契约：固定工作流 / 单测注入）；
        两者都拿不到 → 返回 None，由调用方判「无法核对」（绝不默认 done）。
        """
        parsed = _parse_pytest_counts(output) if isinstance(output, str) else None
        if parsed is not None:
            passed, failed, error = parsed
            all_passed = failed == 0 and error == 0
            return TestReport(
                step_id=step.id,
                status="done" if all_passed else "failed",
                passed=passed,
                failed=failed,
                error=error,
                repro_log=step.inputs.get("repro_log", ""),
                # 失败必须带出「哪条用例、为什么」——否则调用链只剩一句
                # 「角色执行未通过」，模型无从下手（实机踩过）。
                error_msg=(
                    None
                    if all_passed
                    else _failure_detail(
                        str(output), passed=passed, failed=failed, error=error
                    )
                ),
            )

        raw_cases = step.inputs.get("test_cases", [])
        cases = self._parse_cases(raw_cases) if isinstance(raw_cases, list) else []
        if not cases:
            return None
        passed = sum(1 for c in cases if c.status == "passed")
        failed = sum(1 for c in cases if c.status == "failed")
        error = sum(1 for c in cases if c.status == "error")
        return TestReport(
            step_id=step.id,
            status="done" if failed == 0 and error == 0 else "failed",
            cases=cases,
            passed=passed,
            failed=failed,
            error=error,
            repro_log=step.inputs.get("repro_log", ""),
        )

    def _parse_cases(self, raw_cases: list[dict[str, Any]]) -> list[TestCase]:
        """解析测试用例清单。"""
        cases: list[TestCase] = []
        for raw in raw_cases:
            if isinstance(raw, dict) and "name" in raw:
                status = raw.get("status", "skipped")
                if status not in ("passed", "failed", "error", "skipped"):
                    status = "skipped"
                cases.append(
                    TestCase(
                        name=str(raw["name"]),
                        status=status,
                        log=str(raw.get("log", "")),
                    )
                )
        return cases


__all__ = [
    "MAX_RETRIES",
    "TEST_ACTIONS",
    "UNVERIFIABLE_MSG",
    "ExecutorFn",
    "TestCase",
    "TestReport",
    "TestRunner",
]
