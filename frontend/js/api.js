/* ============================================================
 * Agent Builder 前端 —— 后端 API 封装层
 * 后端端点（FastAPI，端口 8000）：
 *   POST   /tasks                创建任务 → planning
 *   GET    /tasks/{id}           查询任务状态
 *   POST   /tasks/{id}/plan      触发分解 → awaiting_confirm
 *   POST   /tasks/{id}/approve   确认计划 → executing
 *   POST   /tasks/{id}/reject    拒绝计划 → planning
 *   POST   /tasks/{id}/interrupt 中断 → interrupted
 *   POST   /tasks/{id}/resume    恢复 → executing
 *   POST   /tasks/{id}/abort     放弃 → failed
 *   GET    /workspace/files      工作区文件树
 *   GET    /workspace/file       读取单个文件内容（只读预览，路径须在工作区内）
 *   PUT    /workspace/file       写回文件内容（仅覆盖已存在文件，原子写）
 *   GET    /settings/api-key     查询密钥状态（只返回是否已配置 + 掩码）
 *   POST   /settings/api-key     保存密钥（仅服务端内存，不回显）
 *   DELETE /settings/api-key     删除密钥
 * ============================================================ */

// 用 127.0.0.1 而不是 localhost：Windows 上浏览器可能把 localhost 解析为 IPv6 ::1，
// 而 uvicorn 默认只监听 IPv4 127.0.0.1，会导致连接被拒（文件树/接口全部失败）。
const API_BASE = 'http://127.0.0.1:8000';

const api = {
  async request(method, path, body) {
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);
    try {
      const resp = await fetch(API_BASE + path, opts);
      const text = await resp.text();
      let data;
      try { data = JSON.parse(text); } catch { data = { message: text }; }
      if (!resp.ok) {
        const msg = data.message || data.detail || `HTTP ${resp.status}`;
        throw new Error(msg);
      }
      return data;
    } catch (err) {
      if (err.message.includes('Failed to fetch')) {
        throw new Error('无法连接后端服务，请确认 FastAPI 已启动（端口 8000）');
      }
      throw err;
    }
  },
  health: () => api.request('GET', '/health'),
  createTask: (requirement) => api.request('POST', '/tasks', { requirement }),
  getTask: (id) => api.request('GET', `/tasks/${id}`),
  planTask: (id, useLLM = false) => api.request('POST', `/tasks/${id}/plan`, { use_llm: useLLM }),
  approveTask: (id) => api.request('POST', `/tasks/${id}/approve`),
  rejectTask: (id) => api.request('POST', `/tasks/${id}/reject`),
  interruptTask: (id, reason = 'user_stop') => api.request('POST', `/tasks/${id}/interrupt`, { reason }),
  resumeTask: (id) => api.request('POST', `/tasks/${id}/resume`),
  abortTask: (id) => api.request('POST', `/tasks/${id}/abort`),
  getWorkspaceFiles: () => api.request('GET', '/workspace/files'),
  readWorkspaceFile: (path) => api.request('GET', `/workspace/file?path=${encodeURIComponent(path)}`),
  writeWorkspaceFile: (path, content) =>
    api.request('PUT', `/workspace/file?path=${encodeURIComponent(path)}`, { content }),
  // API 密钥：只提交与删除，读取仅返回「已配置 + 掩码」，不回显明文。
  getApiKeyStatus: () => api.request('GET', '/settings/api-key'),
  setApiKey: (apiKey) => api.request('POST', '/settings/api-key', { api_key: apiKey }),
  deleteApiKey: () => api.request('DELETE', '/settings/api-key'),
};
