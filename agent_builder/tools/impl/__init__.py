"""工具实现包。每个工具一个模块，导入即注册到 registry。"""

from agent_builder.tools.impl import file_read  # noqa: F401  导入触发注册

__all__: list[str] = []
