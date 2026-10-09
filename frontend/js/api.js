/* ============================================================
 * Agent Builder 前端 —— 后端 API 封装层
 * 后端端点（FastAPI，端口 8000）：
 *   POST   /tasks                创建任务 → planning（响应含 title / pending_approval）
 *   GET    /tasks/{id}           查询任务状态（含 plan / execution_results / high_risk_actions）
 *   GET    /tasks                任务 ID 列表（契约不变）
 *   GET    /task-summaries       任务摘要列表（多任务并存、可回看）
 *   DELETE /tasks/{id}           删除任务（不存在 → 404；不触发执行副作用）
 *   POST   /tasks/{id}/plan      触发分解 → awaiting_confirm（返回 steps / order / parallel_groups）
 *   POST   /tasks/{id}/run       柔性主入口：默认直接执行；有未放行高风险步骤则挂起
 *   POST   /tasks/{id}/approve   严格前置确认通道（省略 approved_tools = 不授权）
 *   POST   /tasks/{id}/reject    改计划 → planning
 *   POST   /tasks/{id}/interrupt 中断 → interrupted
 *   POST   /tasks/{id}/resume    到点暂停后放行继续（携带 approved_tools）
 *   POST   /tasks/{id}/abort     放弃 → failed
 *   GET    /workspace/files      工作区文件树
 *   GET    /workspace/file       读取单个文件内容（只读预览，路径须在工作区内）
 *   PUT    /workspace/file       写回文件内容（仅覆盖已存在文件，原子写）
 *   GET    /settings/api-key     查询密钥状态（已配置 + 掩码 + 指纹 + TTL/轮换元数据）
 *   POST   /settings/api-key     保存密钥（仅服务端内存，不回显；可带 ttl_s）
 *   POST   /settings/api-key/rotate 轮换密钥（替换现有密钥，返回旧指纹）
 *   DELETE /settings/api-key     删除密钥
 * ============================================================ */

// 用 127.0.0.1 而不是 localhost：Windows 上浏览器可能把 localhost 解析为 IPv6 ::1，
// 而 uvicorn 默认只监听 IPv4 127.0.0.1，会导致连接被拒（文件树/接口全部失败）。
const API_BASE = 'http://127.0.0.1:8000';

// 敏感端点（/settings/api-key*、/workspace/file*）要求携带本机客户端标识头：
// 浏览器跨站请求需预检才能带上自定义头，表单型跨站提交则根本无法设置头。
const CLIENT_HEADER = 'X-Agent-Builder-Client';
const CLIENT_HEADER_VALUE = 'web';

/** 组装密钥请求体：ttl_s 只在显式传入时带上（不传 = 不过期）。 */
function keyBody(apiKey, ttlS) {
  return ttlS ? { api_key: apiKey, ttl_s: ttlS } : { api_key: apiKey };
}

// 单次请求超时（毫秒）：后端无响应时中止请求，避免界面永久卡在禁用态（审计 P2）。
// 默认 15s 只适用于轻量请求（查询/状态转换）；plan / approve 是后端同步 LLM 调用，
// 单次生成常超过 15s，必须用 LLM_TIMEOUT_MS，否则前端会误报超时（任务实际仍在跑）。
const REQUEST_TIMEOUT_MS = 15000;
const LLM_TIMEOUT_MS = 120000;
// 不设超时（0）：用于会等待人工操作的后端调用（系统文件夹选择对话框）。
const NO_TIMEOUT_MS = 0;

/** 把后端错误响应体压成一句人话（不再让结构化 detail 变成 "[object Object]"）。
 *
 * 后端 detail 有三种形态：字符串（HTTPException(status, detail="…")）、
 * 对象（`exc.to_dict()`，含 error_code / message 等）、数组（FastAPI 422 校验错误）。
 * 直接塞进 `new Error(detail)` 会得到无意义的 "[object Object]"。
 */
function apiErrorText(data, status) {
  const raw = data && (data.message || data.detail);
  if (typeof raw === 'string' && raw.trim()) return raw.trim();
  if (Array.isArray(raw)) {
    const parts = raw
      .map((item) => {
        if (typeof item === 'string') return item;
        if (!item || typeof item !== 'object') return '';
        const where = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : '';
        const what = item.msg || item.message || '';
        return where && what ? `${where}：${what}` : what;
      })
      .filter(Boolean);
    if (parts.length) return parts.join('；');
  }
  if (raw && typeof raw === 'object' && typeof raw.message === 'string' && raw.message.trim()) {
    return raw.message.trim();
  }
  return `请求失败（HTTP ${status}）`;
}

