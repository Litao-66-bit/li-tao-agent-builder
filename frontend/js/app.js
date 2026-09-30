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

// 工具卡片状态 → 展示文案（键名同时用作 CSS 类名 .tool-status.<key>）。
const TOOL_STATUS_LABELS = {
  success: '✓ 成功',
  failed: '✗ 失败',
  running: '⟳ 进行中',
  pending: '○ 待执行',
  pending_approval: '⚠ 待审批',
};

// ── 当前任务状态 ──
let currentTaskId = null;
let currentStatus = null;

/* ──────────────── 通用工具 ──────────────── */

/** 转义 HTML，避免后端返回内容（工具输出/错误/文件名）注入或破坏布局。 */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

/** 移除卡片内的操作区（批准/拒绝/恢复/放弃按钮）。 */
function clearCardActions(card) {
  if (!card) return;
  card.querySelectorAll('.approval-actions').forEach((el) => el.remove());
}

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
  // 仅执行阶段允许中断（对应后端 EXECUTING → INTERRUPTED）。
  const interruptBtn = document.getElementById('interruptBtn');
  if (interruptBtn) interruptBtn.disabled = status !== 'executing';
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
  const cls = TOOL_STATUS_LABELS[status] ? status : 'pending';
  card.innerHTML = `
    <div class="tool-card-header">
      <span class="tool-card-title">🔧 工具调用 · ${escapeHtml(title)}</span>
      <span class="tool-status ${cls}">${TOOL_STATUS_LABELS[cls]}</span>
    </div>
    <div class="tool-card-detail">${escapeHtml(detail)}</div>
  `;
  area.appendChild(card);
  area.scrollTop = area.scrollHeight;
}

/** 追加审批弹窗（待确认问题） */
function appendApproval(questions) {
  const area = document.getElementById('chatArea');
  const card = document.createElement('div');
  card.className = 'approval-card';
  const list = (questions || []).map((q) => `<div>• ${escapeHtml(q)}</div>`).join('');
  card.innerHTML = `
    <div class="approval-title">⚠ 需要你的确认</div>
    <div class="approval-detail">${list}</div>
    <div class="approval-meta">确认后进入执行阶段，拒绝则返回重新规划</div>
    <div class="approval-actions">
      <button class="btn btn-approve">批准</button>
      <button class="btn btn-reject">拒绝</button>
    </div>
  `;
  area.appendChild(card);
  area.scrollTop = area.scrollHeight;

  // 按卡片作用域绑定，避免多张卡片时 id 冲突误绑。
  card.querySelector('.btn-approve').onclick = () => handleApprove(card);
  card.querySelector('.btn-reject').onclick = () => handleReject(card);
}

/** 追加中断后的恢复/放弃操作卡片 */
function appendInterruptCard() {
  const area = document.getElementById('chatArea');
  const card = document.createElement('div');
  card.className = 'approval-card';
  card.innerHTML = `
    <div class="approval-title">⏸ 任务已中断</div>
    <div class="approval-meta">恢复则从断点继续执行；放弃则任务终止。</div>
    <div class="approval-actions">
      <button class="btn btn-resume">▶ 恢复</button>
      <button class="btn btn-reject">✗ 放弃</button>
    </div>
  `;
  area.appendChild(card);
  area.scrollTop = area.scrollHeight;

  card.querySelector('.btn-resume').onclick = () => handleResume(card);
  card.querySelector('.btn-reject').onclick = () => handleAbort(card);
}

/* ──────────────── 文件树渲染 ──────────────── */

