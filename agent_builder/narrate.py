"""人读文本生成器 —— 把「机器输出」翻成「人话」。

背景：工具卡此前直接展示原始 JSON、英文 action 名、``key=value`` 输入与异常原文
（``RuntimeError: AgentError: web_fetch: HTTP 错误: 404``）。本模块是**纯函数**翻译层：

- :func:`describe_step`：步骤 → （中文标题, 人话描述），供分解器写入
  ``Step.title`` / ``Step.description``；
- :func:`summarize_result`：执行结果 → 一句话人话，供编排器写入 ``StepResult.summary``；
- :func:`humanize_error`：异常原文 → 人话原因。

设计约束：
- 只做确定性映射：不调 LLM、无副作用、无 IO、无全局状态；
- 未收录的输入一律**如实回显**（不编造中文名、不猜测失败原因）；
- 原始 ``result`` / ``error`` 仍由调用方保留（供「查看原始数据」与审计），
  本模块只负责**新增**可读文本，不删除任何既有字段。

放在顶层模块而非 ``roles/``：``roles/`` 下新增任何 .py 会被当作新角色参与评分
（见 ``tests/test_evaluation_agents.py``），而本模块需要被 roles 层（分解器）导入。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# action → 中文动作名（覆盖已注册工具 + 计划里常见动作；未收录的原样返回）。
ACTION_TITLES: dict[str, str] = {
    # 检索 / 抓取
    "web_search": "搜索资料",
    "web_fetch": "抓取网页",
    "citation_check": "核对引用",
    "code_search": "搜索代码",
    # 文件
    "file_read": "读取文件",
    "file_list": "列出文件",
    "file_write": "写入文件",
    "file_edit": "修改文件",
    "file_delete": "删除文件",
    "diff_preview": "预览变更",
    # 生成 / 汇总
    "summarize": "汇总摘要",
    "report": "生成报告",
    "code_gen": "生成代码",
    # 方案 / 影响
    "propose": "提出方案",
    "optimize": "优化方案",
    "impact_analyze": "分析影响",
    "assess": "评估风险",
    # 验证 / 观测
    "test_run": "运行测试",
    "sandbox_run": "沙箱执行",
    "plan_validate": "校验计划",
    "metric_collect": "采集指标",
    "audit_log": "记录审计",
    "config_read": "读取配置",
    "change_notify": "通知变更",
    # 数据 / 记忆
    "data_query": "查询数据",
    "memory_read": "读取记忆",
    "memory_write": "写入记忆",
    "memory_forget": "遗忘记忆",
    # 版本 / 审批
    "git_commit": "提交代码",
    "git_log": "查看提交历史",
    "rollback": "回滚变更",
    "approval_request": "申请审批",
    # 评审会
    "council_build_minutes": "汇总评审纪要",
    "council_check_opinion": "核对评审意见",
}

# 输入参数 → 人话标签（按此顺序优先展示，避免把 key=value 原样抛给用户）。
_INPUT_LABELS: tuple[tuple[str, str], ...] = (
    ("path", "文件"),
    ("file", "文件"),
    ("file_path", "文件"),
    ("files", "文件"),
    ("target", "目标"),
    ("url", "地址"),
    ("query", "关键词"),
    ("q", "关键词"),
    ("keyword", "关键词"),
    ("keywords", "关键词"),
    ("test_path", "测试"),
    ("pattern", "匹配模式"),
    ("command", "命令"),
    ("cmd", "命令"),
    ("topic", "主题"),
    ("name", "名称"),
    ("module", "模块"),
    ("entry_id", "条目"),
    ("entry", "条目"),
)

# 动作相关的标签覆盖：同一个 `path` 在「列目录」里是目录、在读写里是文件。
# 实测把 `file_list` 的 path 标成「文件：.」会让人（和模型）误以为那是文件。
_ACTION_PATH_LABELS: dict[str, str] = {
    "file_list": "目录",
}

# 长文本类输入：只说字数，绝不把原文倾倒进摘要。
_TEXT_KEYS: tuple[str, ...] = (
    "content",
    "text",
    "prompt",
    "document",
    "body",
    "code",
    "diff",
    "message",
)

# 结构化结果里的「计数类」字段 → 量词。
_COUNT_KEYS: tuple[tuple[str, str], ...] = (
    ("items", "条结果"),
    ("matches", "处匹配"),
    ("results", "条结果"),
    ("entries", "条记录"),
    ("conclusions", "条结论"),
    ("sources", "条来源"),
    ("rows", "行数据"),
    ("records", "条记录"),
    ("suggestions", "条建议"),
)

# 工具侧统一的截断标记前缀（见 tools/impl 各实现的 MAX_* 截断，如 code_search
# 的「…[已截断，仅显示前 N 条匹配]」）。
_TRUNCATION_PREFIX = "…[已截断"

# 异常原文 → 人话原因（按序匹配，命中即返回）。
_ERROR_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"请求超时|timeout|timed out", re.IGNORECASE), "请求超时"),
    (
        re.compile(
            r"URL 错误|URLError|无法解析|getaddrinfo|nodename|Name or service", re.IGNORECASE
        ),
        "地址无法访问",
    ),
    (re.compile(r"解码失败|UnicodeDecodeError"), "内容编码无法解析"),
    (
        re.compile(r"not recognized|command not found|不是内部或外部命令"),
        "本机缺少所需命令（环境问题）",
    ),
    (re.compile(r"文件已存在且未授权覆盖"), "目标文件已存在，且未授权覆盖"),
    (re.compile(r"无对应工具"), "该动作没有对应工具，无法执行"),
    (re.compile(r"未授权|越权|permission|not allowed", re.IGNORECASE), "未获授权，已被拦截"),
    (re.compile(r"超出工作区|路径穿越|白名单"), "路径超出允许的工作区范围"),
    (re.compile(r"内容超长|超出.*上限"), "内容超出大小上限"),
    (re.compile(r"不存在"), "目标不存在"),
)

# 异常字符串常见前缀（如 ``RuntimeError: AgentError: ``），逐轮剥离。
_ERROR_PREFIX_RE = re.compile(r"^(?:\[stderr\]\s*)?(?:[A-Za-z_]*Error|Exception):\s*")
# 错误码前缀（如 ``E_VALIDATION: ``）：大写蛇形，与工具名前缀互补。
_ERROR_CODE_RE = re.compile(r"^E_[A-Z0-9_]+:\s*")
# 工具名前缀（如 ``web_fetch: ``）：小写蛇形 + 冒号 + 空白，不会误伤 ``http://``。
_TOOL_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]*:\s*")

_MAX_INPUT_PARTS = 4
_MAX_INPUT_VALUE_CHARS = 40
_MAX_ERR_CHARS = 100
# 关键词匹配窗口：只看错误原文**开头的有限字数**。
# 实测（质量探针 run-1）：一次 ``test_run`` 的失败原文是**一条 622 字的单行** ——
# 前 100 字写着真实原因「测试未通过：失败 4 项、错误 1 项（通过 2 项）」，而第 441 字处
# 嵌着某个用例抛出的 ``PermissionError: [WinError 5] 拒绝访问…``。全文匹配时，深处那个词
# 会把整条失败原因劫持成「未获授权，已被拦截」，真实原因被彻底盖掉（模型与用户都被误导）。
_ERROR_HINT_WINDOW = 120


def action_title(action: str) -> str:
    """action → 中文动作名；未收录时**原样返回**（不编造）。"""
    return ACTION_TITLES.get(action, action)


def _char_label(count: int) -> str:
    """字数标签：1000 字以上折成 k（如 ``1.2k 字``）。"""
    if count >= 1000:
        return f"{count / 1000:.1f}k 字"
    return f"{count} 字"


def _short_text(value: Any, limit: int = _MAX_INPUT_VALUE_CHARS) -> str:
    """把任意值压成单行短文本（折叠换行 + 超长截断）。"""
    text = " ".join(str(value).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _short_path(value: Any, limit: int = 30) -> str:
    """路径的短化：**保留尾部**（文件名才是有信息量的那部分）。

    实测（质量探针 run-1）：结论兜底文案写成
    「产出 _sample_backup/quality_probe/…、_sample_backup/quality_probe/…」——
    同一目录下三个不同文件被截成一模一样的三个省略号，等于没说。
    """
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    tail = text[-(limit - 1) :]
    # 尽量对齐到路径分隔符：否则会把目录名切成半截（"…ality_probe/x.py" 很难看）。
    for sep in ("/", "\\"):
        idx = tail.find(sep)
        if 0 <= idx < 12:
            tail = tail[idx:]
            break
    return "…" + tail


def _value_phrase(value: Any) -> str:
    """把单个参数值翻成人话（长文本/列表只说数量，不倾倒原文）。"""
    if isinstance(value, Mapping):
        return f"{len(value)} 项配置"
    if isinstance(value, (list, tuple, set)):
        items = [_short_text(item, 20) for item in list(value)[:3]]
        joined = "、".join(item for item in items if item)
        if not joined:
            return "空列表"
        if len(value) > 3:
            return f"{joined} 等 {len(value)} 项"
        return joined
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    return _short_text(value)


def _is_blank(value: Any) -> bool:
    """空值判定：None / 空串 / 空列表 / 空字典。"""
    return value is None or value == "" or value == [] or value == {}


def describe_inputs(inputs: Any, *, action: str = "") -> list[str]:
    """把 ``step.inputs`` 翻成人话短语列表（``key=value`` 机器格式不再外泄）。

    Args:
        inputs: 步骤输入参数（非 Mapping 时返回空列表，不猜）。
        action: 动作名；给出时对 ``path`` 用动作相关的标签（``file_list`` → 「目录」）。

    Returns:
        人话短语列表，最多 ``_MAX_INPUT_PARTS`` 条；无可用参数时为空列表。
    """
    if not isinstance(inputs, Mapping):
        return []
    path_label = _ACTION_PATH_LABELS.get(action)
    parts: list[str] = []
    used: set[str] = set()
    # 1) 已知键：优先展示，标签人话化。
    for key, label in _INPUT_LABELS:
        if key in used or key not in inputs:
            continue
        if key == "path" and path_label:
            label = path_label
        value = inputs[key]
        if _is_blank(value):
            continue
        parts.append(f"{label}：{_value_phrase(value)}")
        used.add(key)
        if len(parts) >= _MAX_INPUT_PARTS:
            return parts
    # 2) 长文本键：只说字数。
    for key in _TEXT_KEYS:
        if key in used or key not in inputs:
            continue
        text = inputs[key]
        if isinstance(text, str) and text.strip():
            parts.append(f"内容 {_char_label(len(text))}")
            used.add(key)
            if len(parts) >= _MAX_INPUT_PARTS:
                return parts
    # 3) 覆盖标记：只在为真时提示（默认 false 无需展示）。
    if inputs.get("overwrite") is True:
        parts.append("允许覆盖")
    # 4) 其余键：如实回显，不把用户参数藏起来。
    for key, value in inputs.items():
        if key in used or key == "overwrite" or _is_blank(value):
            continue
        parts.append(f"{key}：{_value_phrase(value)}")
        if len(parts) >= _MAX_INPUT_PARTS:
            break
    return parts


def describe_step(action: str, inputs: Any = None) -> tuple[str, str]:
    """把「英文 action + key=value 输入」翻成 ``(中文标题, 人话描述)``。

    Args:
        action: 动作名（工具名）。
        inputs: 步骤输入参数。

    Returns:
        ``(title, description)``：title 为中文动作名；description 为
        ``标题（参数人话…）``，无参数时与标题相同。
    """
    title = action_title(action)
    parts = describe_inputs(inputs, action=action)
    if parts:
        return title, f"{title}（{'；'.join(parts)}）"
    return title, title


def _http_hint(code: str) -> str:
    """HTTP 状态码 → 人话提示。"""
    if code == "404":
        return "页面不存在"
    if code.startswith("4"):
        return "请求被对方拒绝"
    if code.startswith("5"):
        return "对方服务异常"
    return "请求未成功"


def humanize_error(error: Any) -> str:
    """异常原文 → 人话原因（先剥异常/工具名前缀，再按模式映射）。

    Args:
        error: 原始异常文本（可能带 ``RuntimeError: AgentError: web_fetch: `` 等前缀）。

    Returns:
        单行人话原因；无法识别时如实回显清理后的原文（截断），不编造原因。
    """
    if not error:
        return "原因未知"
    text = str(error).splitlines()[0].strip()
    previous = ""
    while previous != text:
        previous = text
        text = _ERROR_PREFIX_RE.sub("", text)
        text = _ERROR_CODE_RE.sub("", text)
        text = _TOOL_PREFIX_RE.sub("", text)
    match = re.search(r"HTTP 错误:\s*(?P<code>\d{3})", text)
    if match:
        code = match.group("code")
        return f"目标返回 HTTP {code}（{_http_hint(code)}）"
    for pattern, hint in _ERROR_HINTS:
        if pattern.search(text[:_ERROR_HINT_WINDOW]):
            return hint
    return _short_text(text, _MAX_ERR_CHARS) or "原因未知"


# 会真正改动磁盘的动作 —— 只有这些才配说「已写入 X」。
_WRITE_ACTIONS: frozenset[str] = frozenset({"file_write", "file_edit", "git_commit", "rollback"})

# 执行类动作 —— 只有它们能构成「**验证证据**」：模型在结论里写「验证通过」不算，
# 系统只认真实跑过的东西。权威列表见 ``evaluation.tool_report.EXEC_TOOLS``；本模块
# 是零依赖纯函数层（无 IO / 无 LLM / 无全局状态），故自持一份，并由
# ``tests/test_narrate.py`` 断言两者同步，避免长期漂移。
_EXEC_ACTIONS: frozenset[str] = frozenset({"sandbox_run", "test_run"})


def _result_sentence(payload: Mapping[str, Any], action: str = "") -> str:
    """从结构化结果里提炼一句话；无法提炼时返回空串（由调用方兜底）。

    ``files_changed`` 描述的是「这一步碰过的路径」，**只读动作**（列出文件 / 读取文件 /
    查询数据…）同样会带上它。所以只有真正的写动作才说「已写入 X」；其余动作直接用结果
    自带的人话描述，避免「列出文件」被写成「已写入 tests」。
    """
    if str(payload.get("status", "")) == "quality_report":
        # data_analyst 的合法产出：样本不足 → 只出质量报告、不下结论。
        return "数据样本不足，只出具质量报告（未下结论）"
    sections = payload.get("sections")
    if isinstance(sections, (list, tuple)) and sections:
        # Summarizer 的 FinalReport：**只在内存里汇总，不落盘**（artifacts 恒为空）。
        # 必须如实说明，否则「生成报告完成」会被读成"报告文件已产出"（实机踩过：
        # 结论区因此写「最终调研报告已由 summarizer 产出」，而工作区里根本没有报告）。
        conclusions = payload.get("conclusions")
        n_conclusions = len(conclusions) if isinstance(conclusions, (list, tuple)) else 0
        pending = list(payload.get("pending_items") or []) + list(
            payload.get("missing_items") or []
        )
        parts = [f"{n_conclusions} 条结论" if n_conclusions else "无结论"]
        if pending:
            parts.append(f"{len(pending)} 项未完成")
        return f"已汇总报告（{'，'.join(parts)}；仅内存汇总，未落盘）"
    files = payload.get("files_changed")
    if isinstance(files, (list, tuple)) and files:
        names = "、".join(_short_text(item, 30) for item in list(files)[:3])
        if len(files) > 3:
            names = f"{names} 等 {len(files)} 个文件"
        desc = payload.get("change_desc")
        desc = _short_text(desc, 40) if isinstance(desc, str) and desc.strip() else ""
        if action in _WRITE_ACTIONS:
            return f"已写入 {names}：{desc}" if desc else f"已写入 {names}"
        # 只读 / 查询类：优先用结果自带的人话描述，缺失才回退「中文动作名：目标」。
        return desc or f"{action_title(action)}：{names}"
    passed = payload.get("passed")
    # 只有真的核对到用例才说「测试通过 N 项」：全 0 意味着这轮压根没测到东西，
    # 说「测试通过 0 项」会把「没测到」伪装成「测试通过」（实机踩过：test_run
    # 的计数此前恒为 0，却一律报成功）；这种情况退到通用文案。
    verified = bool(passed) or bool(payload.get("failed")) or bool(payload.get("error"))
    if isinstance(passed, int) and not isinstance(passed, bool) and verified:
        return f"测试通过 {passed} 项"
    for key, unit in _COUNT_KEYS:
        value = payload.get(key)
        if isinstance(value, (list, tuple)) and value:
            return f"产出 {len(value)} {unit}"
    document = payload.get("document")
    if isinstance(document, str) and document.strip():
        return f"已生成文档（{_char_label(len(document))}）"
    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        return _short_text(summary, 60)
    return ""


def _file_write_sentence(inputs: Any, text: str) -> str:
    """file_write 结果人话化（工具返回 ``wrote <path> (<N> chars)``）。"""
    match = re.match(r"^wrote\s+(?P<path>\S+)\s+\((?P<chars>\d+)\s+chars\)$", text)
    if match:
        return f"已写入 {match.group('path')}（{int(match.group('chars'))} 字）"
    path = inputs.get("path") if isinstance(inputs, Mapping) else None
    if isinstance(path, str) and path.strip():
        return f"已写入 {_short_text(path, 40)}"
    return "已写入文件"


def _file_edit_sentence(inputs: Any, text: str) -> str:
    """file_edit 结果人话化（工具返回 ``edited <path> (第 X 行，替换 N 处)``）。"""
    match = re.match(r"^edited\s+(?P<path>\S+)\s+\((?P<scope>[^)]*)\)", text)
    if match:
        return f"已修改 {match.group('path')}（{match.group('scope')}）"
    path = inputs.get("path") if isinstance(inputs, Mapping) else None
    if isinstance(path, str) and path.strip():
        return f"已修改 {_short_text(path, 40)}"
    return "已修改文件"


def _success_sentence(action: str, title: str, inputs: Any, result: Any) -> str:
    """成功结果的兜底人话（结构化结果已在上一步提炼）。"""
    if isinstance(result, str):
        text = result.strip()
        if action == "file_write":
            return _file_write_sentence(inputs, text)
        if action == "file_edit":
            return _file_edit_sentence(inputs, text)
        if not text:
            return f"{title}完成（无输出）"
        if action == "code_search":
            # 搜索结果的计量单位是「命中多少条」，**不是**「多少字」——
            # 200 条匹配可以轻松几十万字符，报字数毫无信息量（实机：「198.1k 字」）。
            hits = [
                line
                for line in text.splitlines()
                if line.strip() and not line.startswith(_TRUNCATION_PREFIX)
            ]
            clipped = "，已截断" if _TRUNCATION_PREFIX in text else ""
            return f"{title}完成（命中 {len(hits)} 条{clipped}）"
        return f"{title}完成（{_char_label(len(text))}）"
    if isinstance(result, (list, tuple)):
        return f"{title}完成（{len(result)} 项）"
    if result is None:
        return f"{title}完成"
    if isinstance(result, bool):
        return f"{title}完成（{'是' if result else '否'}）"
    return f"{title}完成（{_short_text(result, 30)}）"


def summarize_result(
    *,
    action: str,
    status: str,
    result: Any = None,
    error: Any = None,
    inputs: Any = None,
    retries: int = 0,
) -> str:
    """把一条执行结果翻成一句话人话（``StepResult.summary`` 的唯一来源）。

    Args:
        action: 动作名（工具名）。
        status: 执行状态（done / failed / skipped / pending / pending_approval）。
        result: 原始结果（dict / str / list；**不要求**已序列化）。
        error: 原始异常文本（失败时）。
        inputs: 步骤输入参数（用于补充文件/地址等上下文）。
        retries: 重试次数（>0 时在失败摘要里点明，对应「重试 N 次仍未成功」）。

    Returns:
        单行人话摘要。结构化结果永远不整段倾倒（dict 不再原样展示）。
    """
    title = action_title(action)
    if status == "pending":
        return "尚未执行"
    if status == "skipped":
        return "已跳过（依赖步骤未完成）"
    if status == "pending_approval":
        return f"等待你放行：{title}"
    if status == "failed" or error:
        suffix = f"（已重试 {retries} 次）" if retries > 0 else ""
        return f"{title}失败：{humanize_error(error)}{suffix}"
    if isinstance(result, Mapping):
        sentence = _result_sentence(result, action)
        return sentence or f"{title}完成"
    return _success_sentence(action, title, inputs, result)


# 零产物时给模型结论补的**事实**说明（实机踩过：零产物却写「已完成构建」）。
NO_ARTIFACT_NOTE = "（说明：本任务未产出文件）"

# 「结论在声称交付了东西」的措辞。
# 刻意**收紧**：只认「已+交付动词」紧跟着「交付物名词」的明确组合 ——
# 「已预览完差异，无需改动」「本次只预览了差异，未改动任何文件」这类如实结论
# 不该被加上说明（那不是谎话，只是本来就没有文件产出）。
_CLAIM_DELIVERABLE_RE = re.compile(
    r"已(?:完成|构建|生成|产出|创建|新建|写入|实现|交付|输出|保存)[^。；;]{0,12}"
    r"(?:代码|实现|脚本|程序|文件|报告|文档|模块|测试|系统|工具|项目|agent|Agent)"
)


def _claims_deliverable(text: str) -> bool:
    """结论是否在声称「已产出/已构建了某个交付物」。"""
    return bool(_CLAIM_DELIVERABLE_RE.search(text or ""))


# 结论里出现的「文件路径」与「可运行模块入口」（用于核对是否真的产出过）。
_ANSWER_PATH_RE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}\b")
_ANSWER_MODULE_RE = re.compile(r"-m\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)")


def _unproduced_refs(answer: str, produced: list[str]) -> list[str]:
    """找出结论里引用了、但**本任务并未产出**的文件 / 入口。

    只在「本任务确实产出过的目录」内比对 —— 模型最典型的编造是**给自己产出的包
    凭空补一个入口**：实机 13 轮真的写出了 ``research_agent/{agent,test_agent}.py``
    与 ``README.md``，结论却写「用法：``python -m research_agent.main``」，
    而 main.py 根本不存在。产出目录**之外**的引用（如「参考了 docs/HANDOVER.md」）
    是正常引用，不报警（否则每句话都要误报）。
    """
    normalized = [item.replace("\\", "/") for item in produced]
    produced_set = set(normalized)
    produced_dirs = {item.rsplit("/", 1)[0] for item in normalized if "/" in item}
    if not produced_dirs:
        return []
    candidates: list[str] = list(_ANSWER_PATH_RE.findall(answer or ""))
    # `python -m pkg.module` 是「可运行入口」声明 → 换算成模块文件路径再比。
    for module in _ANSWER_MODULE_RE.findall(answer or ""):
        candidates.append(module.replace(".", "/") + ".py")
    suspects: list[str] = []
    for raw in candidates:
        path = raw.strip("`'\"()（）【】,;：:。、").replace("\\", "/")
        head = path.rsplit("/", 1)[0] if "/" in path else ""
        if head and head in produced_dirs and path not in produced_set and path not in suspects:
            suspects.append(path)
    return suspects


def _unproduced_note(missing: list[str], produced: list[str]) -> str:
    """「结论引用了未产出的文件」的事实说明（列出真实产出，便于对照）。"""
    real = "、".join(_short_path(x, 30) for x in produced[:3]) if produced else "无"
    return (
        f"（说明：本任务未产出 {'、'.join(_short_path(x, 40) for x in missing[:3])}；"
        f"实际产出 {real}）"
    )


# 结论里的**验证声明**：「验证 / 测试 / 校验」与「通过 / 成功」出现在**同一句**里就够了。
# 不卡字间距 —— 实测（首次收敛那轮）真实结论写成
# 「验证证据：1) pytest 运行 tests/test_research_agent.py，5 项全部通过」，
# 中间隔着路径与逗号，卡 6 字以内会漏判、事实行附不上。
# `未通过` / `不通过` 属如实汇报，不算声明（否则如实失败反而被加事实行）。
_CLAIM_VERIFY_RE = re.compile(r"(?:验证|测试|校验)[^。；\n]*(?<![未不])(?:通过|成功)")


def _claims_verification(text: str) -> bool:
    """结论是否在声称「验证通过 / 测试成功」。"""
    return bool(_CLAIM_VERIFY_RE.search(text or ""))


def _verification_fact(results: list[Mapping[str, Any]]) -> str:
    """后端核对出的**验证证据** —— 替代模型自己写的「验证通过」。

    模型可以在结论里写「验证通过」，但那只是它的自述：实机产出的 ``verify()``
    就是 ``bool(self.plan.steps) and bool(self.code_artifact)`` 这种**自证式**判断
    （检查容器非空即算通过）。这里只认**真实运行过**的执行类动作及其真实状态：
    跑过几次、成了几次；一次都没跑过就如实说明「验证」只是代码内的自述。
    """
    runs = [item for item in results if str(item.get("action") or "") in _EXEC_ACTIONS]
    if not runs:
        return "（系统核对：本次未真实运行任何测试或命令，「验证」仅为代码内的自述）"
    done = sum(1 for item in runs if str(item.get("status") or "") == "done")
    detail = f"真实运行 {len(runs)} 次，成功 {done} 次"
    if len(runs) - done:
        detail += f"、失败 {len(runs) - done} 次"
    return f"（系统核对：{detail}）"


def _collect_artifacts(results: list[Mapping[str, Any]]) -> list[str]:
    """汇总**本任务产出的文件**（去重、保序、**返回完整路径**）。

    ⚠️ 必须返回完整名：它既用于展示、也用于**事实核对**（``_unproduced_refs`` 拿它判断
    "结论引用的文件是否真产出过"）。一旦在这里截断，结论里写全路径就对不上 ——
    实测（用户任务第 4 次复现）：结论明明产出了 ``paper_survey_agent/test_agent.py``，
    却被告知「本任务未产出」；**假警报比不说更伤信任**。短化只发生在展示处
    （``_unproduced_note`` 与兜底结论）。
    """
    artifacts: list[str] = []
    for item in results:
        files = item.get("artifacts")
        if not isinstance(files, (list, tuple)):
            continue
        for name in files:
            if name and name not in artifacts:
                artifacts.append(name)
    return artifacts


def conclude(
    *,
    final_answer: str = "",
    execution_results: Any = None,
    status: str = "",
    stopped: bool = False,
) -> str:
    """产出「结论区」的一句话结论（跑完总要有句人话说清做成了什么）。

    优先级：
    1. ``final_answer`` —— agentic 模式下模型以 ``kind=final`` 收尾时说的话，
       这是**模型自己的结论**，最有信息量，直接采用；但要过两道**事实核对**并补说明：
       ① **零产物却在声称交付** → :data:`NO_ARTIFACT_NOTE`（模型可能把工作区里原有文件
       说成自己的产出）；② **引用了本任务没产出过的文件 / 运行入口** → 列出真实产出
       （实机：凭空写「用法：python -m pkg.main」而 main.py 并不存在）；
       ③ **声称「验证通过」** → 附上后端核对出的真实运行证据，因为那句话很可能只是
       代码内的自证（实机产出的 ``verify()`` 检查容器非空即算通过）；
    2. 从执行结果**如实汇总** —— 固定工作流没有「模型收尾」这一步，不该因此没有结论；
    3. 两者都拿不到（没跑 / 无产出）→ 返回空串，由前端决定不渲染结论块。

    刻意**不编造**：没有依据时宁可没有结论，也不写「任务已完成」这种无信息量的话。

    ``status`` / ``stopped`` 只影响**兜底汇总的措辞**：任务没收敛（用户暂停 / 预算耗尽 /
    空转 / 决策非法）时不能说「执行完成」—— 否则与顶栏的「已暂停 / 已停下」自相矛盾
    （实机在「用户点中断」后复现）。

    Args:
        final_answer: 模型收敛时给出的结论（agentic）。
        execution_results: 执行结果列表（``StepResult`` 形状的 dict）。
        status: 任务状态字符串；``interrupted`` 时按「已中断」措辞。
        stopped: 循环是否**非收敛停下**（与前端「已停下」同口径）；此时即便步骤都成功，
            也只说「未收尾」。默认 False（不知情时不改措辞）。

    Returns:
        单行人话结论；无依据时为空串。
    """
    answer = (final_answer or "").strip()
    results = [item for item in (execution_results or []) if isinstance(item, Mapping)]
    if answer:
        produced = _collect_artifacts(results)
        # 模型自己的结论最有信息量，但不能只凭它一面之词，三处**事实由系统补**：
        # ① 跑过步骤却一个文件都没产出、结论又在声称交付 → 补说明（实机：零产物写「已完成构建」）；
        # ② 结论引用了本任务没产出过的文件 / 运行入口 → 补说明（实机：产出了
        #    research_agent/{agent,test_agent}.py + README.md，却写「用法：python -m
        #    research_agent.main」，而 main.py 根本不存在）；
        # ③ 结论声称「验证通过 / 测试成功」→ 附上**后端核对出的真实运行证据**，
        #    因为那句「验证通过」很可能只是代码里的自证（实机产出的 verify() 是
        #    bool(steps) and bool(code)，检查容器非空即算通过）。
        notes: list[str] = []
        if results and not produced and _claims_deliverable(answer):
            notes.append(NO_ARTIFACT_NOTE)
        missing = _unproduced_refs(answer, produced)
        if missing:
            notes.append(_unproduced_note(missing, produced))
        if _claims_verification(answer):
            notes.append(_verification_fact(results))
        return answer + "".join(notes) if notes else answer
    if not results:
        return ""
    artifacts = _collect_artifacts(results)
    total = len(results)
    done = sum(1 for item in results if item.get("status") == "done")
    failed = sum(1 for item in results if item.get("status") == "failed")
    waiting = sum(1 for item in results if item.get("status") == "pending_approval")
    produced = f"，产出 {'、'.join(_short_path(x, 30) for x in artifacts[:3])}" if artifacts else ""
    if waiting:
        return f"执行暂停：{total} 步中 {waiting} 步等待你放行（已完成 {done} 步）{produced}"
    # 用户主动暂停：顶栏写着「已暂停」，结论不能自说「执行完成」。
    if status == "interrupted":
        tail = f"、失败 {failed} 步" if failed else ""
        return f"已中断：共 {total} 步，已完成 {done} 步{tail}{produced}"
    # 非收敛停下（预算耗尽 / 空转 / 决策非法）：顶栏写着「已停下」，
    # 结论必须先说「已停下」——**包括失败步数不为 0 的情况**（此前被下面的
    # 「有失败」分支抢先，实机出现「顶栏已停下、结论只说执行了 11 步」的自相矛盾）。
    if stopped:
        if failed:
            return f"已停下：共 {total} 步，成功 {done} 步、失败 {failed} 步{produced}"
        if done == total:
            return f"已停下：{total} 步均成功，但任务未收尾{produced}"
        return f"已停下：{total} 步中已完成 {done} 步{produced}"
    if failed:
        return f"执行了 {total} 步：成功 {done} 步、失败 {failed} 步{produced}"
    if done == total:
        return f"执行完成：{total} 步全部成功{produced}"
    return f"执行结束：{total} 步中已完成 {done} 步{produced}"


__all__ = [
    "ACTION_TITLES",
    "NO_ARTIFACT_NOTE",
    "action_title",
    "conclude",
    "describe_inputs",
    "describe_step",
    "humanize_error",
    "summarize_result",
]
