"""pytest 兼容插件：修「受限环境下临时目录连创建者都打不开」的问题。

背景（已实测定位，A/B 验证过）：
    Python 用 ``os.mkdir(path, 0o700)`` 建目录时会写入**显式 DACL**，绕过从父目录继承下来的
    授权；在 Windows 受限令牌（沙箱 / 加固 CI）下，连**创建者自己**都无法 ``listdir`` 该目录
    （``PermissionError [Errno 13]`` / WinError 5）。而 pytest 的 ``tmp_path`` 与 ``tempfile``
    恰好都用 0o700 建目录 —— 于是所有用临时目录的用例在 setup 阶段就红。

    实测影响（用户任务 8d50ac95）：agent 自己写的 ``test_cli_end_to_end`` 一直卡在这个
    ``PermissionError`` 上，模型在思考里**已经正确识别**它是"测试自身在 tmpdir 写文件的问题"，
    但它无论如何推理都修不掉 —— 一轮任务因此停在「失败 2 项」直到空转。

本插件的做法（**只做这一件事**）：
    把「位于临时目录之下 **且** ``mode == 0o700``」的 ``os.mkdir`` 放宽为 ``0o755``，
    其余一律原样透传：项目自身代码建的目录、以及 0o755/0o777 的既有调用都不受影响。
    0o755 而非 0o777：A/B 实测 0o755 已经可读可写（属主仍有 rwx），权限放开面更小。

用法（由 ``test_run`` 自动注入，一般不需要手动调用）::

    python -m pytest -p agent_builder.tools.pytest_tmpfix ...
"""

from __future__ import annotations

import os
import tempfile

_orig_mkdir = os.mkdir

_roots: tuple[str, ...] = tuple(
    sorted(
        {
            os.path.abspath(value).lower()
            for value in (
                tempfile.gettempdir(),
                os.environ.get("PYTEST_DEBUG_TEMPROOT") or "",
                os.environ.get("TEMP") or "",
                os.environ.get("TMP") or "",
            )
            if value
        }
    )
)


def _mkdir(path, mode=0o777, *args, **kwargs):
    """临时目录下的 0o700 放宽为 0o755；其余原样透传。"""
    if mode == 0o700:
        try:
            target = os.path.abspath(os.fspath(path)).lower()
            if any(target.startswith(root) for root in _roots):
                mode = 0o755
        except (OSError, TypeError, ValueError):
            pass
    return _orig_mkdir(path, mode, *args, **kwargs)


os.mkdir = _mkdir


__all__: list[str] = []