/** 递归渲染文件树：目录可折叠，文件可点击打开。 */
function renderFileTree(nodes, container, depth = 0) {
  nodes.forEach((node) => {
    const row = document.createElement('div');
    row.className = `tree-node depth-${depth}${node.type === 'dir' ? ' tree-dir' : ' tree-file'}`;
    row.title = node.path;

    if (node.type === 'dir') {
      row.innerHTML = `<span class="tree-caret">▾</span> 📁 ${escapeHtml(node.name)}`;
      container.appendChild(row);

      // 子节点放进独立容器，便于整块折叠/展开。
      const childBox = document.createElement('div');
      childBox.className = 'tree-children';
      container.appendChild(childBox);
      renderFileTree(node.children || [], childBox, depth + 1);

      row.addEventListener('click', () => {
        const collapsed = childBox.classList.toggle('collapsed');
        row.querySelector('.tree-caret').textContent = collapsed ? '▸' : '▾';
      });
    } else {
      const sizeLabel = node.size != null
        ? ` <span class="tree-size">${formatSize(node.size)}</span>`
        : '';
      row.innerHTML = `📄 ${escapeHtml(node.name)}${sizeLabel}`;
      row.addEventListener('click', () => openWorkspaceFile(node));
      container.appendChild(row);
    }
  });
}

/* ──────────────── 文件查看/编辑 ────────────────
 * 两种模式：
 * - 工作区文件：可编辑，点「保存」写回后端（超大可编辑上限的文件降级为只读预览）；
 * - 本地文件：可编辑，点「另存为」下载修改后的副本（浏览器无法直接写回本地路径）。
 */

const fileViewer = document.getElementById('fileViewer');
const fileViewerPath = document.getElementById('fileViewerPath');
const fileViewerMeta = document.getElementById('fileViewerMeta');
const fileViewerDirty = document.getElementById('fileViewerDirty');
const fileViewerSave = document.getElementById('fileViewerSave');
const fileViewerDownload = document.getElementById('fileViewerDownload');
const fileViewerBody = document.getElementById('fileViewerBody');

// 当前打开的文件：viewerWorkspacePath 为工作区相对路径；viewerLocalName 为本地文件名。
let viewerWorkspacePath = null;
let viewerLocalName = '';
let viewerDirty = false;

/** 标记「有未保存修改」。 */
function setViewerDirty(dirty) {
  viewerDirty = dirty;
  fileViewerDirty.hidden = !dirty;
}

/** 复位查看器状态并清空内容（默认只读）。 */
function resetViewer() {
  viewerWorkspacePath = null;
  viewerLocalName = '';
  fileViewerDirty.hidden = true;
  viewerDirty = false;
  fileViewerSave.hidden = true;
  fileViewerDownload.hidden = true;
  fileViewerSave.disabled = false;
  fileViewerBody.value = '';
  fileViewerBody.readOnly = true;
}

/** 关闭查看器；有未保存修改时先二次确认。 */
function closeFileViewer(force) {
  if (viewerDirty && !force && !window.confirm('有未保存的修改，确定关闭吗？')) return;
  fileViewer.hidden = true;
  resetViewer();
}

/** 打开工作区文件：可编辑则显示「保存」；超出可编辑上限则只读截断预览。 */
async function openWorkspaceFile(node) {
  fileViewer.hidden = false;
  resetViewer();
  viewerWorkspacePath = node.path;
  fileViewerPath.textContent = node.path;
  fileViewerMeta.textContent = node.size != null ? formatSize(node.size) : '';
  fileViewerBody.value = '加载中…';
  try {
    const file = await api.readWorkspaceFile(node.path);
    fileViewerBody.value = file.content;
    if (file.truncated) {
      // 内容已被截断，禁止编辑，避免把残缺内容覆盖回原文件。
      fileViewerBody.readOnly = true;
      fileViewerMeta.textContent = `${formatSize(file.size)} · 过大，仅只读预览`;
    } else {
      fileViewerBody.readOnly = false;
      fileViewerSave.hidden = false;
      fileViewerMeta.textContent = formatSize(file.size);
    }
    setViewerDirty(false); // 程序赋值不触发 input，这里显式复位
  } catch (err) {
    fileViewerBody.value = `无法打开文件：${err.message}`;
    fileViewerBody.readOnly = true;
  }
}

/** 保存当前工作区文件（覆盖写，仅限已存在文件）。 */
async function handleViewerSave() {
  if (!viewerWorkspacePath) return;
  fileViewerSave.disabled = true;
  try {
    const file = await api.writeWorkspaceFile(viewerWorkspacePath, fileViewerBody.value);
    fileViewerMeta.textContent = `${formatSize(file.size)} · 已保存`;
    setViewerDirty(false);
    appendMessage('agent', `已保存文件：${file.path}`);
  } catch (err) {
    fileViewerMeta.textContent = `保存失败：${err.message}`;
    appendMessage('agent', `❌ 保存失败：${err.message}`);
  } finally {
    fileViewerSave.disabled = false;
  }
}

