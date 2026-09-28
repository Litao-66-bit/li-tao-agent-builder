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
 * ============================================================ */

const API_BASE = 'http://localhost:8000';

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
};
