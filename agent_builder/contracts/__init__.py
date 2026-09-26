"""契约层：主架构全部角色通信与工具校验的基准。

对应 docs/contracts/ 01~04：
- errors.py        → 04-error-codes.md  错误码体系
- messages.py      → 03-message-protocol.md  消息协议
- schemas.py       → 01-schemas.md  六类核心 Schema
- state_machine.py → 02-state-machine.md  任务状态机
"""

from agent_builder.contracts.errors import ERROR_NAMES, AgentError, ErrorInfo
from agent_builder.contracts.state_machine import TaskStatus, TransitionError

__all__ = [
    "ERROR_NAMES",
    "AgentError",
    "ErrorInfo",
    "TaskStatus",
    "TransitionError",
]
