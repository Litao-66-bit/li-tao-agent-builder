# 工具清单（Tool Inventory）

Agent Builder 已注册工具一览。所有工具经 `ToolGatekeeper` 唯一出口执行：
权限校验 → 高风险审批 → 沙箱/URL 校验 → 审计日志 → registry 分发。

> **真源约定**：工具清单以 `registry.list_tools()` 为准；角色授权与审批门以
> `agent_builder/tools/permissions.py` 为准。本文档的「角色」行需与之一致，
> 由 `agent_builder.evaluation.tool_report` 的文档漂移检查守住。

## operator 角色可用工具

| 工具 | 用途 | 风险 | 审批 | 数据边界 | 超时 |
|---|---|---|---|---|---|
| `file_read` | 读文件内容（UTF-8） | low | 否 | 白名单目录（realpath 校验） | 10s |
| `file_list` | 列目录 | low | 否 | 白名单目录 | 10s |
| `code_search` | 代码内搜索（ripgrep） | low | 否 | 白名单目录 | 30s |
| `file_write` | 写/覆盖文件 | medium | ⚠ 是 | 白名单目录；所有写均需审批 | 10s |
| `file_edit` | 局部修改文件（精确替换，无需整份重写） | medium | ⚠ 是 | 白名单目录；所有修改均需审批 | 10s |
| `file_delete` | 删除文件（不递归） | high | ⚠ 是 | 白名单目录；所有删除均需审批 | 10s |
| `web_fetch` | 抓取页面/文档 | low | 否 | http/https；禁私有网段（防 SSRF） | 30s |
| `web_search` | 网页搜索（DuckDuckGo IA） | low | 否 | 固定 DDG API；禁内网 | 30s |
| `citation_check` | 校验引用来源存在性 | low | 否 | URL + DOI；URL 经 url_guard 校验 | 120s |
| `sandbox_run` | 沙箱内执行命令 | medium | 否 | 工作目录沙箱；禁网络命令 + Linux netns | 60s |
| `test_run` | 跑 pytest / 抓文档 | medium | 否 | 路径沙箱 or URL 校验（自动识别） | 120s |
| `data_query` | 读 CSV/JSON 数据 | low | 否 | 白名单目录（path 沙箱） | 15s |
| `plan_validate` | 校验步骤 DAG（格式/环依赖） | low | 否 | 纯计算，无 IO | 10s |
| `memory_read` | 检索记忆（短期/长期） | low | 否 | 只读记忆存储 | 10s |
| `audit_log` | 写审计日志 | low | 否 | 只追加不可篡改 | 10s |
| `metric_collect` | 采集运行指标 | low | 否 | 只读审计日志 | 10s |
| `config_read` | 读架构配置 | low | 否 | 只读 permissions/registry | 10s |
| `diff_preview` | 生成变更 diff 预览 | low | 否 | 纯计算（difflib） | 10s |
| `approval_request` | 发起审批请求 | low | 否 | 框架级 | 10s |
| `change_notify` | 变更通知 | low | 否 | 框架级 | 10s |
| `git_log` | 查询版本历史 | low | 否 | 只读（git log） | 15s |

## memory_manager 角色可用工具

| 工具 | 用途 | 风险 | 审批 | 数据边界 | 超时 |
|---|---|---|---|---|---|
| `memory_read` | 检索记忆（短期/长期） | low | 否 | 只读记忆存储 | 10s |
| `memory_write` | 写入记忆 | low | 否 | 记忆存储；敏感信息 base64 编码 | 10s |
| `memory_forget` | 遗忘/清理过期记忆 | low | 否 | 记忆存储 | 10s |

## sub_architect 角色可用工具

| 工具 | 用途 | 风险 | 审批 | 数据边界 | 超时 |
|---|---|---|---|---|---|
| `git_commit` | 提交版本 | high | ⚠ 是 | 副架构专用（git add+commit） | 30s |
| `rollback` | 回滚版本 | high | ⚠ 是 | 副架构专用（git reset --hard） | 30s |
| `git_log` | 查询版本历史 | low | 否 | 只读（git log） | 15s |

