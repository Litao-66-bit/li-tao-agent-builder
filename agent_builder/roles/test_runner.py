"""测试执行者（TestRunner）—— 主架构・验证层。

职责：按验收标准写/跑用例（沙箱内）+ 权限边界检查 + 输出测试报告。

边界声明：
- 不修改被测代码（只读+跑测试）
- 失败 → 附上失败原因与日志打回执行层
- 测试环境异常 → 重试 1 次，仍异常则标记"环境失败"并上报

执行协议：
1. 按验收标准写/跑用例（沙箱内）
2. 权限边界检查：路径、命令白名单是否被踩
3. 输出测试报告（passed/failed + 可复现日志）
"""

from __future__ import annotations

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
                executor_fn(step)
                # 从 inputs 提取测试结果。
                raw_cases = step.inputs.get("test_cases", [])
                cases = self._parse_cases(raw_cases)
                passed = sum(1 for c in cases if c.status == "passed")
                failed = sum(1 for c in cases if c.status == "failed")
                error = sum(1 for c in cases if c.status == "error")
                repro_log = step.inputs.get("repro_log", "")

                return TestReport(
                    step_id=step.id,
                    status="done" if failed == 0 and error == 0 else "failed",
                    cases=cases,
                    passed=passed,
                    failed=failed,
                    error=error,
                    repro_log=repro_log,
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
    "ExecutorFn",
    "TestCase",
    "TestReport",
    "TestRunner",
]
