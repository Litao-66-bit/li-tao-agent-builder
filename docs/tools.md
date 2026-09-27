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

## 安全机制

### 路径沙箱（文件类工具）
`file_read` / `file_list` / `file_write` / `file_edit` / `code_search` 的 `path` 参数
经 `ToolGatekeeper._check_sandbox_path` 校验：`Path.resolve()` 必须落在 `WORKSPACE_DIR` 内，
防止路径穿越越权读写系统目录。

### URL 安全校验（web 类工具）
`web_fetch` 的 `url` 参数经 `ToolGatekeeper._check_url_safety` → `url_guard.validate_url`：
1. scheme 仅允许 `http` / `https`；
2. 阻断云元数据端点 `169.254.169.254`；
3. hostname 解析为 IP 后禁止私有/保留/环回/链路本地/组播网段；
4. 可选域名白名单（默认空 = 允许所有公共域名）。

`web_search` 的 URL 在实现内部构造（DuckDuckGo 固定 API），门卫跳过 URL 校验。

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
