/* ============================================================
 * Agent Builder 前端 —— 业务逻辑与 UI 渲染
 * 依赖：api.js（全局 api 对象）
 * ============================================================ */

const STAGES = ['需求', '计划', '确认', '执行', '验证', '汇报'];

// 状态机 → 顶栏阶段索引映射
const STATUS_TO_STAGE = {
  'received': 0, 'planning': 1, 'awaiting_confirm': 2,
  'executing': 3, 'verifying': 4, 'reworking': 1,
  'delivering': 5, 'delivered': 5,
  'interrupted': -1, 'failed': -1,
};

// ── 当前任务状态 ──
let currentTaskId = null;
let currentStatus = null;

/* ──────────────── UI 渲染函数 ──────────────── */

/** 更新顶栏六阶段状态 */
function updateStageBar(status) {
  currentStatus = status;
  const bar = document.getElementById('stageBar');
  const stageIdx = STATUS_TO_STAGE[status] ?? -1;
  bar.innerHTML = STAGES.map((name, i) => {
    let cls = 'stage';
    if (status === 'failed') {
      cls += i === 0 ? ' failed' : '';
    } else if (status === 'interrupted') {
      if (i < stageIdx) cls += ' done';
      else if (i === stageIdx) cls += ' active';
    } else if (stageIdx >= 0) {
      if (i < stageIdx) cls += ' done';
      else if (i === stageIdx) cls += ' active';
    }
    return `<span class="${cls}">${name}</span>`;
  }).join('');
  if (status === 'failed') {
    const fail = document.createElement('span');
    fail.className = 'stage';
    fail.style.background = '#cf222e';
    fail.textContent = '✗ 失败';
    bar.appendChild(fail);
  }
}

/** 在对话区追加一条消息 */
function appendMessage(role, text) {
  const area = document.getElementById('chatArea');
  const wrap = document.createElement('div');
  wrap.className = role === 'user' ? 'msg-user' : 'msg-agent';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(bubble);
  area.appendChild(wrap);
  area.scrollTop = area.scrollHeight;
}

/** 追加工具调用卡片 */
function appendToolCard(title, status, detail) {
  const area = document.getElementById('chatArea');
  const card = document.createElement('div');
  card.className = 'tool-card';
  card.innerHTML = `
    <div class="tool-card-header">
      <span class="tool-card-title">🔧 工具调用 · ${title}</span>
      <span class="tool-status ${status}">${status === 'success' ? '✓ 成功' : status === 'failed' ? '✗ 失败' : '⟳ 进行中'}</span>
    </div>
    <div class="tool-card-detail">${detail}</div>
  `;
  area.appendChild(card);
  area.scrollTop = area.scrollHeight;
}

/** 追加审批弹窗（待确认问题） */
function appendApproval(questions) {
  const area = document.getElementById('chatArea');
  const card = document.createElement('div');
  card.className = 'approval-card';
  card.innerHTML = `
    <div class="approval-title">⚠ 需要你的确认</div>
    <div class="approval-detail">${questions.map(q => `<div>• ${q}</div>`).join('')}</div>
    <div class="approval-meta">确认后进入执行阶段，拒绝则返回重新规划</div>
    <div class="approval-actions">
      <button class="btn btn-approve" id="approveBtn">批准</button>
      <button class="btn btn-reject" id="rejectBtn">拒绝</button>
    </div>
  `;
  area.appendChild(card);
  area.scrollTop = area.scrollHeight;

  document.getElementById('approveBtn').onclick = handleApprove;
  document.getElementById('rejectBtn').onclick = handleReject;
}

/* ──────────────── 文件树渲染 ──────────────── */

/** 递归渲染文件树节点 */
function renderFileTree(nodes, container, depth = 0) {
  nodes.forEach(node => {
    const div = document.createElement('div');
    div.className = `tree-node depth-${depth}`;
    const icon = node.type === 'dir' ? '📁' : '📄';
    const sizeLabel = node.type === 'file' && node.size != null
      ? ` <span style="color:#8b949e;font-size:10px;">${formatSize(node.size)}</span>`
      : '';
    div.innerHTML = `${icon} ${node.name}${sizeLabel}`;
    div.title = node.path;
    container.appendChild(div);
    if (node.children && node.children.length > 0) {
      renderFileTree(node.children, container, depth + 1);
    }
  });
}