/** 「另存为」：把编辑后的本地文件内容下载为副本（浏览器不能写回原路径）。 */
function handleViewerDownload() {
  if (!viewerLocalName) return;
  const blob = new Blob([fileViewerBody.value], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = viewerLocalName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setViewerDirty(false);
}

fileViewerBody.addEventListener('input', () => {
  if (!fileViewerBody.readOnly) setViewerDirty(true);
});
fileViewerSave.addEventListener('click', handleViewerSave);
fileViewerDownload.addEventListener('click', handleViewerDownload);
document.getElementById('fileViewerClose').addEventListener('click', () => closeFileViewer());
fileViewer.addEventListener('click', (e) => {
  if (e.target.dataset && e.target.dataset.close) closeFileViewer();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !fileViewer.hidden) closeFileViewer();
});

/* ──────────────── 选择本地文件（纯前端读取，不上传） ──────────────── */

// 本地文件可打开上限：超过则只给提示，避免超大文件卡死浏览器。
const MAX_LOCAL_FILE_BYTES = 5 * 1024 * 1024;

const localFileInput = document.getElementById('localFileInput');
const localFileBtn = document.getElementById('localFileBtn');

/** 打开本地文件：可编辑，「另存为」下载副本；不上传、不落盘、不发请求。 */
function openLocalFile(file) {
  fileViewer.hidden = false;
  resetViewer();
  viewerLocalName = file.name;
  fileViewerPath.textContent = `${file.name}（本地）`;
  fileViewerMeta.textContent = formatSize(file.size);
  if (file.size > MAX_LOCAL_FILE_BYTES) {
    fileViewerBody.value = `文件过大（${formatSize(file.size)}），超过本地可打开上限 ${formatSize(MAX_LOCAL_FILE_BYTES)}。`;
    fileViewerBody.readOnly = true;
    return;
  }
  fileViewerBody.readOnly = false;
  fileViewerDownload.hidden = false;
  fileViewerBody.value = '读取中…';
  const reader = new FileReader();
  reader.onload = () => {
    fileViewerBody.value = String(reader.result);
    setViewerDirty(false);
  };
  reader.onerror = () => { fileViewerBody.value = '读取本地文件失败。'; };
  reader.readAsText(file);
}

localFileBtn.addEventListener('click', () => localFileInput.click());
localFileInput.addEventListener('change', () => {
  const file = localFileInput.files && localFileInput.files[0];
  // 清空 value，保证连续选择同一个文件也能再次触发 change。
  localFileInput.value = '';
  if (file) openLocalFile(file);
});

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
    tree.innerHTML = `<div class="tree-node depth-0" style="color:#cf222e;">❌ ${escapeHtml(err.message)}</div>`
      + '<div class="tree-node depth-0" style="color:#57606a;font-size:10px;">启动后端：python -m uvicorn agent_builder.api.app:create_app --factory --reload --port 8000</div>';
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

  // 2. 读取 LLM 拆分开关（未勾选时后端返回待确认/空计划）。
  const useLLM = document.getElementById('llmToggle')?.checked || false;

  try {
    // 3. 创建任务
    const task = await api.createTask(requirement);
    currentTaskId = task.task_id;
    updateStageBar(task.status);
    appendMessage('agent', `已接收需求，任务 ID：${task.task_id.slice(0, 8)}… 正在分解步骤…`);

    // 4. 触发分解
    const result = await api.planTask(task.task_id, useLLM);
    updateStageBar(result.status);

    const stepCount = Object.keys(result.steps || {}).length;
    const questions = result.pending_questions || [];
    if (questions.length > 0) {
      appendMessage('agent', `分解完成，发现 ${questions.length} 个待确认点：`);
      appendApproval(questions);
    } else if (stepCount > 0) {
      appendMessage('agent', `分解完成，共 ${stepCount} 个步骤，等待确认计划…`);
      appendApproval(['计划已生成，是否批准执行？']);
    } else {
      appendMessage('agent', '分解完成但未生成步骤（未启用 LLM 拆分）。勾选底部「LLM 拆分」后重新发送需求，或点击「⏸ 中断」。');
    }
  } catch (err) {
    appendMessage('agent', `❌ 错误：${err.message}`);
  }
}

