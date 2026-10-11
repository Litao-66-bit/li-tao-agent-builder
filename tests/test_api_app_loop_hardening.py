"""后端不因"客户端断开"而退出（实测 `WinError 10054` 把 uvicorn 整个带崩）。

**为什么只修崩溃、不做"没人连就自动退出"**（用户口径，2026-10-11）：
"关了前端后端还活着"是**优点** —— 任务不会因为刷新/关页面就丢；
而加自动退出很容易变成"前端关了、后端也关不掉/起不来"，反而让用户失去控制。
所以后端进程的启停**由用户说了算**，本文件只保证"客户端断开"不带走进程。

三条不变量：

1. 处理器**装了**（否则等于没修）；
2. 连接类异常被吞掉，**不逃出回调**（进程不死）；
3. **其它异常仍然上报** —— 绝不能把真 bug 一起吞了。
"""

from __future__ import annotations

import asyncio

import pytest

from agent_builder.api.app import (
    _install_connection_reset_guard,
    _is_connection_noise,
    create_app,
)


class TestConnectionNoiseClassification:
    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"),
            ConnectionAbortedError(10053, "软件中止了一个已建立的连接。"),
            BrokenPipeError(32, "Broken pipe"),
        ],
    )
    def test_连接类异常被认作噪声(self, exc: OSError) -> None:
        assert _is_connection_noise(exc) is True

    def test_winerror形式的10054也被认出(self) -> None:
        exc = OSError(10054, "远程主机强迫关闭了一个现有的连接。")
        exc.winerror = 10054  # type: ignore[attr-defined]
        assert _is_connection_noise(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("真的业务错误"),
            KeyError("缺键"),
            RuntimeError("真 bug"),
            OSError(2, "文件不存在"),
        ],
    )
    def test_其它异常不是噪声(self, exc: BaseException) -> None:
        """防"把真 bug 一起吞了"：这条是安全边界。"""
        assert _is_connection_noise(exc) is False


class TestGuardInstalled:
    def test_在运行中的循环里能装上(self) -> None:
        """真实场景是"有运行中的循环"（uvicorn lifespan 跑在 asyncio.run 里）。

        回归：第一版用 `get_event_loop()` 拿循环 → 在 `asyncio.run` 下抛
        `RuntimeError: There is no current event loop` → **应用启动直接失败**（实测把后端整挂）。
        """
        installed: list[bool] = []

        async def _run() -> None:
            loop = asyncio.get_running_loop()
            assert loop.get_exception_handler() is None
            installed.append(_install_connection_reset_guard())
            assert loop.get_exception_handler() is not None

        asyncio.run(_run())
        assert installed == [True]

    def test_拿不到循环时不抛也不崩启动(self, monkeypatch) -> None:
        """守卫是"防崩溃"的，绝不能因为它自己失败而让应用起不来。"""
        monkeypatch.setattr("agent_builder.api.app._current_loop", lambda: None)

        assert _install_connection_reset_guard() is False  # 不抛

    def test_创建应用不抛(self) -> None:
        app = create_app()
        assert app is not None

    def test_重复安装不叠加崩溃(self) -> None:
        """反复调用只应替换处理器，不应把上一个包成链式调用（避免无限套娃）。"""

        async def _run() -> None:
            for _ in range(3):
                assert _install_connection_reset_guard() is True
            assert asyncio.get_running_loop().get_exception_handler() is not None

        asyncio.run(_run())


class TestHandlerBehaviour:
    """直接调处理器，验证"吞连接异常、放行真异常"。"""

    def _handler_in_loop(self, loop: asyncio.AbstractEventLoop):
        asyncio.set_event_loop(loop)
        assert _install_connection_reset_guard() is True
        return loop.get_exception_handler()

    def test_连接异常被吞(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            handler = self._handler_in_loop(loop)
            # 被吞时**不会有任何记录**（只记 debug）；这里断言"没有被当成错误上报"。
            with caplog_at_error() as captured:
                handler(loop, {"exception": ConnectionResetError(10054, "断开")})
            assert captured.records == [], "连接断开不该被当成错误上报"
        finally:
            loop.close()

    def test_真异常仍然上报(self, caplog) -> None:
        """真异常必须走到默认处理器（默认处理器会记日志 + 打印），不能被静默吞掉。"""
        import logging

        loop = asyncio.new_event_loop()
        try:
            handler = self._handler_in_loop(loop)
            with caplog.at_level(logging.ERROR, logger="asyncio"):
                handler(
                    loop,
                    {"exception": RuntimeError("真 bug 必须可见"), "message": "boom"},
                )
            # asyncio 默认处理器会把它记到 logger "asyncio"；只要它没被我们静默 return 就算通过。
            assert caplog.records, "真异常必须仍然上报（否则会吞掉真 bug）"
        finally:
            loop.close()


def caplog_at_error():
    """小助手：拿一个"只收 ERROR 及以上"的日志采集器（避免依赖 caplog 的全局状态）。"""
    import logging

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    collector = _Collector(level=logging.ERROR)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(collector)
    root.setLevel(logging.DEBUG)

    class _Ctx:
        @property
        def records(self):
            return records

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> None:
            root.removeHandler(collector)
            root.setLevel(previous_level)

    return _Ctx()

