# 项目交接提示词（Agent Builder）

我正在开发 **Agent Builder** 项目（一个能生成 Agent 的 Meta Agent）。前端已完成 10 项问题修复并推送到 GitHub；**密钥管理功能与「副结构自检」开关只在本地，尚未推送**。接下来的工作可能涉及前端页面改动、密钥接入各 agent。

## 基础运行环境

- **操作系统：** Windows + PowerShell
- **项目根目录（当前会话工作目录）：** `C:\Users\李陶\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\work-mode-projects\6abcee34807a00aa83da2398`
  - 说明：这是从旧目录 `...\6ab90d65a59bc24924239f43\li-tao-agent-builder` 复制出来的工作副本（已排除 `.venv` 与各类缓存）。编辑操作被限制在本工作目录内。
- **GitHub：** `Litao-66-bit/li-tao-agent-builder`，branch `main`（无本地 `.git`）
  - 最近已推送提交：`0164a202`（fix(frontend): 修复前端 10 项问题）
  - 推送方式：`git credential fill` 取 token → REST API：blob → tree(base_tree=远端 HEAD tree) → commit → PATCH ref
- **Python：** `"$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe"`（3.14），ruff 0.16.9，依赖已装（fastapi / pydantic / pytest 可用）
- **后端：** FastAPI + uvicorn，端口 **8000**，CORS 已开 `*`
- **前端：** 纯静态 HTML/CSS/JS（无构建步骤、无 npm），需静态服务器跑在 **8080**
- **代码注释 / commit message 一律用中文**

## 启动与校验命令

```powershell
# 后端（项目根目录）
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m uvicorn agent_builder.api.app:create_app --factory --reload --port 8000

# 前端（项目根目录）
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m http.server 8080 --directory frontend

# 校验
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m ruff check .
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m pytest
```

当前基线：`ruff` 干净、`pytest` 744 passed。

## 密钥管理（安全优先）—— 本功能未推送 GitHub

**核心原则：密钥只进内存、不回显、不落盘、不进日志。任何接口响应都不含明文。**

后端：

- `agent_builder/api/secrets.py`（新增）：进程内单例 `ApiKeyStore`
  - `set_key(raw)` 校验：非空、长度 8–512、**禁止空白与控制字符**（防 HTTP 头 / 日志注入）；非法抛 `InvalidApiKeyError`（消息只含原因，不含原文）
  - 只对外暴露 `is_configured` 与 `masked_hint()`（形如 `sk-***`，仅保留前 3 位）
  - `get()` 返回明文，**仅供进程内调用**（如 LLM 客户端工厂），禁止直接对外
  - 日志只记掩码；`reset_api_key_store()` 供测试重置
- `agent_builder/api/routes.py`：三个端点
  - `GET /settings/api-key` → `{configured, masked}`
  - `POST /settings/api-key` body `{api_key}` → 校验失败返回 400（无明文），成功返回 `{configured: true, masked}`
  - `DELETE /settings/api-key` → 幂等清除
- `agent_builder/api/schemas.py`：`ApiKeyRequest` / `ApiKeyStatusResponse`（响应体无明文字段）
- `agent_builder/api/deps.py`：`get_llm_client()` 密钥来源优先级 **运行时存储 > 环境变量 `DEEPSEEK_API_KEY`**；后续各 agent 走此工厂即可拿到密钥

前端：

- `frontend/index.html`：原伪 `sk-••••••` + 眼睛按钮已删除，改为 `type="password"` 输入框 + 「保存」按钮（`#apiKeyInputChip`）；已配置时只显示「⚿ 已保存 sk-***」+「删除密钥」（`#apiKeyStatusChip`）
- `frontend/js/app.js`：保存成功后**先清空输入框**再渲染掩码；删除需二次确认（确认文案不含密钥）；**不写入 localStorage/sessionStorage**，无任何"查看密钥"入口
- `frontend/js/api.js`：`getApiKeyStatus` / `setApiKey` / `deleteApiKey`
- `frontend/styles.css`：密钥样式 + `.setting-chip[hidden]{display:none}`（修掉 `.setting-chip{display:flex}` 覆盖 `[hidden]` 的坑）

已知限制 / 后续事项：

- 密钥**仅存内存，重启后端即失效**，需重新输入
- 目前只做**格式校验**，未做真实连通性验证（避免网络依赖）；如需"正确密钥"语义可加可选 probe
- 开发环境是 localhost 明文 HTTP，正式部署必须走 HTTPS
- 下一步目标：把密钥接入各类 agent（统一走 `get_llm_client()`）

## 前端文件清单

| 文件 | 职责 |
|---|---|
| `frontend/index.html` | 顶栏六阶段状态条 + 三栏布局；底栏：模型、推理强度、**API 密钥输入口**、LLM 拆分开关、**副结构自检开关**、高级 |
| `frontend/styles.css` | 全部样式（工具卡片、审批卡、文件树、密钥输入区、响应式） |
| `frontend/js/api.js` | API 封装层，`API_BASE='http://localhost:8000'`，13 个方法 |
| `frontend/js/app.js` | 业务逻辑 + UI 渲染：状态机映射、消息/工具卡片/审批卡、文件树、密钥管理、各任务操作 |

## 后端关键文件

| 文件 | 职责 |
|---|---|
| `agent_builder/api/app.py` | FastAPI 应用工厂 `create_app()` |
| `agent_builder/api/routes.py` | HTTP 路由：health、tasks 系列、workspace/files、settings/api-key |
| `agent_builder/api/secrets.py` | **密钥运行时存储（新增）** |
| `agent_builder/api/deps.py` | 单例 store + LLM 客户端工厂（密钥优先级逻辑） |
| `agent_builder/api/schemas.py` | Pydantic 模型（含 ApiKey*、TaskResponse/StepResult/FileNode） |
| `agent_builder/api/orchestrator.py` | 执行编排器 `run_plan()`：Router + ToolGatekeeper + registry |
| `agent_builder/api/store.py` | `TaskEntry` + `InMemoryTaskStore` |
| `agent_builder/contracts/state_machine.py` | 10 状态 / 事件转换表 |
| `agent_builder/llm/config.py` · `client.py` | LLM 配置（env）与客户端 |

## 当前状态

- 后端状态机：received → planning → awaiting_confirm → executing → verifying → delivering → delivered（异常分支 interrupted / failed / reworking）；`INTERRUPT` 仅 `executing→interrupted`，`RESUME` 仅 `interrupted→executing`，`ABORT` 任意态可终止
- `POST /tasks/{id}/approve` 触发 `run_plan` 执行，结果回填 `execution_results`；全 done 转 `verifying`
- LLM 拆分：`use_llm` 由前端「LLM 拆分」开关控制；无密钥时降级为待确认/空计划
- 前端 10 项修复已推送（`0164a202`）：状态类名统一、恢复/放弃入口、`escapeHtml` 防注入、LLM 开关、文件树真实数据 + 点击预览、上传改刷新、待审批不再计失败、中断按钮按状态禁用、卡片作用域绑定、移除假 token 数据
- 未推送的本地改动：**密钥管理全链路**、**「副结构自检」开关**（纯前端状态位，后端暂无对应字段）
- 模型下拉 / 推理强度 / 副结构自检目前均为**前端本地状态**，后端 `PlanRequest` 只接收 `use_llm`；若要生效需扩展请求字段