/** 格式化文件大小 */
function formatSize(bytes) {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

/** 加载并渲染工作区文件树 */
async function loadFileTree() {
  const tree = document.querySelector('.file-tree');
  if (!tree) return;
  tree.innerHTML = '<div class="tree-node depth-0">⟳ 加载中…</div>';
  try {
    const nodes = await api.getWorkspaceFiles();
    tree.innerHTML = '';
    if (nodes.length === 0) {
      tree.innerHTML = '<div class="tree-node depth-0">（工作区为空）</div>';
      return;
    }
    renderFileTree(nodes, tree);
  } catch (err) {
    tree.innerHTML = `<div class="tree-node depth-0" style="color:#cf222e;">❌ ${err.message}</div>`;
  }
}

/* ──────────────── 业务流程 ──────────────── */

/** 发送需求 → 创建任务 → 自动分解 */
async function handleSend() {
  const input = document.getElementById('msgInput');
  const requirement = input.value.trim();
  if (!requirement) return;

  // 1. 显示用户消息
  appendMessage('user', requirement);
  input.value = '';

  try {
    // 2. 创建任务
    const task = await api.createTask(requirement);
    currentTaskId = task.task_id;
    updateStageBar(task.status);
    appendMessage('agent', `已接收需求，任务 ID：${task.task_id.slice(0, 8)}… 正在分解步骤…`);

    // 3. 触发分解
    const result = await api.planTask(task.task_id, false);
    updateStageBar(result.status);

    if (result.pending_questions && result.pending_questions.length > 0) {
      appendMessage('agent', `分解完成，发现 ${result.pending_questions.length} 个待确认点：`);
      appendApproval(result.pending_questions);
    } else {
      const stepCount = Object.keys(result.steps || {}).length;
      appendMessage('agent', `分解完成，共 ${stepCount} 个步骤，等待确认计划…`);
      appendApproval(['计划已生成，是否批准执行？']);
    }
  } catch (err) {
    appendMessage('agent', `❌ 错误：${err.message}`);
  }
}

/** 批准计划 */
async function handleApprove() {
  if (!currentTaskId) return;
  try {
    const task = await api.approveTask(currentTaskId);
    updateStageBar(task.status);
    appendMessage('agent', '计划已批准，进入执行阶段…');
    document.querySelectorAll('#approveBtn, #rejectBtn').forEach(b => b.remove());

    // 渲染执行编排器产出的步骤结果卡片。
    const results = task.execution_results || [];
    let doneCount = 0;
    let failedCount = 0;
    for (const r of results) {
      const detail = r.error || r.result || '';
      const cardStatus = r.status === 'done' ? 'success'
        : (r.status === 'failed' || r.status === 'pending_approval') ? 'failed'
        : 'pending';
      if (r.status === 'done') doneCount++;
      else if (r.status === 'failed' || r.status === 'pending_approval') failedCount++;
      appendToolCard(`${r.action} · ${r.step_id}`, cardStatus, detail || '(无输出)');
    }

    // 状态汇总提示。
    if (task.status === 'verifying') {
      appendMessage('agent', `所有 ${results.length} 个步骤执行完成（成功 ${doneCount}），进入验证阶段。`);
    } else if (failedCount > 0) {
      appendMessage('agent', `执行结束：成功 ${doneCount} / 失败 ${failedCount}。可中断任务或重新规划。`);
    } else if (results.length > 0) {
      appendMessage('agent', `执行进度：成功 ${doneCount} / 共 ${results.length}。等待后续状态。`);
    }
  } catch (err) {
    appendMessage('agent', `❌ 批准失败：${err.message}`);
  }
}

/** 拒绝计划 */
async function handleReject() {
  if (!currentTaskId) return;
  try {
    const task = await api.rejectTask(currentTaskId);
    updateStageBar(task.status);
    appendMessage('agent', '计划已拒绝，返回重新规划…');
    document.querySelectorAll('#approveBtn, #rejectBtn').forEach(b => b.remove());
  } catch (err) {
    appendMessage('agent', `❌ 拒绝失败：${err.message}`);
  }
}

/** 中断任务 */
async function handleInterrupt() {
  if (!currentTaskId) {
    appendMessage('agent', '当前无运行中的任务');
    return;
  }
  try {
    const task = await api.interruptTask(currentTaskId, 'user_stop');
    updateStageBar(task.status);
    appendMessage('agent', '任务已中断，可点击恢复继续执行。');
  } catch (err) {
    appendMessage('agent', `❌ 中断失败：${err.message}`);
  }
}

/* ──────────────── 事件绑定 ──────────────── */
document.getElementById('sendBtn').addEventListener('click', handleSend);
document.getElementById('interruptBtn').addEventListener('click', handleInterrupt);
document.getElementById('msgInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  }
});

// 推理强度 slider
const slider = document.getElementById('tempSlider');
const tempValue = document.getElementById('tempValue');
slider.addEventListener('input', () => {
  tempValue.textContent = parseFloat(slider.value).toFixed(1);
});

// 密钥显示/隐藏
const keyToggle = document.getElementById('keyToggle');
const keyMasked = document.getElementById('keyMasked');
let keyVisible = false;
keyToggle.addEventListener('click', () => {
  keyVisible = !keyVisible;
  keyMasked.textContent = keyVisible ? 'sk-abc123def456...' : 'sk-••••••';
  keyToggle.textContent = keyVisible ? '🙈' : '👁';
});

// 模型切换
document.getElementById('modelSelect').addEventListener('change', (e) => {
  console.log('切换模型:', e.target.value);
});

// 技能 toggle
document.querySelectorAll('.toggle input').forEach(t => {
  t.addEventListener('change', () => console.log('技能开关:', t.checked));
});

// 新建项目按钮
document.querySelector('.new-project-btn').addEventListener('click', () => {
  document.getElementById('chatArea').innerHTML = '';
  currentTaskId = null;
  updateStageBar('received');
  document.getElementById('msgInput').focus();
});

// 上传按钮 → 刷新文件树
document.querySelector('.file-upload-btn')?.addEventListener('click', loadFileTree);

// 初始化：检测后端连通性 + 加载文件树
(async () => {
  try {
    await api.health();
    appendMessage('agent', '✅ 后端服务已连接，可开始输入需求。');
  } catch (err) {
    appendMessage('agent', `⚠️ ${err.message}`);
  }
  updateStageBar('received');
  loadFileTree();
})();