## 安全机制

### 路径沙箱（文件类工具）
`file_read` / `file_list` / `file_write` / `file_delete` / `file_edit` / `code_search` / `data_query` 的 `path` 参数，
以及 `git_commit` / `rollback` / `git_log` 的 `repo_path` 参数，
经 `ToolGatekeeper._check_sandbox_path` 校验：`Path.resolve()` 必须落在 `WORKSPACE_DIR` 内，
防止路径穿越越权读写系统目录。

### 可选路径校验（sandbox_run）
`sandbox_run` 的 `path` 参数（可选）经 `ToolGatekeeper._check_sandbox_path_optional` 校验：
存在则校验沙箱，不存在则跳过（用默认工作目录）。

### 混合校验（test_run）
`test_run` 的 `target` 参数经 `ToolGatekeeper._check_target_safety` 混合校验：
- 以 `http://` / `https://` 开头 → URL 安全校验（url_guard）；
- 否则 → 路径沙箱校验（必须落在 `WORKSPACE_DIR` 内）。

### URL 安全校验（web 类工具）
`web_fetch` 的 `url` 参数经 `ToolGatekeeper._check_url_safety` → `url_guard.validate_url`：
1. scheme 仅允许 `http` / `https`；
2. 阻断云元数据端点 `169.254.169.254`；
3. hostname 解析为 IP 后禁止私有/保留/环回/链路本地/组播网段；
4. 可选域名白名单（默认空 = 允许所有公共域名）。

`web_search` 的 URL 在实现内部构造（DuckDuckGo 固定 API），门卫跳过 URL 校验。
`citation_check` 的 URL 在实现内部调 `url_guard.validate_url`（双层防护）。

### 记忆存储加密（memory 类工具）
`memory_write` 的 `sensitive=true` 时，content 经 `memory_store.encode_sensitive` 做 base64 编码后写入存储文件，
存储层不落明文（防文件被直接读取时泄露敏感信息）。`memory_read` 读取时解码显示原文，并加 `[SENSITIVE]` 标记。
`memory_write` / `memory_forget` 仅授权 `memory_keeper` / `memory_manager` 角色（`operator` 不可写/清记忆，最小权限原则）。

### 高风险审批
审批门由 `permissions.py` 的 `high_risk_tools` 决定（**不是** `ToolSpec.risk_level`，后者是声明性元数据）。
`file_write` / `file_edit` / `file_delete` 列入 `operator` / `doc_worker` / `code_worker` 的 `high_risk_tools`
（`file_delete` 目前仅授权 `operator`），`git_commit` / `rollback` 列入 `gatekeeper` / `sub_architect`。

**授权通道**：编排层对高风险步骤一律置 `Approval(required=True)`，**仅当被显式授权**时才附
`granted_by`；否则门卫拒绝（`E_PERMISSION`，永不重试）。授权来源是
`POST /tasks/{id}/approve` 请求体的 `approved_tools`（逐工具列表）——**省略该字段 = 不授权任何
高风险工具**（安全默认）。`/plan` 与 `GET /tasks/{id}` 均返回 `high_risk_actions`，供前端提示
并选择要授权的工具。

### 参数归一化（registry 边界，宽松）
编排层传入的是**分解器产出的自由 `inputs`**，其键名未必等于工具签名。`registry.execute`
在执行前按 `ToolSpec.parameters` 归一化，避免「多一个键」直接崩在 Python 签名上
（历史报错形如 `list_dir() got an unexpected keyword argument 'pattern'`）：

1. 已声明的键 → 保留；
2. 未声明但命中 `ARG_ALIASES`（如 `query`→`pattern`、`file`→`path`）、且目标已声明且未被占用 → 重命名；
3. 其余未声明键 → 丢弃（以 WARNING 记日志，含工具名与被丢弃的键）。

