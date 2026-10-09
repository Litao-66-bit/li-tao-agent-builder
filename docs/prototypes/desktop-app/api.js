/* ============================================================
 * Agent Builder · 桌面版原型 —— 桩接口层（stub API）
 *
 * 重要：本文件不发起任何网络请求。所有方法只用 setTimeout 模拟
 * 延迟后返回 mock.js 中的假数据，用于验证界面形态与交互。
 * 每个方法上方标注了它对应的「真实后端端点」与响应形状，
 * 便于后续按 backend-handoff 契约替换为真实调用。
 * ============================================================ */
(function () {
  'use strict';

  const MOCK = window.MOCK;

  /** 模拟网络延迟（毫秒）。 */
  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  const api = {
    /** GET /health → { status: 'ok' } */
    async health() {
      await delay(120);
      return { status: 'ok' };
    },

    /** GET /projects → { projects: [{ id, name, path, current }] } */
    async listProjects() {
      await delay(150);
      return { projects: MOCK.projects.map((p) => ({ ...p })) };
    },

    /** POST /projects/{id}/open → { project } */
    async openProject(projectId) {
      await delay(220);
      const project = MOCK.projects.find((p) => p.id === projectId) || MOCK.projects[0];
      MOCK.projects.forEach((p) => { p.current = p.id === project.id; });
      return { project: { ...project, current: true } };
    },

    /** POST /projects/pick-directory → { cancelled, path }（后端弹系统对话框） */
    async pickDirectory() {
      // 真实实现会在后端弹原生文件夹选择框；原型里由前端模拟对话框给出结果，
      // 未选中时返回 { cancelled: true }。
      await delay(80);
      return { cancelled: false, path: MOCK.folderPicker.selected };
    },

    /** POST /projects/create-folder → { project }（后端创建目录） */
    async createWorkspaceFolder(parentPath, name) {
      await delay(260);
      const path = `${parentPath}\\${name}`;
      const project = { id: `prj-${Date.now()}`, name, path, current: true };
      MOCK.projects.forEach((p) => { p.current = false; });
      MOCK.projects.unshift(project);
      return { project: { ...project } };
    },

    /** GET /workspace/files → { nodes: [TreeNode] } */
    async getWorkspaceFiles() {
      await delay(180);
      return { nodes: MOCK.fileTree };
    },

    /** GET /workspace/file?path=… → { path, content, size, editable } */
    async readFile(path) {
      await delay(140);
      const content = MOCK.fileContents[path];
      if (content == null) return { path, content: '（原型未内置该文件内容）', size: 0, editable: false };
      return { path, content, size: content.length, editable: true };
    },

    /** PUT /workspace/file?path=… → { path, saved: true } */
    async writeFile(path, content) {
      await delay(200);
      MOCK.fileContents[path] = content;
      return { path, saved: true };
    },

    /** POST /tasks → { task_id, status: 'planning' } */
    async createTask() {
      await delay(200);
      return { task_id: 'a1b2c3d4e5f6', status: 'planning' };
    },

    /**
     * POST /tasks/{id}/plan → DecomposeResponse
     * { task_id, steps: [...], questions: [...], high_risk_actions: [...] }
     * 注意：真实端点是后端同步 LLM 调用，前端需用长超时（120s）。
     */
    async planTask() {
      await delay(600);
      return {
        task_id: 'a1b2c3d4e5f6',
        questions: [
          '阈值口径以「对话轮次 / 累计 token / 字符」三条件任一命中为准？',
          '「一键新开对话」是否需要同时把历史摘要写入新会话？',
        ],
        high_risk_actions: ['file_write', 'file_delete'],
        steps: [
          { id: 'step-1', action: 'searcher', title: '检索仓库既有实现' },
          { id: 'step-2', action: 'file_write', title: '新建 agent_builder/api/handover.py' },
          { id: 'step-3', action: 'file_write', title: '前端交接卡片渲染' },
          { id: 'step-4', action: 'test_run', title: 'pytest tests/test_api_handover.py' },
          { id: 'step-5', action: 'summarizer', title: '生成交接文案' },
        ],
      };
    },

    /**
     * POST /tasks/{id}/approve → TaskResponse
     * 省略 approved_tools = 不授权任何高风险工具（写/删/提交/回滚会被门卫拒绝）。
     */
    async approveTask() {
      await delay(700);
      return {
        execution_results: [
          { step_id: 'step-1', status: 'done', detail: '命中 docs/HANDOVER.md 第 9 节' },
          { step_id: 'step-2', status: 'done', detail: '写入 4.3 KB（overwrite=true）' },
          { step_id: 'step-3', status: 'done', detail: 'frontend/js/app.js +180 行' },
          { step_id: 'step-4', status: 'done', detail: 'pytest 23 passed' },
          { step_id: 'step-5', status: 'done', detail: '交付摘要已生成' },
        ],
      };
    },

    /** POST /tasks/{id}/reject → { status: 'planning' } */
    async rejectTask() {
      await delay(180);
      return { status: 'planning' };
    },

    /** POST /tasks/{id}/interrupt → { status: 'interrupted' } */
    async interruptTask() {
      await delay(180);
      return { status: 'interrupted' };
    },

    /** POST /tasks/{id}/resume → { status: 'executing' } */
    async resumeTask() {
      await delay(180);
      return { status: 'executing' };
    },

    /** POST /tasks/{id}/abort → { status: 'failed' } */
    async abortTask() {
      await delay(180);
      return { status: 'failed' };
    },

    /**
     * POST /tasks/{id}/council → CouncilMinutes
     * { topic, participants, absent, rounds, agreements, disagreements, unresolved, decision_note }
     * 真实端点会做多次 LLM 调用（用长超时）。
     */
    async runCouncil() {
      await delay(900);
      return { ...MOCK.councilMinutes };
    },

    /**
     * GET /tasks/{id}/handover?turns=…&context_tokens=…&context_chars=…
     * → HandoverResponse（规则式判定，不调 LLM）
     */
    async getHandover(taskId, { turns = 0 } = {}) {
      await delay(260);
      const strength = turns >= 7 ? 'strong' : 'suggest';
      return MOCK.scenarios.delivered.handover
        ? { ...MOCK.scenarios.delivered.handover, strength, task_status: 'delivered' }
        : null;
    },

    /** POST /tasks/{id}/handover/ack → { status } */
    async ackHandover(taskId, status) {
      await delay(120);
      return { task_id: taskId, status };
    },
  };

  window.api = api;
})();
