# 工具清单（Tool Inventory）

Agent Builder 已注册工具一览。所有工具经 `ToolGatekeeper` 唯一出口执行：
权限校验 → 高风险审批 → 沙箱/URL 校验 → 审计日志 → registry 分发。

## operator 角色可用工具

| 工具 | 用途 | 风险 | 审批 | 数据边界 | 超时 |
|---|---|---|---|---|---|
| `file_read` | 读文件内容（UTF-8） | low | 否 | 白名单目录（realpath 校验） | 10s |
| `file_list` | 列目录 | low | 否 | 白名单目录 | 10s |
| `code_search` | 代码内搜索（ripgrep） | low | 否 | 白名单目录 | 30s |
| `file_write` | 写/覆盖文件 | medium | ⚠ 是 | 白名单目录；所有写均需审批 | 10s |
| `web_fetch` | 抓取页面/文档 | low | 否 | http/https；禁私有网段（防 SSRF） | 30s |
| `web_search` | 网页搜索（DuckDuckGo IA） | low | 否 | 固定 DDG API；禁内网 | 30s |
| `citation_check` | 校验引用来源存在性 | low | 否 | URL + DOI；URL 经 url_guard 校验 | 120s |
| `sandbox_run` | 沙箱内执行命令 | medium | 否 | 工作目录沙箱；禁网络命令 + Linux netns | 60s |
| `test_run` | 跑 pytest / 抓文档 | low | 否 | 路径沙箱 or URL 校验（自动识别） | 120s |
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
`file_read` / `file_list` / `file_write` / `file_edit` / `code_search` / `data_query` 的 `path` 参数，
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
`memory_write` / `memory_forget` 仅授权 `memory_manager` 角色（`operator` 不可写/清记忆，最小权限原则）。

### 高风险审批
`file_write` 列入 `operator.high_risk_tools`，所有写操作必须 `approval.granted_by` 非空，
否则门卫拒绝（E_PERMISSION，永不重试）。

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
- **错误**：`E_VALIDATION`（参数非法/文件已存在未授权覆盖/内容超长）、`E_TOOL`（写入失败）

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
- **网络阻断**：命令黑名单（curl/wget/nc/ssh 等）+ 代理环境变量清理 + Linux `unshare(CLONE_NEWNET)`
- **错误**：`E_VALIDATION`（command 为空/timeout 非法/含网络命令）、`E_TOOL`（执行失败/超时）、`E_PERMISSION`（path 超出沙箱）

### test_run
- **参数**：`target` (string, required)、`timeout` (float, default 60s)
- **输出**：pytest 输出或页面文本；超过 50,000 字符截断
- **自动识别**：`http://` / `https://` 开头 → 抓取文档；否则 → 跑 `pytest target -v --tb=short`
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
- **角色**：operator

### memory_read
- **参数**：`query` (string, default "")、`scope` (enum short/long/all, default all)、`limit` (int, default 10, 上限 100)
- **输出**：每行 `[scope] key — content (ts)`；敏感条目加 `[SENSITIVE]` 前缀并解码显示原文；无匹配返回 `(no memory found)`
- **存储**：JSON 文件（`~/.li-tao-agent/memory.json`，可通过 `AGENT_MEMORY_FILE` 环境变量覆盖）
- **错误**：`E_VALIDATION`（scope/limit 非法）、`E_TOOL`（存储读写失败）
- **角色**：operator（只读）、memory_manager

### memory_write
- **参数**：`key` (string, required)、`content` (string, required)、`scope` (enum short/long, default long)、`sensitive` (bool, default false)
- **输出**：`stored <key> (<scope>, sensitive=<bool>)`
- **加密**：`sensitive=true` 时 content base64 编码存储（存储层不落明文）
- **覆盖**：同 scope 内同 key 的旧条目自动覆盖
- **错误**：`E_VALIDATION`（key/content 为空/scope 非法/content 超长）、`E_TOOL`（存储读写失败）
- **角色**：memory_manager（专用，operator 不可调用）

### memory_forget
- **参数**：`key` (string, default "")、`scope` (enum short/long/all, default all)、`expired_only` (bool, default true)
- **输出**：`forgot <N> entries`
- **清理规则**：
  - 指定 `key`：只删该 key 的条目（忽略 expired_only）
  - 未指定 `key` + `expired_only=true`：只清过期的 short 记忆（超过 `DEFAULT_SHORT_TTL`=3600s）；long 记忆无 TTL 不清理
  - 未指定 `key` + `expired_only=false`：清空该 scope 全部
  - 无时间戳/时间戳损坏的条目不清理（数据保护）
- **错误**：`E_VALIDATION`（scope 非法）、`E_TOOL`（存储读写失败）
- **角色**：memory_manager（专用，operator 不可调用）

