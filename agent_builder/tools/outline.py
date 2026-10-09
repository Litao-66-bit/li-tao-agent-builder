"""源码符号轮廓：把「读不全的文件」里最要紧的信息（类 / 函数定义 + 行号）摘出来。

**为什么住在 tools 层**（而不是 ``api/deciders``）：写文件的工具（``file_write`` /
``file_edit``）也要在返回值里带上它 —— 而 ``tools`` 不能反向 import ``api``。
放在这里，api 与 tools 两边都能安全引用，且只有一份实现。
"""

from __future__ import annotations

import re

_SYMBOL_LINE_RE = re.compile(r"^\s*(?:async\s+)?(?:class|def)\s+[A-Za-z_]\w*")


def symbol_outline(raw: str, *, max_symbols: int = 40) -> str:
    """抽取**全文**的 class/def 定义行（行号 + 定义）；没有符号时返回空串。

    为什么需要它（实测，用户任务 ``cea128b8``）：观察窗口只有 2000 字，装不下大文件的
    尾部，而模型最需要的恰恰是那里的接口签名 —— 它连续 **4 次**读同一个 6.6k 字文件，
    思考里明写「需要看到 ``ResearchAgent.__init__`` 的真实签名」，那段永远在窗口之外，
    于是只能重读 → 被截断 → 再重读，8 步零产物 ``stagnant`` 停下。

    写文件的工具再用它解决另一半问题（实测）：模型写完实现后写测试时，会按**想象**的
    API 写（``rank_papers(topic=)`` vs ``keywords`` …），自己写的测试和自己的实现自相矛盾。
    """
    found: list[str] = []
    for lineno, line in enumerate(raw.splitlines(), 1):
        if _SYMBOL_LINE_RE.match(line):
            found.append(f"{lineno}: {line.strip()[:110]}")
            if len(found) >= max_symbols:
                found.append("…（更多定义已省略）")
                break
    if not found:
        return ""
    return (
        "\n【全文符号轮廓（行号: 定义）—— 需要某段细节请用 file_read 的 "
        "start_line/end_line 精确读取】\n" + "\n".join(found)
    )


__all__ = ["symbol_outline"]
