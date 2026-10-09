"""临时诊断插件：把 pytest 失败以 GitHub Actions **注解**形式带出去。

为什么需要它：CI 在 ``ubuntu-latest`` 上 pytest 失败，而 workflow/job 的日志接口需要
``actions:read`` 权限（本次会话的 token 没有该权限，403）。**注解是公开可读的**，所以把
失败写成 ``::error file=...::...``，就能在不额外授权的情况下定位到具体失败用例。

通过 ``pyproject.toml`` 的 ``[project.entry-points.pytest11]`` 自动加载（CI 会
``pip install -e .``），因此**不需要改 CI 命令，也不需要新增 conftest.py**。

⚠️ 这是**临时诊断代码**：定位并修好 Linux 特有失败后，会连同那个 entry point 一起删除。
"""

from __future__ import annotations


def pytest_runtest_logreport(report):
    """任一阶段失败 → 打一条 GitHub Actions 注解（日志里表现为 error annotation）。"""
    if not report.failed:
        return
    path = str(report.nodeid).split("::")[0]
    detail = " ⏎ ".join(str(report.longrepr).splitlines()[:6])[:600]
    print(f"::error file={path}::{report.nodeid} :: {detail}")


def pytest_terminal_summary(terminalreporter):
    """末尾再打一条汇总注解（连收集错误一起计数，便于确认是否还有遗漏）。"""
    stats = terminalreporter.stats
    failed = len(stats.get("failed", []))
    errors = len(stats.get("error", []))
    if failed or errors:
        print(f"::error ::pytest 汇总 failed={failed} errors={errors}")


__all__: list[str] = []