const api = {
  async request(method, path, body, timeoutMs = REQUEST_TIMEOUT_MS) {
    const opts = {
      method,
      headers: {
        'Content-Type': 'application/json',
        [CLIENT_HEADER]: CLIENT_HEADER_VALUE,
      },
    };
    if (body) opts.body = JSON.stringify(body);
    const controller = new AbortController();
    opts.signal = controller.signal;
    const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const resp = await fetch(API_BASE + path, opts);
      const text = await resp.text();
      let data;
      try { data = JSON.parse(text); } catch { data = { message: text }; }
      if (!resp.ok) {
        throw new Error(apiErrorText(data, resp.status));
      }
      return data;
    } catch (err) {
      if (err && err.name === 'AbortError') {
        throw new Error(`请求超时（${timeoutMs / 1000}s 内后端无响应），请检查后端服务`);
      }
      // err.message 可能为 undefined（非 Error 抛出），直接调用 includes 会抛 TypeError（审计 P3）
      if (err && err.message && err.message.includes('Failed to fetch')) {
        throw new Error('无法连接后端服务，请确认 FastAPI 已启动（端口 8000）');
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  },
  health: () => api.request('GET', '/health'),
  // 对话入口：先判断这句话是闲聊（kind=chat，直接回话）还是执行诉求（kind=task，转任务链路）。
  // 无副作用、不建任务；无密钥时后端如实回一句中文提示，不会回退成固定工作流。
  chat: (message, history = [], options = {}) =>
    api.request('POST', '/chat', { message, history, ...options }, LLM_TIMEOUT_MS),
  createTask: (requirement) => api.request('POST', '/tasks', { requirement }),
  getTask: (id) => api.request('GET', `/tasks/${id}`),
  // 删除任务：对应 DELETE /tasks/{id}（不存在 → 404；不触发执行副作用）。
  deleteTask: (id) => api.request('DELETE', `/tasks/${id}`),
  // 任务摘要列表：多任务并存、可回看（标题 / 状态 / 是否待放行）。
  listTaskSummaries: () => api.request('GET', '/task-summaries'),
  planTask: (id, useLLM = false, options = {}) =>
    api.request('POST', `/tasks/${id}/plan`, { use_llm: useLLM, ...options }, LLM_TIMEOUT_MS),
  // 柔性主入口：默认直接执行；若计划里有未放行的高风险步骤，后端挂起并返回 pending_approval。
  runTask: (id, approvedTools = null) =>
    api.request(
      'POST',
      `/tasks/${id}/run`,
      approvedTools && approvedTools.length ? { approved_tools: approvedTools } : null,
      LLM_TIMEOUT_MS,
    ),
  // 严格前置确认通道（保留契约）：省略 approved_tools = 不授权任何高风险工具。
  approveTask: (id, approvedTools = null) =>
    api.request(
      'POST',
      `/tasks/${id}/approve`,
      approvedTools && approvedTools.length ? { approved_tools: approvedTools } : null,
      LLM_TIMEOUT_MS,
    ),
  rejectTask: (id) => api.request('POST', `/tasks/${id}/reject`),
  interruptTask: (id, reason = 'user_stop') => api.request('POST', `/tasks/${id}/interrupt`, { reason }),
  // 到点暂停后放行：携带 approved_tools 逐工具授权；全部放行后才继续执行剩余计划。
  resumeTask: (id, approvedTools = null) =>
    api.request(
      'POST',
      `/tasks/${id}/resume`,
      approvedTools && approvedTools.length ? { approved_tools: approvedTools } : null,
      LLM_TIMEOUT_MS,
    ),
  abortTask: (id) => api.request('POST', `/tasks/${id}/abort`),
  // 确认交付：agentic 收敛后停在 verifying 的**唯一出口**（verifying → delivered）。
  deliverTask: (id) => api.request('POST', `/tasks/${id}/deliver`),
  // 副结构提议的决定：批准会把脚手架写入工作区，拒绝只记录决定。
  approveProposal: (id) => api.request('POST', `/tasks/${id}/proposal/approve`),
  rejectProposal: (id) => api.request('POST', `/tasks/${id}/proposal/reject`),
  getWorkspaceFiles: () => api.request('GET', '/workspace/files'),
  readWorkspaceFile: (path) => api.request('GET', `/workspace/file?path=${encodeURIComponent(path)}`),
  writeWorkspaceFile: (path, content) =>
    api.request('PUT', `/workspace/file?path=${encodeURIComponent(path)}`, { content }),
  // API 密钥：只提交/轮换/删除；读取仅返回「已配置 + 掩码 + 指纹 + TTL/轮换元数据」，不回显明文。
  getApiKeyStatus: () => api.request('GET', '/settings/api-key'),
  setApiKey: (apiKey, ttlS) => api.request('POST', '/settings/api-key', keyBody(apiKey, ttlS)),
  rotateApiKey: (apiKey, ttlS) =>
    api.request('POST', '/settings/api-key/rotate', keyBody(apiKey, ttlS)),
  deleteApiKey: () => api.request('DELETE', '/settings/api-key'),
  // ── 项目（工作区）──
  // 工作区由后端「当前项目」决定：选定目录 → 登记 → 设为当前，/workspace/* 随之前移。
  listProjects: () => api.request('GET', '/projects'),
  // 系统文件夹对话框在后端弹出，用户可能长时间停留 → 不设超时。
  pickDirectory: () => api.request('POST', '/projects/pick-directory', null, NO_TIMEOUT_MS),
  createWorkspaceFolder: (parentPath, name) =>
    api.request('POST', '/projects/create-folder', { parent_path: parentPath, name }),
  createProject: (path) => api.request('POST', '/projects', { path }),
  openProject: (projectId) => api.request('POST', `/projects/${projectId}/open`),
  // 评审会：多角色独立表态后收敛为结构化纪要（多次 LLM 调用，用长超时）
  runCouncil: (taskId, payload = {}) =>
    api.request('POST', `/tasks/${taskId}/council`, payload, LLM_TIMEOUT_MS),
  // ── 交接提示（handover hint）──
  // 规则式判定（不调 LLM）：门控（轮次 / token / 字符）+ 未决项 + 任务状态综合。
  // turns=用户消息数；context_tokens / context_chars 由前端累计上报。
  getHandover: (taskId, { turns = 0, contextTokens = 0, contextChars = 0 } = {}) =>
    api.request(
      'GET',
      `/tasks/${taskId}/handover?turns=${turns}&context_tokens=${contextTokens}&context_chars=${contextChars}`
    ),
  ackHandover: (taskId, status) =>
    api.request('POST', `/tasks/${taskId}/handover/ack`, { status }),
};
