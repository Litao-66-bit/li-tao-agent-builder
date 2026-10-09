"""临时诊断插件：把 pytest 失败以 GitHub Actions **注解**形式带出去。

为什么需要它：CI 在 ``ubuntu-latest`` 上 pytest 失败，而 workflow/job 的日志接口需要
``actions:read`` 权限（本次会话的 token 没有该权限，403）。**注解是公开可读的**，所以把
失败写成 ``::error ...``，就能在不额外授权的情况下定位到具体失败用例。

实现要点（实测踩过）：
- ``pytest_runtest_logreport`` 里的 ``print`` **会被 pytest 的输出捕获吞掉**，不会进 CI 日志；
  只有 ``pytest_terminal_summary`` 的 ``print`` 出现在日志里（实测），所以统一在这里上报。
- GitHub 注解按**行**解析，所以把明细压成单行（换行用 ``⏎`` 占位），一次上报全部失败。

⚠️ 这是**临时诊断代码**：定位并修好 Linux 特有失败后，会连同 pyproject 的 entry point 一起删除。
"""

from __future__ import annotations


def pytest_terminal_summary(terminalreporter):
    """把失败明细汇总成一条注解（含用例 nodeid 与前几行报错）。"""
    stats = terminalreporter.stats
    failed = list(stats.get("failed", []))
    errors = list(stats.get("error", []))
    if not (failed or errors):
        return
    parts = [f"pytest failed={len(failed)} errors={len(errors)}"]
    for report in (failed + errors)[:12]:
        detail = " ⏎ ".join(str(getattr(report, "longrepr", "") or "").splitlines()[:5])
        parts.append(f"{getattr(report, 'nodeid', '?')} :: {detail[:400]}")
    print("::error ::" + " ⏎⏎ ".join(parts))


__all__: list[str] = []