/** 批准计划 */
async function handleApprove(card) {
  if (!currentTaskId) return;
  try {
    const task = await api.approveTask(currentTaskId);
    updateStageBar(task.status);
    clearCardActions(card);
    appendMessage('agent', '计划已批准，进入执行阶段…');

    // 渲染执行编排器产出的步骤结果卡片。
    const results = task.execution_results || [];
    let doneCount = 0;
    let failedCount = 0;
    let approvalCount = 0;
    let pendingCount = 0;
    for (const r of results) {
      let cardStatus;
      if (r.status === 'done') {
        cardStatus = 'success'; doneCount++;
      } else if (r.status === 'failed') {
        cardStatus = 'failed'; failedCount++;
      } else if (r.status === 'pending_approval') {
        // 门卫拦截 → 待审批，非失败。
        cardStatus = 'pending_approval'; approvalCount++;
      } else {
        cardStatus = 'pending'; pendingCount++;
      }
      const detail = r.error || r.result || '';
      appendToolCard(`${r.action} · ${r.step_id}`, cardStatus, detail || '(无输出)');
    }

    // 状态汇总提示。
    if (results.length === 0) {
      appendMessage('agent', '计划无步骤（未启用 LLM 拆分）。请勾选「LLM 拆分」重新发送需求，或点击「⏸ 中断」。');
    } else if (task.status === 'verifying') {
      appendMessage('agent', `所有 ${results.length} 个步骤执行完成（成功 ${doneCount}），进入验证阶段。`);
    } else {
      const parts = [`成功 ${doneCount}`];
      if (failedCount) parts.push(`失败 ${failedCount}`);
      if (approvalCount) parts.push(`待审批 ${approvalCount}`);
      if (pendingCount) parts.push(`待执行 ${pendingCount}`);
      appendMessage('agent', `执行进度：${parts.join(' / ')} · 共 ${results.length} 个步骤。`);
    }
  } catch (err) {
    appendMessage('agent', `❌ 批准失败：${err.message}`);
  }
}

/** 拒绝计划 */
async function handleReject(card) {
  if (!currentTaskId) return;
  try {
    const task = await api.rejectTask(currentTaskId);
    updateStageBar(task.status);
    clearCardActions(card);
    appendMessage('agent', '计划已拒绝，返回重新规划…');
  } catch (err) {
    appendMessage('agent', `❌ 拒绝失败：${err.message}`);
  }
}

/** 中断任务（仅执行阶段可用） */
async function handleInterrupt() {
  if (!currentTaskId) {
    appendMessage('agent', '当前无运行中的任务');
    return;
  }
  if (currentStatus !== 'executing') {
    appendMessage('agent', `当前状态「${currentStatus || '未知'}」不支持中断，仅执行阶段可中断。`);
    return;
  }
  try {
    const task = await api.interruptTask(currentTaskId, 'user_stop');
    updateStageBar(task.status);
    appendMessage('agent', '任务已中断，可恢复继续执行或放弃任务。');
    appendInterruptCard();
  } catch (err) {
    appendMessage('agent', `❌ 中断失败：${err.message}`);
  }
}

/** 恢复被中断的任务 */
async function handleResume(card) {
  if (!currentTaskId) return;
  try {
    const task = await api.resumeTask(currentTaskId);
    updateStageBar(task.status);
    clearCardActions(card);
    appendMessage('agent', '任务已恢复，继续执行…');
  } catch (err) {
    appendMessage('agent', `❌ 恢复失败：${err.message}`);
  }
}