### audit_log
- **参数**：`role` (string, required)、`action` (string, required)、`detail` (string, default "")、`correlation_id` (string, default "")
- **输出**：`logged <id>`（如 `logged a-000001`）
- **存储**：JSON 文件（`~/.li-tao-agent/audit.json`，可通过 `AGENT_AUDIT_FILE` 环境变量覆盖）
- **只追加不可篡改**：`audit_store.append_audit` 只追加，不提供修改/删除接口
- **correlation_id**：为空时自动用当前上下文的 correlation_id（由 registry 通过 contextvars 传递）
- **错误**：`E_VALIDATION`（role/action 为空/detail 超长）、`E_TOOL`（存储读写失败）
- **角色**：operator

### metric_collect
- **参数**：`scope` (enum summary/by_action/by_role/by_status, default summary)、`window` (int, default 0=全部历史)
- **输出**：
  - `summary`：`total: N` + top-3 actions/roles
  - `by_action`/`by_role`/`by_status`：逐项计数 `field: count`
- **数据源**：audit_store 的审计日志（只读聚合）
- **时间窗口**：`window>0` 只统计最近 N 秒的条目（按 `ts` 过滤）
- **错误**：`E_VALIDATION`（scope/window 非法）、`E_TOOL`（审计日志读取失败）
- **角色**：operator

### config_read
- **参数**：`section` (enum roles/tools/all, default all)
- **输出**：
  - `roles`：角色权限矩阵（allowed_tools / high_risk_tools / notes）
  - `tools`：工具规格（risk / timeout / cost / roles）
  - `all`：roles + tools
- **数据源**：`permissions.get_default_role_perms()` + `registry.list_tools()`（只读，无 IO）
- **错误**：`E_VALIDATION`（section 非法）
- **角色**：operator

### diff_preview
- **参数**：`old_content` (string, required)、`new_content` (string, required)、`context` (int, default 3)、`label` (string, default "")
- **输出**：unified diff 文本（`---/+++/-/+` 格式）；无差异返回 `(no differences)`
- **计算**：`difflib.unified_diff`，纯计算无 IO
- **label**：显示为 `<label> (old)` / `<label> (new)`；空则 `(old)` / `(new)`
- **错误**：`E_VALIDATION`（两侧都空/内容超长/context 为负）
- **角色**：operator

### git_commit
- **参数**：`repo_path` (string, required)、`message` (string, required)、`files` (array, 可选，空则 git add -A)
- **输出**：`committed <hash>`
- **执行**：`git_ops.run_git` 执行 `git add` + `git commit` + `git rev-parse HEAD`
- **安全**：副架构专用 + 高风险需审批 + repo_path 沙箱校验（门卫 + git_ops 二次校验）
- **错误**：`E_VALIDATION`（message 为空/超长）、`E_TOOL`（git 命令失败）
- **角色**：sub_architect（专用，operator 不可调用）

### rollback
- **参数**：`repo_path` (string, required)、`target` (string, required，commit hash 或 ref)
- **输出**：`rolled back: <old8> -> <new8>`
- **执行**：`git_ops.run_git` 执行 `git rev-parse HEAD` + `git reset --hard <target>` + `git rev-parse HEAD`
- **安全**：副架构专用 + 高风险需审批 + repo_path 沙箱校验
- **错误**：`E_VALIDATION`（target 为空）、`E_TOOL`（git 命令失败）
- **角色**：sub_architect（专用，operator 不可调用）

### git_log
- **参数**：`repo_path` (string, required)、`limit` (int, default 10)、`oneline` (bool, default true)
- **输出**：git log 文本；无 commit 返回 `(no commits)`
- **执行**：`git_ops.run_git` 执行 `git log -n<limit> [--oneline]`
- **安全**：只读 + repo_path 沙箱校验（limit 上限 100）
- **错误**：`E_VALIDATION`（limit 非正）、`E_TOOL`（git 命令失败）
- **角色**：operator + sub_architect

### approval_request
- **参数**：`tool_call_id` (string, required)、`reason` (string, required)、`requested_role` (string, 可选)
- **输出**：`approval requested: apr-<ts>-<tcid8>`
- **框架级**：纯记录生成，无 IO，无审批
- **错误**：`E_VALIDATION`（tool_call_id/reason 为空/reason 超长）
- **角色**：operator

### change_notify
- **参数**：`target` (string, required)、`change_type` (enum code/config/doc/test/other, required)、`summary` (string, required)
- **输出**：`notified <target>: ntf-<ts>-<target8>`
- **框架级**：纯记录生成，无 IO，无审批
- **错误**：`E_VALIDATION`（target/summary 为空/change_type 非法/summary 超长）
- **角色**：operator