归一化在**门卫之前**执行，因此别名后的 `path` 仍会被 `_check_sandbox_path` 校验，
且审计记录与实际执行参数一致；归一化**不做安全校验**、**不替代必填校验**。缺必填参数
时抛 `E_VALIDATION`（错误信息附带允许的参数名）。`additionalProperties: false` 仍是
声明性契约，由 `agent_builder.evaluation.tool_report` 与 `tests/test_evaluation_tools.py` 守住。

> **未注册工具**（模型臆造的工具名）由**编排层在调用工具前**拒绝：`E_VALIDATION`
> （`retryable=False`），报错附可用工具清单，不重派也不进入归一化。

## 工具规格详情

### file_read
- **参数**：`path` (string, required)
- **输出**：文件文本内容；超过 200,000 字符截断
- **错误**：`E_VALIDATION`（不存在/非文件/二进制）、`E_TOOL`（读取失败）

### file_list
- **参数**：`path` (string, required)
- **输出**：每行 `[DIR] name/` 或 `[FILE] name (N bytes)`；超过 500 条目截断
- **错误**：`E_VALIDATION`（不存在/非目录）、`E_TOOL`（列目录失败）

### code_search
- **参数**：`path` (string, required)、`pattern` (string, required)、`max_results` (int, default 200)
- **输出**：每行 `file:line:content`；超过 max_results 截断
- **依赖**：系统需安装 ripgrep（`rg`）
- **错误**：`E_VALIDATION`（参数非法）、`E_TOOL`（rg 不可用/超时/执行错误）

### file_write
- **参数**：`path` (string, required)、`content` (string, required)、`overwrite` (bool, default false)
- **输出**：`wrote <path> (<N> chars)`
- **审批**：所有写操作均需审批（high_risk_tools）
- **覆盖语义**：目标文件已存在时必须显式传 `overwrite=true`，否则拒绝（`E_VALIDATION`，**不可重试**；错误信息自带「如需覆盖请传 `overwrite=true`」提示）
- **错误**：`E_VALIDATION`（参数非法/文件已存在未授权覆盖/内容超长）、`E_TOOL`（写入失败）

### file_edit
- **参数**：`path` (string, required)、`old_string` (string, required)、`new_string` (string, required)、`replace_all` (bool, default false)
- **输出**：`edited <path> (第 X-Y 行，替换 N 处)` + **改后全文符号轮廓**（让模型当场看到新接口）
- **审批**：所有修改均需审批（high_risk_tools）
- **唯一性**：`old_string` 在文件里必须**只出现一次**；出现多次时要么补足上下文让它唯一，要么显式传 `replace_all=true`（否则拒绝：`E_VALIDATION`，**不可重试**）
- **为什么需要它**：只有 `file_write`（整份重写）时，改一个函数签名也要重写 16.6k 字符，模型会退化成发 `content="PLACEHOLDER"`（被占位符护栏拦下）然后干脆不写、转去反复读文件（实测：17 步 0 产物 `stagnant`）
- **换行**：以 `newline=""` 读写，**原样保留**文件既有换行（不会把 CRLF 整份改写成 LF）
- **错误**：`E_VALIDATION`（参数非法 / 找不到 old_string / 出现多次未开 replace_all / 新旧相同 / 内容超长）、`E_TOOL`（读写失败）

### file_delete
- **参数**：`path` (string, required)
- **输出**：`deleted <path>`
- **审批**：所有删除均需审批（high_risk_tools）
- **限制**：仅删除常规文件；**目标是目录一律拒绝**（不提供递归删除，避免误删整棵目录树）
- **错误**：`E_VALIDATION`（path 为空/文件不存在/目标是目录）、`E_TOOL`（删除失败）

### web_fetch
- **参数**：`url` (string, required)、`timeout` (float, default 20s)、`max_chars` (int, default 100,000)
- **输出**：页面文本内容；超过 max_chars 截断
- **错误**：`E_VALIDATION`（参数非法）、`E_TOOL`（HTTP/网络/解码错误）、`E_PERMISSION`（URL 安全校验失败）

### web_search
- **参数**：`query` (string, required)、`max_results` (int, default 10, 上限 50)
- **输出**：每行 `[N] title — abstract (url)`；超过 max_results 截断
- **API**：DuckDuckGo Instant Answer API（免 key）
- **错误**：`E_VALIDATION`（参数非法）、`E_TOOL`（HTTP/网络/JSON 解析错误）