/** 放弃任务（任意状态可终止） */
async function handleAbort(card) {
  if (!currentTaskId) return;
  try {
    const task = await api.abortTask(currentTaskId);
    updateStageBar(task.status);
    clearCardActions(card);
    appendMessage('agent', '任务已放弃。');
  } catch (err) {
    appendMessage('agent', `❌ 放弃失败：${err.message}`);
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

// 模型切换
document.getElementById('modelSelect').addEventListener('change', (e) => {
  console.log('切换模型:', e.target.value);
});

// 高级选项切换
document.getElementById('advancedSelect').addEventListener('change', (e) => {
  console.log('高级选项:', e.target.value);
});

/* ──────────────── API 密钥管理（安全优先） ────────────────
 * 原则：
 * 1. 明文只在输入框中短暂停留，提交后立即清空输入框；
 * 2. 服务端只回传「是否已配置 + 掩码」，前端不再持有明文；
 * 3. 不写入 localStorage / sessionStorage，不做「显示密钥」功能。
 */

const apiKeyInputChip = document.getElementById('apiKeyInputChip');
const apiKeyStatusChip = document.getElementById('apiKeyStatusChip');
const apiKeyInput = document.getElementById('apiKeyInput');
const apiKeyMasked = document.getElementById('apiKeyMasked');

/** 按服务端状态切换密钥区域形态：已配置 → 只显示掩码 + 删除按钮。 */
function renderApiKeyStatus(status) {
  const configured = Boolean(status && status.configured);
  apiKeyInputChip.hidden = configured;
  apiKeyStatusChip.hidden = !configured;
  if (configured) {
    apiKeyMasked.textContent = status.masked || '已配置';
  } else {
    apiKeyInput.value = ''; // 未配置时不留任何残留明文
  }
}

/** 查询密钥状态；后端未就绪时保持「可输入」形态，不阻塞启动。 */
async function loadApiKeyStatus() {
  try {
    renderApiKeyStatus(await api.getApiKeyStatus());
  } catch {
    renderApiKeyStatus({ configured: false });
  }
}

/** 保存密钥：先清空输入框，再按服务端状态渲染，绝不回显。 */
async function handleApiKeySave() {
  const raw = apiKeyInput.value;
  if (!raw.trim()) return;
  try {
    const status = await api.setApiKey(raw.trim());
    apiKeyInput.value = ''; // 无论成败都立即清空，避免明文驻留
    renderApiKeyStatus(status);
    appendMessage('agent', 'API 密钥已保存到后端（仅驻留内存，不回显）。如需移除可点击「删除密钥」。');
  } catch (err) {
    apiKeyInput.value = '';
    appendMessage('agent', `❌ 密钥保存失败：${err.message}`);
  }
}

/** 删除密钥：二次确认，确认文案不包含任何密钥内容。 */
async function handleApiKeyDelete() {
  if (!window.confirm('确定删除已保存的 API 密钥吗？删除后不可恢复，需要重新输入。')) return;
  try {
    const status = await api.deleteApiKey();
    renderApiKeyStatus(status);
    appendMessage('agent', 'API 密钥已删除。');
  } catch (err) {
    appendMessage('agent', `❌ 密钥删除失败：${err.message}`);
  }
}

document.getElementById('apiKeySave').addEventListener('click', handleApiKeySave);
document.getElementById('apiKeyDelete').addEventListener('click', handleApiKeyDelete);
apiKeyInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    handleApiKeySave();
  }
});

// 技能 toggle（仅左栏技能开关）
document.querySelectorAll('.sidebar-left .toggle input').forEach((t) => {
  t.addEventListener('change', () => console.log('技能开关:', t.checked));
});

// 副结构自检更新开关
const selfCheckToggle = document.getElementById('selfCheckToggle');
selfCheckToggle?.addEventListener('change', () => {
  console.log('副结构自检更新:', selfCheckToggle.checked);
});

// 新建项目按钮
document.querySelector('.new-project-btn').addEventListener('click', () => {
  document.getElementById('chatArea').innerHTML = '';
  currentTaskId = null;
  updateStageBar('received');
  document.getElementById('msgInput').focus();
});

// 刷新按钮 → 重新拉取文件树（后端当前无上传接口）
document.querySelector('.file-refresh-btn')?.addEventListener('click', loadFileTree);

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
  loadApiKeyStatus();
})();
