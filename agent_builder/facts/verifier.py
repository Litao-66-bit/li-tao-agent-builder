"""事实核验 —— P1-3「模型行为层」最小落地：来源可溯性检查。

当前为「标记不拦截」机制（保守策略）：
- 识别执行结果中**需要来源支撑的断言**（增长/下降/达到/排名/占比…）；
- 无来源标注 → 标记 UNVERIFIED，汇总时在报告尾部附核验提示，建议人工复核；
- 后续可升级为交叉检索核验（web_search 对照）与 STRICT 拦截模式。

这是主架构「事实核验者」角色的首个确定性实现。
"""

from __future__ import annotations

import re

# 断言动词：命中即认为该句需要来源支撑。
ASSERTION_VERBS: tuple[str, ...] = (
    "增长",
    "下降",
    "达到",
    "突破",
    "排名",
    "首次",
    "总计",
    "超过",
    "占比",
    "同比",
    "环比",
    "同期",
    "预计",
    "实际",
    "平均",
    "最高",
    "最低",
    "第一",
    "唯一",
    "突破",
)

# 来源标注：命中即认为断言有出处。
SOURCE_MARKERS: tuple[str, ...] = (
    "[来源",
    "(来源",
    "（来源",
    "来源：",
    "来源:",
    "http://",
    "https://",
    "参考文献",
    "引用",
    "[1]",
    "[2]",
    "[3]",
    "[4]",
    "[5]",
)

# 断言句模式：句子含数字 + 断言动词（比单独动词更精准，降低误报）。
_ASSERTION_SENTENCE_RE = re.compile(r"\d+(\.\d+)?%?\s*(个|次|万|亿|元|%|年|月|人|家|项|倍)?")


def requires_source(text: str) -> bool:
    """判断文本是否包含需要来源支撑的事实断言。"""
    return any(v in text for v in ASSERTION_VERBS)


def has_source(text: str) -> bool:
    """判断文本是否包含来源标注（URL / 引用标记 / 出处声明）。"""
    return any(m in text for m in SOURCE_MARKERS)


def verify(text: str) -> dict[str, str | bool | None]:
    """核验一段文本的来源可溯性。

    返回：
    - status: verified（断言有来源）| unverified（断言缺来源）| not_applicable（无断言）
    - issue: unverified 时给出提示文案
    """
    if not requires_source(text):
        return {"requires_source": False, "has_source": False, "status": "not_applicable", "issue": None}
    if has_source(text):
        return {"requires_source": True, "has_source": True, "status": "verified", "issue": None}
    return {
        "requires_source": True,
        "has_source": False,
        "status": "unverified",
        "issue": "包含需要来源支撑的断言（如增长/下降/达到/排名/占比），但缺少来源标注，建议人工复核",
    }


__all__ = ["ASSERTION_VERBS", "SOURCE_MARKERS", "has_source", "requires_source", "verify"]