### citation_check
- **参数**：`sources` (array of strings, required)、`timeout` (float, default 15s)
- **输出**：每行 `[OK/FAIL/SKIP] source — 详情`；自动识别 URL / DOI 格式
- **校验**：URL 经 `url_guard.validate_url`（实现内部双层防护）；DOI 查询 doi.org
- **错误**：`E_VALIDATION`（sources 为空/超限/timeout 非法）

### sandbox_run
- **参数**：`command` (string, required)、`path` (string, optional)、`timeout` (float, default 30s)
- **输出**：命令 stdout + stderr；超过 50,000 字符截断
- **解释器 PATH**：子进程 `PATH` 前置**当前解释器目录**（`Path(sys.executable).parent`）——
  后端是用全路径解释器启动的，它的 PATH 里没有 `python`，不补就直接「'python' is not recognized」
- **退出码**：非零 = **失败**（`E_TOOL`，错误信息里带上完整 stdout/stderr）——
  此前只看输出、不看退出码，失败被当成成功，空转检测因此失效、一路烧到预算上限
- **网络阻断**：命令黑名单（curl/wget/nc/ssh 等）+ 代理环境变量清理 + Linux `unshare(CLONE_NEWNET)`
- **错误**：`E_VALIDATION`（command 为空/timeout 非法/含网络命令）、`E_TOOL`（执行失败/超时/**退出码非零**）、`E_PERMISSION`（path 超出沙箱）

### test_run
- **参数**：`target` (string, required)、`timeout` (float, default 60s)
- **输出**：pytest 输出或页面文本；超过 50,000 字符截断
- **自动识别**：`http://` / `https://` 开头 → 抓取文档；否则 → 用**当前解释器**跑
  `python -m pytest target -v --tb=short`（不依赖 PATH 里的 `pytest`／`python`——
  本机依赖是 `pip --target .deps` 装的，两者都不在 PATH）
- **风险**：medium（会执行代码，与 `sandbox_run` 同级；未列入任何角色的 `high_risk_tools`，故无需审批）
- **错误**：`E_VALIDATION`（target 为空/timeout 非法）、`E_TOOL`（pytest 不可用/超时/抓取失败）、`E_PERMISSION`（路径超沙箱/URL 安全校验失败）

### data_query
- **参数**：`path` (string, required)、`limit` (int, default 100, 上限 1000)
- **输出**：CSV → 制表符分隔行；JSON → 格式化文本（数组截断到 limit 条）
- **格式**：自动检测 `.csv` / `.json` 后缀
- **错误**：`E_VALIDATION`（path 为空/limit 非法/文件不存在/格式不支持）、`E_TOOL`（读取/解析失败）

### plan_validate
- **参数**：`steps` (array, required)、`order` (array of strings, required)、`parallel_groups` (array of arrays, optional)
- **输出**：`DAG 校验通过：N 个步骤，M 个并行组，无环依赖`
- **校验**：步骤格式（id/action 非空、id 不重复、depends_on 为列表）→ 引用完整性（order/parallel_groups/depends_on 引用的步骤存在）→ 环检测（DFS 三色标记法）
- **错误**：`E_VALIDATION`（steps/order 为空/超限/格式非法/引用缺失/DAG 含环）
- **角色**：conductor、operator、router、scheduler

### council_check_opinion
- **参数**：`role` (string, required)、`stance` (enum support/oppose/neutral, required)、`content` (string, required)、`evidence` (array of strings, optional)、`round_index` (int, default 1, 上限 5)
- **输出**：规范化意见字典（role/stance/content/evidence/round_index/content_chars/sanitized）
- **校验**：角色名非空且不含空白 → 立场合法 → 正文非空且 ≤2000 字符 → 证据 ≤20 条且单条 ≤300 字符 → 轮次 1–5 → 剥离疑似注入短行（清洗后为空则拒绝）
- **错误**：`E_VALIDATION`（角色 / 立场 / 正文 / 证据 / 轮次非法或疑似仅含注入指令）
- **角色**：conductor

### council_build_minutes
- **参数**：`topic` (string, required)、`opinions` (array of objects, required)、`absent` (array of strings, optional)
- **输出**：纪要字典（topic/participants/absent/support/oppose/neutral/agreements/disagreements/unresolved/suggested_decision/opinion_count）
- **收敛规则**：无反对 → 「可推进」；反对多于支持 → 「建议暂缓」；其余 → 「建议推进前先裁决」；支持与反对并存时双方列入分歧点，反对与中立进未决项
- **错误**：`E_VALIDATION`（议题为空或超长 / 无任何意见 / 意见条数或单条非法 / 缺席名单非法）
- **角色**：conductor

### memory_read
- **参数**：`query` (string, default "")、`scope` (enum short/long/all, default all)、`limit` (int, default 10, 上限 100)
- **输出**：每行 `[scope] key — content (ts)`；敏感条目加 `[SENSITIVE]` 前缀并解码显示原文；无匹配返回 `(no memory found)`
- **存储**：JSON 文件（`<当前工作区>/.agent-memory/memory.json`，可通过 `AGENT_MEMORY_FILE` 环境变量覆盖）
- **错误**：`E_VALIDATION`（scope/limit 非法）、`E_TOOL`（存储读写失败）
- **角色**：auditor、code_worker、conductor、data_analyst、decomposer、doc_worker、fact_checker、gatekeeper、historian、impact_analyzer、memory_keeper、memory_manager、operator、proposer、scheduler、searcher、summarizer、test_runner

### memory_write
- **参数**：`key` (string, required)、`content` (string, required)、`scope` (enum short/long, default long)、`sensitive` (bool, default false)
- **输出**：`stored <key> (<scope>, sensitive=<bool>)`
- **加密**：`sensitive=true` 时 content base64 编码存储（存储层不落明文）
- **覆盖**：同 scope 内同 key 的旧条目自动覆盖
- **错误**：`E_VALIDATION`（key/content 为空/scope 非法/content 超长）、`E_TOOL`（存储读写失败）
- **角色**：historian、memory_keeper、memory_manager、summarizer

### memory_forget
- **参数**：`key` (string, default "")、`scope` (enum short/long/all, default all)、`expired_only` (bool, default true)
- **输出**：`forgot <N> entries`
- **清理规则**：
  - 指定 `key`：只删该 key 的条目（忽略 expired_only）
  - 未指定 `key` + `expired_only=true`：只清过期的 short 记忆（超过 `DEFAULT_SHORT_TTL`=3600s）；long 记忆无 TTL 不清理
  - 未指定 `key` + `expired_only=false`：清空该 scope 全部
  - 无时间戳/时间戳损坏的条目不清理（数据保护）
- **错误**：`E_VALIDATION`（scope 非法）、`E_TOOL`（存储读写失败）
- **角色**：memory_keeper、memory_manager（operator 不可调用）

### audit_log
- **参数**：`role` (string, required)、`action` (string, required)、`detail` (string, default "")、`correlation_id` (string, default "")
- **输出**：`logged <id>`（如 `logged a-000001`）
- **存储**：JSON 文件（`<当前工作区>/.agent-audit/audit.json`，可通过 `AGENT_AUDIT_FILE` 环境变量覆盖）
- **只追加不可篡改**：`audit_store.append_audit` 只追加，不提供修改/删除接口
- **correlation_id**：为空时自动用当前上下文的 correlation_id（由 registry 通过 contextvars 传递）
- **错误**：`E_VALIDATION`（role/action 为空/detail 超长）、`E_TOOL`（存储读写失败）
- **角色**：auditor、code_worker、conductor、data_analyst、decomposer、doc_worker、fact_checker、gatekeeper、historian、impact_analyzer、memory_keeper、operator、proposer、router、scheduler、searcher、summarizer、test_runner

### metric_collect
- **参数**：`scope` (enum summary/by_action/by_role/by_status, default summary)、`window` (int, default 0=全部历史)
- **输出**：
  - `summary`：`total: N` + top-3 actions/roles
  - `by_action`/`by_role`/`by_status`：逐项计数 `field: count`
- **数据源**：audit_store 的审计日志（只读聚合）
- **时间窗口**：`window>0` 只统计最近 N 秒的条目（按 `ts` 过滤）
- **错误**：`E_VALIDATION`（scope/window 非法）、`E_TOOL`（审计日志读取失败）
- **角色**：auditor、operator、proposer、router

### config_read
- **参数**：`section` (enum roles/tools/all, default all)
- **输出**：
  - `roles`：角色权限矩阵（allowed_tools / high_risk_tools / notes）
  - `tools`：工具规格（risk / timeout / cost / roles）
  - `all`：roles + tools
- **数据源**：`permissions.get_default_role_perms()` + `registry.list_tools()`（只读，无 IO）
- **错误**：`E_VALIDATION`（section 非法）
- **角色**：auditor、conductor、decomposer、impact_analyzer、operator、proposer、router、scheduler

### diff_preview
- **参数**：`old_content` (string, required)、`new_content` (string, required)、`context` (int, default 3)、`label` (string, default "")
- **输出**：unified diff 文本（`---/+++/-/+` 格式）；无差异返回 `(no differences)`
- **计算**：`difflib.unified_diff`，纯计算无 IO
- **label**：显示为 `<label> (old)` / `<label> (new)`；空则 `(old)` / `(new)`
- **错误**：`E_VALIDATION`（两侧都空/内容超长/context 为负）
- **角色**：impact_analyzer、operator、proposer

### git_commit
- **参数**：`repo_path` (string, required)、`message` (string, required)、`files` (array, 可选，空则 git add -A)
- **输出**：`committed <hash>`
- **执行**：`git_ops.run_git` 执行 `git add` + `git commit` + `git rev-parse HEAD`
- **安全**：高风险需审批 + repo_path 沙箱校验（门卫 + git_ops 二次校验）
- **错误**：`E_VALIDATION`（message 为空/超长）、`E_TOOL`（git 命令失败）
- **角色**：gatekeeper、sub_architect（operator 不可调用）

### rollback
- **参数**：`repo_path` (string, required)、`target` (string, required，commit hash 或 ref)
- **输出**：`rolled back: <old8> -> <new8>`
- **执行**：`git_ops.run_git` 执行 `git rev-parse HEAD` + `git reset --hard <target>` + `git rev-parse HEAD`
- **安全**：高风险需审批 + repo_path 沙箱校验
- **错误**：`E_VALIDATION`（target 为空）、`E_TOOL`（git 命令失败）
- **角色**：gatekeeper、sub_architect（operator 不可调用）

### git_log
- **参数**：`repo_path` (string, required)、`limit` (int, default 10)、`oneline` (bool, default true)
- **输出**：git log 文本；无 commit 返回 `(no commits)`
- **执行**：`git_ops.run_git` 执行 `git log -n<limit> [--oneline]`
- **安全**：只读 + repo_path 沙箱校验（limit 上限 100）
- **错误**：`E_VALIDATION`（limit 非正）、`E_TOOL`（git 命令失败）
- **角色**：historian、operator、sub_architect

### approval_request
- **参数**：`tool_call_id` (string, required)、`reason` (string, required)、`requested_role` (string, 可选)
- **输出**：`approval requested: apr-<ts>-<tcid8>`
- **框架级**：纯记录生成，无 IO，无审批
- **错误**：`E_VALIDATION`（tool_call_id/reason 为空/reason 超长）
- **角色**：conductor、operator

### change_notify
- **参数**：`target` (string, required)、`change_type` (enum code/config/doc/test/other, required)、`summary` (string, required)
- **输出**：`notified <target>: ntf-<ts>-<target8>`
- **框架级**：纯记录生成，无 IO，无审批
- **错误**：`E_VALIDATION`（target/summary 为空/change_type 非法/summary 超长）
- **角色**：conductor、historian、operator
