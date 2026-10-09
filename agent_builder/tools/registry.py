"""工具注册表 + 执行器。

所有工具在此注册。执行流程：
    gatekeeper.check(权限/沙箱/审批) → registry 查实现 → 调用 → 回填结果。

安全逻辑（权限/沙箱/审批）完全在门卫，registry 只负责分发与超时兜底。
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any

from agent_builder.contracts.errors import timeout_error, validation_error
from agent_builder.contracts.schemas import ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper, current_workspace_dir
from agent_builder.tools.spec import ToolSpec, normalize_args

logger = logging.getLogger(__name__)

# 跨实现函数传递 correlation_id（避免每个 impl 签名都带该参数）。
current_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tool_correlation_id", default="c-unknown"
)


class ToolRegistry:
    """工具注册表。register 时不可重名；execute 时查不到抛 E_VALIDATION。"""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Callable[..., Any]]] = {}

    def register(self, spec: ToolSpec, impl: Callable[..., Any]) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具 {spec.name!r} 已注册，不可重复注册")
        self._tools[spec.name] = (spec, impl)

    def get(self, name: str) -> tuple[ToolSpec, Callable[..., Any]] | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def execute(self, gatekeeper: ToolGatekeeper, call: ToolCall) -> ToolCall:
        """执行一次工具调用。

        0. 参数归一化（宽松）：别名映射 + 丢弃未声明键；**先于门卫**，使别名后的
           ``path`` 等仍能被沙箱校验，且审计记录与实际执行参数一致。
        1. 门卫校验（权限/沙箱/审批）；不通过由门卫抛 E_PERMISSION。
        2. 查注册表；未注册抛 E_VALIDATION。
        3. 必填参数校验；缺必填抛 E_VALIDATION（错误信息附允许参数）。
        4. 按 spec.timeout_s 调用实现；超时抛 E_TIMEOUT。
        5. 回填 call.result。
        """
        # 参数契约：分解器产出的 inputs 未必等于工具签名，先按 spec 归一化，
        # 避免「多一个键」直接崩在 Python 签名上（历史 TypeError）。
        entry = self._tools.get(call.tool)
        if entry is not None:
            spec_for_args, _impl = entry
            normalized, dropped = normalize_args(spec_for_args, call.args)
            if dropped:
                logger.warning("工具 %s 忽略未声明参数: %s", call.tool, dropped)
            call.args = normalized

        gatekeeper.check(call)  # 安全边界：权限 + 沙箱 + 审批
        if entry is None:
            raise validation_error(
                f"工具 {call.tool!r} 未注册",
                source="tool_registry",
                correlation_id=gatekeeper.correlation_id,
            )
        spec, impl = entry
        required = spec.parameters.get("required") or []
        missing = [name for name in required if name not in call.args]
        if missing:
            allowed = sorted((spec.parameters.get("properties") or {}).keys())
            raise validation_error(
                f"工具 {call.tool!r} 缺少必填参数: {missing}（允许的参数: {allowed}）",
                source=f"tool.{call.tool}",
                correlation_id=gatekeeper.correlation_id,
            )
        token = current_correlation_id.set(gatekeeper.correlation_id)
        # 沙箱基准同样用 contextvar 传给实现：实现拿不到门卫实例，此前只能按进程 cwd 解析
        # 相对路径（换工作区后会读写到错误目录）。
        ws_token = current_workspace_dir.set(gatekeeper.workspace_dir)
        try:
            try:
                result = self._run_with_timeout(
                    impl, call.args, spec.timeout_s, call.tool
                )
            except TypeError as exc:
                raise validation_error(
                    f"工具 {call.tool!r} 参数非法: {exc}",
                    source=f"tool.{call.tool}",
                    correlation_id=gatekeeper.correlation_id,
                ) from exc
        finally:
            current_correlation_id.reset(token)
            current_workspace_dir.reset(ws_token)
        call.result = result
        return call

    @staticmethod
    def _run_with_timeout(
        impl: Callable[..., Any],
        args: dict[str, Any],
        timeout_s: float,
        tool_name: str,
    ) -> Any:
        """线程池超时兜底（跨平台，不依赖 signal）。

        用 contextvars.copy_context() 将当前 contextvar 传递到子线程，
        使 impl 内 current_correlation_id.get() 能取到门卫设置的值。
        """
        cid = current_correlation_id.get()
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ctx.run, impl, **args)
            try:
                return future.result(timeout=timeout_s)
            except FuturesTimeout:
                future.cancel()
                raise timeout_error(
                    f"工具 {tool_name!r} 执行超时（>{timeout_s}s）",
                    source=f"tool.{tool_name}",
                    correlation_id=cid,
                ) from None


registry = ToolRegistry()


__all__ = ["ToolRegistry", "current_correlation_id", "registry"]
