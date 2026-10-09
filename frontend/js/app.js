/* ============================================================
 * Agent Builder 前端 —— 业务逻辑与 UI 渲染
 * 依赖：api.js（全局 api 对象）
 *
 * 信息架构（IA）：
 *   标题栏 / 菜单栏 / 工具栏（状态提示）/ 三栏（任务列表 · 工作区 · 产物）/ 状态栏
 *
 * 产品原则：
 *   默认直接跑（/plan → 自动 /run），仅在计划里存在**未放行的高风险步骤**时
 *   「到点暂停」并渲染提醒卡；不设固定流程，顶栏只显示单一状态标签，用户随时可中断。
 * ============================================================ */

// 状态机 → 中文状态文案（状态栏 / 任务列表 / 提示语 / 顶栏状态标签）。
const STATUS_LABELS = {
  received: '已接收', planning: '规划中', awaiting_confirm: '等待确认',
  executing: '执行中', verifying: '验证中', reworking: '返工中',
  delivering: '汇报中', delivered: '已交付', interrupted: '已暂停', failed: '已失败',
};

// 状态 → chip 视觉档（只表达「现在是什么状态」，不表达固定路线）
const STATUS_CHIP_STATE = {
  received: 'idle', planning: 'running', awaiting_confirm: 'waiting',
  executing: 'running', verifying: 'running', reworking: 'running',
  delivering: 'running', delivered: 'done',
  interrupted: 'paused', failed: 'failed',
};

/** agentic 循环「非收敛停下」的原因 → 人话（只在过程里，不出现在正常收尾 / 到点暂停上）。
 *
 * 这些原因意味着：模型没把需求做完就停了。此前前端完全不渲染 `loop_state.stopped_reason`，
 * 用户只看到顶栏「执行中」——既不知道为什么停，也没有任何操作入口。
 */
const LOOP_STOP_LABELS = {
  budget_steps: '步数预算用完了',
  budget_tokens: 'token 预算用完了',
  stagnant: '连续两轮没有新进展',
  invalid_decision: '模型没能给出可解析的决策',
};

// 工具卡片状态 → 展示文案（键名同时用作 CSS 类名 .tool-status.<key>）。
const TOOL_STATUS_LABELS = {
  success: '✓ 成功',
  failed: '✗ 失败',
  running: '⟳ 进行中',
  pending: '○ 待执行',
  pending_approval: '⚠ 待审批',
};

// 执行结果 status → 工具卡状态类名（与 TOOL_STATUS_LABELS 的键对齐）。
const EXEC_STATUS_CLASS = {
  done: 'success', failed: 'failed', running: 'running',
  pending_approval: 'pending_approval', skipped: 'pending', pending: 'pending',
};

/* ──────────────── 当前任务状态 ──────────────── */

// 当前激活任务（工作区里正在看的那个）。
let currentTaskId = null;
let currentTaskTitle = '';
let currentStatus = null;
// 当前任务「待放行」清单（到点暂停时后端返回；供菜单/状态栏判断可用性）。
let currentPendingApproval = null;

// 当前任务的「非收敛停下」原因（人话）；空串 = 没有异常停下。顶栏文案据此改写。
let currentStopReason = '';
// 当前任务的计划区块状态：steps / order / parallel_groups / high_risk_actions / execution_results。
let planState = null;
// 发送请求是否在途（防连点重复建任务）。
let isSending = false;
// 后端连通性：null=未知、true/false=已知。
let backendOnline = null;
// 当前工作区展示文案（右栏 / 状态栏）。
let currentWorkspaceLabel = '';

/* ──────────────── 通用工具 ──────────────── */

/** 转义 HTML，避免后端返回内容（工具输出/错误/文件名）注入或破坏布局。 */
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

/** 移除卡片内的操作区（放行/改计划/恢复/放弃等按钮）。 */
function clearCardActions(card) {
  if (!card) return;
  card.querySelectorAll('.approval-actions').forEach((el) => el.remove());
}

/** 取卡片所属任务 ID（卡片作用域；不读全局 currentTaskId，避免跨任务误操作）。 */
function cardTaskId(card) {
  return (card && card.dataset.taskId) || null;
}

/** 菜单等**非卡片入口**用的任务作用域壳：只承载 ``dataset.taskId``。
 *
 * 这些入口（改计划 / 放弃）本来借用计划区块或提醒卡当"卡片"来取任务 ID；
 * 计划已折进「过程」块、不再有独立槽位，所以用一个轻量壳，语义更直白。
 */
function taskScopeEl(taskId) {
  const el = document.createElement('div');
  el.dataset.taskId = taskId || currentTaskId || '';
  return el;
}

/** 仅当卡片属于当前任务时才更新工具栏/状态栏，避免旧任务状态覆盖当前任务。 */
function syncTaskStatus(taskId, status) {
  if (taskId && taskId === currentTaskId) updateTaskStatus(status);
}

/** 状态文案兜底。 */
function statusLabel(status) {
  return STATUS_LABELS[status] || status || '未知';
}

/** 从 ``` 围栏文本中提取代码（无围栏返回空串）。 */
function extractCodeBlock(text) {
  if (!text || typeof text !== 'string') return '';
  const match = text.match(/```[^\n]*\n([\s\S]*?)```/);
  return match ? match[1].replace(/\s+$/, '') : '';
}

/** 输入摘要：优先后端下发的人读描述（方案 A 的 `description`）。
 *
 * 回退路径**不再产出 `key=value`** —— 那正是待消除的第 ③ 类机器输出。
 */
function summarizeInputs(step) {
  const desc = step && typeof step.description === 'string' ? step.description.trim() : '';
  if (desc) return desc;
  const inputs = step && step.inputs ? step.inputs : null;
  if (!inputs || typeof inputs !== 'object') return '—';
  const path = typeof inputs.path === 'string' && inputs.path ? inputs.path : '';
  if (path) return `文件：${path}`;
  const values = Object.keys(inputs)
    .slice(0, 3)
    .map((key) => {
      const value = inputs[key];
      if (typeof value === 'string' && value.trim()) return value.trim().slice(0, 60);
      if (typeof value === 'number' || typeof value === 'boolean') return String(value);
      return '';
    })
    .filter(Boolean);
  return values.length ? `参数：${values.join('、')}` : '—';
}

/** 取某步骤**真正产出**的文件路径；没有产出返回 null。
 *
 * 不能用 `step.inputs.path` 冒充：只读动作（列出文件 / 读取文件）的 path 是「被访问的
 * 对象」（可能是 `.` 这种目录），拿它当产物 → 点开就是「路径不是文件: .」。
 */
function stepArtifactPath(item) {
  const artifacts = item && Array.isArray(item.artifacts) ? item.artifacts.filter(Boolean) : [];
  return artifacts.length ? String(artifacts[0]) : null;
}

/** 取某步骤的人读标题（后端下发的中文动作名）；缺失时回退英文 action。 */
function stepTitle(stepId, fallback) {
  const step = planState && planState.steps ? planState.steps[stepId] : null;
  const title = step && typeof step.title === 'string' ? step.title.trim() : '';
  return title || fallback || '步骤';
}

/** 取某个动作的人读中文名（按 action 反查计划里同名步骤的 title）。
 *
 * 待放行工具清单、高风险动作提示只拿得到 action 字符串，用它换成人话，
 * 避免英文 action 名直接露给用户（第 ② 类机器输出）。
 */
function actionTitle(action, fallback) {
  const steps = (planState && planState.steps) || {};
  for (const id of Object.keys(steps)) {
    const step = steps[id];
    if (
      step &&
      step.action === action &&
      typeof step.title === 'string' &&
      step.title.trim()
    ) {
      return step.title.trim();
    }
  }
  return fallback || action || '动作';
}

/** 异常原文的人话收敛：剥掉 `RuntimeError:` / `AgentError:` / `工具名:` 前缀。
 *
 * 与后端 `narrate.humanize_error` 同口径，是"后端没给 summary"时的最后一道收口 ——
 * 目的是绝不把异常原文倒给用户（第 ④ 类机器输出）。
 */
function humanizeError(text) {
  if (typeof text !== 'string' || !text.trim()) return '这一步失败了';
  const cleaned = text
    .replace(/^[A-Za-z_]*Error:\s*/g, '')
    .replace(/^[A-Za-z_]*Exception:\s*/g, '')
    .replace(/^E_[A-Z0-9_]+:\s*/, '')
    .replace(/^[a-z][a-z0-9_]*:\s*/, '')
    .trim();
  return cleaned || '这一步失败了';
}

/** 工具卡默认展示内容：只给人话。
 *
 * 后端 `summary`（方案 A 的产物）优先；缺失时失败走人话收敛、成功只接受字符串结果。
 * **原始 `result` / `error` 一律不进默认视图** —— 它们正是第 ① 与第 ④ 类机器输出，
 * 仍保留在任务审计数据里（`GET /tasks/{id}`）。
 */
function toolCardDetail(item) {
  if (!item) return '(无输出)';
  if (item.summary) return item.summary;
  if (item.status === 'failed') return humanizeError(item.error);
  if (item.status === 'pending_approval') return '等待你放行后执行';
  const result = item.result;
  if (typeof result === 'string' && result.trim()) return result;
  return '(无输出)';
}

/* ──────────────── 会话上限与持久化（多任务并存）─────────────── */

// 单个任务对话区最多保留的条目数：DOM 与持久化记录按同一上限裁剪。
const MAX_CHAT_ITEMS = 200;

// sessionStorage 中最多保留的任务会话数（防无限膨胀）。
const MAX_TASKS = 20;

// 会话存储键。用 sessionStorage：刷新不丢，关闭标签页即清，不做长期落盘。
const SESSION_KEY = 'agent-builder:session';

/** 当前执行形态：`agentic` = 由模型逐轮决定下一步，`workflow` = 固定工作流。
 *
 * 与后端 `PlanRequest.mode` 的默认值保持一致（改这里要同步后端默认值）。
 * agentic 下**没有**「一份固定的 DAG 计划」可展示——步骤是模型逐轮长出来的，
 * 实际过程收在「过程」块里，所以计划块只在 workflow 模式渲染。
 */
const EXECUTION_MODE = 'agentic';

// 当前任务可见的会话记录（消息 + 工具卡片）。
let transcript = [];

// 是否已发生过裁剪（提示节点用；持久化以便刷新后仍显示提示）。
let trimmed = false;

// 所有任务的会话记录：taskId → { title, entries, plan }（供切换回看）。
let taskSessions = {};

/** 把节点挂到对话区并滚动到底。
 *  插入目标是内层 live region（#chatLog），滚动容器仍是外层 #chatArea。 */
function mountChatNode(node) {
  const log = document.getElementById('chatLog');
  log.appendChild(node);
  const area = document.getElementById('chatArea');
  area.scrollTop = area.scrollHeight;
}

/* ──────────────── 过程块：默认收起的过程收纳 ──────────────── */

/** 当前过程块（对话流里收纳工具卡 / 过程说明的唯一折叠容器）。 */
let currentProcessBlock = null;

/** 当前结论块（一个任务只保留一条，避免每轮刷新叠出一堆结论）。 */
let conclusionBlockEl = null;

/** 最近一次用于刷新过程块的任务快照。
 *
 * 过程块的摘要（含「用时」）依赖任务时间戳，但 `mountProcessNode` 追加节点时**没有**任务
 * 上下文；不记住上一次的快照，后到的追加会把带用时的摘要覆盖成不带用时的版本。
 */
let lastProcessTask = null;

/** 取当前过程块，需要时新建。
 *
 * 它**自身就是 `.chat-item`**：对 `trimChatArea` 而言过程块只算一条，
 * 内部的工具卡不再带 `.chat-item`，因此裁剪与"条目数上限"不变式都不受影响。
 */
function ensureProcessBlock() {
  if (currentProcessBlock && currentProcessBlock.isConnected) return currentProcessBlock;
  const block = document.createElement('details');
  block.className = 'process-block chat-item';
  const summary = document.createElement('summary');
  summary.className = 'process-summary';
  summary.textContent = '过程';
  const body = document.createElement('div');
  body.className = 'process-body';
  block.appendChild(summary);
  block.appendChild(body);
  mountChatNode(block);
  currentProcessBlock = block;
  return block;
}

/** 把「过程类」节点收进过程块（去掉 .chat-item，避免与容器重复计数）。 */
function mountProcessNode(node) {
  const block = ensureProcessBlock();
  node.classList.remove('chat-item');
  block.querySelector('.process-body').appendChild(node);
  refreshProcessBlock();
  const area = document.getElementById('chatArea');
  if (area) area.scrollTop = area.scrollHeight;
}

/** 追加一条过程说明（自检结论等）：收进过程块，不占对话正文。 */
function appendProcessNote(text, options) {
  const note = document.createElement('div');
  note.className = 'process-note';
  if (options && options.failed) note.dataset.failed = '1';
  note.textContent = text;
  mountProcessNode(note);
  recordEntry({ kind: 'process-note', text, failed: !!(options && options.failed) });
}

/** 过程块里是否有"不能让用户错过"的失败：工具失败或自检发现问题。 */
function processHasFailure() {
  const block = currentProcessBlock;
  if (!block) return false;
  if (block.querySelector('.tool-status.failed')) return true;
  return !!block.querySelector('.process-note[data-failed="1"]');
}

/** 任务总耗时文本（后端暂无单步耗时，先给总时长）。 */
function taskElapsedText(task) {
  const start = task && task.created_at ? Date.parse(task.created_at) : NaN;
  const end = task && task.updated_at ? Date.parse(task.updated_at) : NaN;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '';
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

/** 是否需要"强制展开"过程块：有失败 / 有待放行 / 有需要用户决定的状态。 */
function shouldExpandProcess(task) {
  if (processHasFailure()) return true;
  if (!task) return false;
  if (task.pending_approval) return true;
  if (task.pending_proposal) return true;
  return task.status === 'awaiting_confirm' || task.status === 'interrupted';
}

/** 刷新过程块摘要与展开态。
 *
 * 默认收起；出现上面三类情况**自动展开一次**（用 data-auto-opened 记账，
 * 用户手动收起后不会每轮刷新又被弹开）。
 */
function refreshProcessBlock(task) {
  // 带任务上下文时**先**记下来：本次调用可能早于过程块的创建（过程块由第一批工具卡
  // 追加时才建），那时下面会提前 return —— 不能因此丢掉任务快照，否则随后「无上下文」
  // 的刷新算不出「用时」，摘要会被覆盖成不带用时的版本。
  if (task) lastProcessTask = task;
  const block = currentProcessBlock;
  if (!block || !block.isConnected) return;
  const ctx = task || lastProcessTask;
  const body = block.querySelector('.process-body');
  const cards = body.querySelectorAll('.tool-card');
  const failed = body.querySelectorAll('.tool-status.failed').length;
  // 「待审批」既不算完成也不算失败 —— 混进「已完成」会让摘要在"到点暂停"时虚报步数。
  const waiting = body.querySelectorAll('.tool-status.pending_approval').length;
  const elapsed = taskElapsedText(ctx);
  // 摘要行对齐 DeepSeek 网页版："已深度思考（用时 N 秒）"，后面补上可核对的步数。
  const parts = [elapsed ? `已深度思考（用时 ${elapsed}）` : '已深度思考'];
  parts.push(`已完成 ${Math.max(0, cards.length - failed - waiting)} 步`);
  if (waiting) parts.push(`待放行 ${waiting} 步`);
  if (failed) parts.push(`失败 ${failed} 次`);
  block.querySelector('.process-summary').textContent = parts.join(' · ');

  if (shouldExpandProcess(ctx)) {
    if (block.dataset.autoOpened !== '1') {
      block.open = true;
      block.dataset.autoOpened = '1';
    }
  } else {
    block.dataset.autoOpened = '0';
  }
}

/** 裁剪超限的最早对话条目；仅统计/裁剪 .chat-item，提示节点与结构化区块不占名额。
 *
 * 不变式：对话条目数（.chat-item）恒不超过 MAX_CHAT_ITEMS，
 * 且恢复后的条目与持久化记录一致 → 可反复刷新而不丢消息。
 */
function trimChatArea() {
  const log = document.getElementById('chatLog');
  const items = () => log.querySelectorAll(':scope > .chat-item');
  while (items().length > MAX_CHAT_ITEMS) {
    items()[0].remove();
    trimmed = true;
  }
  if (trimmed && !log.querySelector('.chat-trimmed')) {
    const notice = document.createElement('div');
    notice.className = 'chat-trimmed';
    notice.textContent = '…更早的消息已省略';
    log.insertBefore(notice, log.firstChild);
  }
}

/** 记录一条会话条目（写入 transcript → 裁剪 → 持久化）。
 *
 * 带 ``stepId`` 的条目**同 kind 同 stepId 只保留一条**：放行后 ``/resume`` 会回传全部执行
 * 结果，不去重的话刷新时会按旧记录重建出一堆重复卡片。
 */
function recordEntry(entry) {
  if (entry && entry.stepId) {
    transcript = transcript.filter(
      (item) => !(item && item.kind === entry.kind && item.stepId === entry.stepId),
    );
  }
  transcript.push(entry);
  if (transcript.length > MAX_CHAT_ITEMS) {
    transcript.splice(0, transcript.length - MAX_CHAT_ITEMS);
  }
  trimChatArea();
  saveSession();
}

/** 把当前任务的会话快照进任务表（供切换回看）。 */
function snapshotCurrentTask() {
  if (!currentTaskId) return;
  taskSessions[currentTaskId] = {
    title: currentTaskTitle,
    entries: transcript,
    plan: planState,
  };
}

/** 持久化会话（含当前任务与各任务历史）。 */
function saveSession() {
  snapshotCurrentTask();
  try {
    const ids = Object.keys(taskSessions);
    if (ids.length > MAX_TASKS) {
      const keep = ids.slice(ids.length - MAX_TASKS);
      const pruned = {};
      keep.forEach((id) => { pruned[id] = taskSessions[id]; });
      taskSessions = pruned;
    }
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      currentTaskId,
      tasks: taskSessions,
      trimmed,
    }));
  } catch (err) {
    console.warn('会话持久化失败（忽略）：', err.message);
  }
}

/** 读取持久化会话；无记录或数据损坏返回 null。 */
function loadSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const data = JSON.parse(raw);
    if (!data || typeof data !== 'object') return null;
    return data;
  } catch (err) {
    console.warn('会话恢复失败（忽略）：', err.message);
    return null;
  }
}

/** 重置当前可见视图（不动其它任务的历史会话）。 */
function resetActiveView() {
  transcript = [];
  trimmed = false;
  planState = null;
  currentPendingApproval = null;
  currentStopReason = '';
  conclusionBlockEl = null;
  lastProcessTask = null;
  const log = document.getElementById('chatLog');
  if (log) log.innerHTML = '';
  const actionSlot = document.getElementById('actionSlot');
  if (actionSlot) actionSlot.innerHTML = '';
  renderPlanBlock(null);
  // 交接提示计量随会话重置（卡片不持久化，刷新即消失）。
  handoverState.turns = 0;
  handoverState.contextChars = 0;
  handoverState.data = null;
  handoverState.dismissed = false;
  handoverState.error = null;
  const slot = document.getElementById('handoverSlot');
  if (slot) slot.innerHTML = '';
}

/** 清空全部会话（含其它任务历史）——切换工作区时使用。 */
function clearSession() {
  resetActiveView();
  taskSessions = {};
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch (err) {
    console.warn('会话清理失败（忽略）：', err.message);
  }
}

/** 重建刷新前的会话：先恢复对话记录，再按后端当前状态重连任务与可操作入口。
 *
 * Returns:
 *    是否成功恢复了历史会话（用于避免重复插入连接提示）。
 */
async function restoreSession() {
  const data = loadSession();
  if (!data) return false;
  taskSessions = data.tasks || {};
  trimmed = data.trimmed === true;
  const taskId = data.currentTaskId;
  if (taskId && taskSessions[taskId]) {
    currentTaskId = taskId;
    currentTaskTitle = taskSessions[taskId].title || '';
    transcript = (taskSessions[taskId].entries || []).slice(0, MAX_CHAT_ITEMS);
    planState = taskSessions[taskId].plan || null;
  }
  for (const entry of transcript) renderEntry(entry);
  trimChatArea();
  renderPlanBlock(planState);
  updateTitlebar();
  if (!currentTaskId) return true;

  try {
    const task = await api.getTask(currentTaskId);
    applyTaskToView(task);
  } catch (err) {
    const message = String(err.message || '');
    if (message.includes('task not found')) {
      // 后端重启后内存任务已丢 → 提示并重置，不阻断继续使用。
      currentTaskId = null;
      currentTaskTitle = '';
      appendMessage('agent', '⚠️ 之前的任务已不存在（后端可能已重启），已重置为新建任务。');
    } else {
      appendMessage('agent', `⚠️ 无法恢复之前的任务：${message}`);
    }
  }
  return true;
}

/** 按持久化条目重建对话（不写回 transcript，避免重复记录）。 */
function renderEntry(entry) {
  if (!entry || typeof entry !== 'object') return;
  if (entry.kind === 'msg') renderMessage(entry.role, entry.text);
  else if (entry.kind === 'tool') {
    // 重建时按步骤号去重。历史记录可能没有 stepId（旧版本写入的），从标题回推 ——
    // 键必须与后端 execution_results 的 step_id 一致，否则与 applyTaskToView 的渲染打架。
    const stepKey = entry.stepId || stepIdFromTitle(entry.title);
    dropPreviousStepNodes(stepKey, '.tool-card');
    renderToolCard(entry.title, entry.status, entry.detail, {
      path: entry.path,
      thought: entry.thought,
      stepId: stepKey,
    });
  } else if (entry.kind === 'process-note') appendProcessNote(entry.text, { failed: entry.failed });
  else if (entry.kind === 'artifact') {
    const stepKey = entry.stepId || stepIdFromTitle(entry.title) || entry.path || '';
    dropPreviousStepNodes(stepKey, '.artifact-row');
    renderArtifactRow(entry.path, entry.title, stepKey);
  }
  else if (entry.kind === 'council') renderCouncilCard(entry.minutes);
  else if (entry.kind === 'noplan') renderNoPlanCard(entry.taskId, entry.questions, { canReject: entry.canReject });
}

/** 刷新后/切换后按任务当前状态重建可操作入口（卡片绑定任务 ID，避免跨任务误操作）。 */
function reopenTaskEntry(task) {
  const shortId = String(task.task_id).slice(0, 8);
  if (task.status === 'awaiting_confirm') {
    appendMessage('agent', `已恢复任务 ${shortId}…（等待确认计划，可「改计划」或重新发送需求）`);
  } else if (task.status === 'interrupted' && !task.pending_approval) {
    appendMessage('agent', `已恢复任务 ${shortId}…（已暂停，可恢复继续或放弃）`);
  } else if (task.status === 'executing') {
    appendMessage('agent', `已恢复任务 ${shortId}…（执行中，可用「⏸ 中断」）`);
  } else {
    appendMessage('agent', `已恢复任务 ${shortId}…（状态：${statusLabel(task.status)}）`);
  }
}

/* ──────────────── 交接提示（handover hint）────────────────
 * 规则式判定（不调 LLM）：门控（轮次 / token / 字符）+ 未决项 + 任务状态综合。
 * 卡片**不写入 transcript**（仅后端记 ack），刷新即消失。
 * turns = 用户消息数；context_chars = 所有消息文本累计；context_tokens ≈ chars / 4。
 */

// 交接提示状态（仅内存，不持久化）。
const handoverState = {
  turns: 0,            // 用户消息数
  contextChars: 0,     // 所有消息文本累计字符数
  data: null,          // 后端返回的交接对象
  pick: 'human',       // 用户选择先处理哪一类未决（人的未决优先）
  dismissed: false,    // 是否已「暂不处理」（ignored）
  loading: false,
  error: null,
};

// token 估算系数（中文约 0.6–1 token/字，混合文本取保守值 4 字符 ≈ 1 token）。
const CHARS_PER_TOKEN = 4;

/** 累计消息到交接计量（用户消息计入 turns，所有消息计入 chars）。 */
function accountHandover(role, text) {
  if (role === 'user') handoverState.turns += 1;
  handoverState.contextChars += String(text || '').length;
}

/** 触发交接判定（幂等；已 dismissed 或无 taskId 时跳过）。 */
async function checkHandover() {
  if (!currentTaskId || handoverState.dismissed) return;
  const slot = document.getElementById('handoverSlot');
  if (!slot) return;
  handoverState.loading = true;
  handoverState.error = null;
  renderHandover();
  try {
    const data = await api.getHandover(currentTaskId, {
      turns: handoverState.turns,
      contextTokens: Math.floor(handoverState.contextChars / CHARS_PER_TOKEN),
      contextChars: handoverState.contextChars,
    });
    handoverState.data = data;
    handoverState.dismissed = false;
    if (data.human_open_questions && data.human_open_questions.length) {
      handoverState.pick = 'human';
    } else {
      handoverState.pick = 'machine';
    }
  } catch (err) {
    handoverState.data = null;
    handoverState.error = err;
  } finally {
    handoverState.loading = false;
    renderHandover();
  }
}

function handoverStrengthLabel(strength) {
  return { strong: '强烈建议开新对话', suggest: '建议开新对话', none: '暂不需要交接' }[strength] || '';
}

function handoverEffectivePick(data) {
  const hasHuman = (data.human_open_questions || []).length > 0;
  const hasMachine = (data.machine_open_items || []).length > 0;
  if (handoverState.pick === 'human') return hasHuman ? 'human' : 'machine';
  return hasMachine ? 'machine' : 'human';
}

function handoverFmtNum(n) {
  return Number(n || 0).toLocaleString('zh-CN');
}

/** 组装「复制到新对话首条消息」的纯文本（只带选中那一类未决）。 */
function buildHandoverCopyText(data, pick) {
  const lines = [];
  const pickLabel = pick === 'human' ? '人的未决' : '机器未决';
  const g = data.gate || {};
  lines.push(`【交接提示｜${handoverStrengthLabel(data.strength)} · 先处理：${pickLabel}】`);
  lines.push(`任务：${data.task_id} · 状态：${data.task_status}`);
  lines.push(
    `触发：对话轮次 ${g.turns} / ${g.turns_threshold}${g.turns_hit ? '（已超）' : ''} · ` +
    `累计 token ${handoverFmtNum(g.tokens)} / ${handoverFmtNum(g.tokens_threshold)}${g.tokens_hit ? '（已超）' : ''}`
  );
  lines.push(`判定：${(data.reasons || []).join('；')}`);
  lines.push('');
  if (pick === 'human') {
    const items = data.human_open_questions || [];
    if (items.length) {
      lines.push('一、人的未决（优先）');
      items.forEach((item, i) => lines.push(`${i + 1}. ${item.text}（来源：${item.source}）`));
      lines.push('');
    }
    if ((data.machine_open_items || []).length) {
      lines.push(`（另有机器未决 ${data.machine_open_items.length} 项未选，可在卡片中切换后再次复制）`);
      lines.push('');
    }
  } else {
    const items = data.machine_open_items || [];
    if (items.length) {
      lines.push('一、机器未决');
      items.forEach((item) => lines.push(`- ${item.step_id} ${item.title || item.action} [${item.status}] ${item.detail}`));
      lines.push('');
    }
    if ((data.human_open_questions || []).length) {
      lines.push(`（另有人的未决 ${data.human_open_questions.length} 项未选——注意：人的未决优先级更高）`);
      lines.push('');
    }
  }
  const actions = data.next_actions || [];
  if (actions.length) {
    lines.push('二、建议下一步');
    actions.forEach((text, i) => lines.push(`${i + 1}. ${text}`));
    lines.push('');
  }
  lines.push('（本提示由本地规则式判定生成，未调用模型；粘贴到新对话首条消息即可接续。）');
  return lines.join('\n');
}

function renderHandoverCard(data) {
  const copyText = buildHandoverCopyText(data, handoverEffectivePick(data));
  const pick = handoverEffectivePick(data);
  const g = data.gate || {};
  const humanCount = (data.human_open_questions || []).length;
  const machineCount = (data.machine_open_items || []).length;
  const ackLabel = { none: '', seen: '已查看', ignored: '已忽略' }[(data.ack || {}).status] || '';
  const hasOpen = humanCount || machineCount;

  const humanRows = (data.human_open_questions || [])
    .map((item) => `<li class="hn-item"><span class="hn-src">${escapeHtml(item.source)}</span><span class="hn-text">${escapeHtml(item.text)}</span></li>`)
    .join('');
  const machineRows = (data.machine_open_items || [])
    .map((item) => `<li class="hn-item"><span class="hn-step">${escapeHtml(item.step_id)} ${escapeHtml(item.title || item.action)}</span><span class="hn-status ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span><span class="hn-text">${escapeHtml(item.detail)}</span></li>`)
    .join('');
  const actionRows = (data.next_actions || [])
    .map((text) => `<li class="hn-item"><span class="hn-text">${escapeHtml(text)}</span></li>`)
    .join('');

  return `
    <article class="hn-card hn-${escapeHtml(data.strength)}" aria-labelledby="hnTitle">
      <div class="hn-head">
        <span class="hn-icon" aria-hidden="true">↔</span>
        <h2 class="hn-title" id="hnTitle">交接提示</h2>
        <span class="hn-badge hn-badge-${escapeHtml(data.strength)}">${handoverStrengthLabel(data.strength)}</span>
      </div>
      <p class="hn-reason">${(data.reasons || []).map(escapeHtml).join(' · ')}</p>
      <div class="hn-gate">
        <span class="hn-gate-item${g.turns_hit ? ' hit' : ''}">对话轮次 ${g.turns} / ${g.turns_threshold}</span>
        <span class="hn-gate-item${g.tokens_hit ? ' hit' : ''}">累计 token ${handoverFmtNum(g.tokens)} / ${handoverFmtNum(g.tokens_threshold)}</span>
        <span class="hn-gate-item${g.chars_hit ? ' hit' : ''}">字符 ${handoverFmtNum(g.chars)} / ${handoverFmtNum(g.chars_threshold)}</span>
        <span class="hn-gate-item">已提示 ${g.prompts_shown} / ${g.max_prompts}</span>
        ${ackLabel ? `<span class="hn-ack hn-ack-${escapeHtml((data.ack || {}).status)}">${ackLabel}</span>` : ''}
      </div>
      ${hasOpen ? `
      <div class="hn-choice">
        <p class="hn-choice-note">人的未决优先级更高：它决定「新对话该先问什么」；机器未决是执行层收尾。请选择先处理哪一类——复制内容只带出你选中的那一类。</p>
        <div class="hn-choice-seg" role="group" aria-label="选择先处理哪一类未决">
          <button class="seg-btn${pick === 'human' ? ' is-active' : ''}" type="button" data-hn-act="pick" data-hn-pick="human" aria-pressed="${pick === 'human'}" ${humanCount ? '' : 'disabled'}>人的未决（优先）· ${humanCount}</button>
          <button class="seg-btn${pick === 'machine' ? ' is-active' : ''}" type="button" data-hn-act="pick" data-hn-pick="machine" aria-pressed="${pick === 'machine'}" ${machineCount ? '' : 'disabled'}>机器未决 · ${machineCount}</button>
        </div>
        <ul class="hn-list">${pick === 'human' ? humanRows : machineRows}</ul>
      </div>` : ''}
      ${actionRows ? `<section class="hn-section"><h3 class="hn-section-title">接着做什么</h3><ol class="hn-list hn-list-ordered">${actionRows}</ol></section>` : ''}
      <details class="hn-preview">
        <summary>预览「复制」内容</summary>
        <pre class="hn-pre">${escapeHtml(copyText)}</pre>
      </details>
      <div class="hn-actions-bar">
        <button class="btn btn-primary" type="button" data-hn-act="copy"><span class="btn-label">复制交接提示</span></button>
        <button class="btn btn-outline" type="button" data-hn-act="start">一键新开对话</button>
        <button class="btn btn-ghost" type="button" data-hn-act="dismiss">暂不处理</button>
        <span class="hn-note">卡片不写入 transcript；仅后端记 ack</span>
      </div>
    </article>`;
}

function renderHandoverNone(data) {
  const g = data.gate || {};
  return `
    <article class="hn-card hn-none">
      <div class="hn-head">
        <span class="hn-icon" aria-hidden="true">↔</span>
        <h2 class="hn-title">暂不需要交接</h2>
        <span class="hn-metric">轮次 ${g.turns}/${g.turns_threshold} · token ${handoverFmtNum(g.tokens)}/${handoverFmtNum(g.tokens_threshold)}</span>
      </div>
      <p class="hn-reason">${escapeHtml((data.reasons || []).join('；'))}</p>
    </article>`;
}

function renderHandover() {
  const slot = document.getElementById('handoverSlot');
  if (!slot) return;
  if (handoverState.loading) {
    slot.innerHTML = `<article class="hn-card hn-skeleton" aria-busy="true"><div class="hn-head"><span class="hn-icon" aria-hidden="true">⏳</span><span>正在请求后端判定…</span></div></article>`;
    return;
  }
  if (handoverState.error) {
    const msg = handoverState.error && handoverState.error.message ? handoverState.error.message : String(handoverState.error);
    slot.innerHTML = `<article class="hn-card hn-error" role="alert"><div class="hn-head"><span class="hn-icon" aria-hidden="true">⚠</span><h2 class="hn-title">交接判定获取失败</h2></div><p class="hn-reason">${escapeHtml(msg)}</p></article>`;
    return;
  }
  if (handoverState.dismissed) {
    slot.innerHTML = `<div class="hn-dismissed"><span>交接提示已忽略（后端已记录 ack=ignored，本会话不再重复提示）</span><button class="hn-link" type="button" data-hn-act="restore">恢复显示</button></div>`;
    return;
  }
  if (!handoverState.data) { slot.innerHTML = ''; return; }
  slot.innerHTML = handoverState.data.should_suggest ? renderHandoverCard(handoverState.data) : renderHandoverNone(handoverState.data);
}

function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', 'readonly');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); resolve(); }
    catch (err) { reject(err); }
    finally { document.body.removeChild(area); }
  });
}

function bindHandoverSlot() {
  const slot = document.getElementById('handoverSlot');
  if (!slot) return;
  slot.addEventListener('click', async (event) => {
    const trigger = event.target.closest('[data-hn-act]');
    if (!trigger) return;
    const act = trigger.getAttribute('data-hn-act');
    const data = handoverState.data;
    if (!data && act !== 'restore') return;

    if (act === 'pick') {
      handoverState.pick = trigger.getAttribute('data-hn-pick');
      renderHandover();
    } else if (act === 'dismiss') {
      try {
        await api.ackHandover(data.task_id, 'ignored');
        handoverState.dismissed = true;
        renderHandover();
      } catch (err) { console.warn('记录 ack 失败（忽略）：', err.message); }
    } else if (act === 'restore') {
      handoverState.dismissed = false;
      checkHandover();
    } else if (act === 'copy') {
      const text = buildHandoverCopyText(data, handoverEffectivePick(data));
      try {
        await copyToClipboard(text);
        const label = trigger.querySelector('.btn-label');
        if (label) label.textContent = '已复制';
        trigger.classList.add('is-done');
        setTimeout(() => { if (label) label.textContent = '复制交接提示'; trigger.classList.remove('is-done'); }, 1600);
        try { await api.ackHandover(data.task_id, 'seen'); } catch (err) { /* 不阻断复制反馈 */ }
      } catch (err) { console.warn('复制失败：', err.message); }
    } else if (act === 'start') {
      const text = buildHandoverCopyText(data, handoverEffectivePick(data));
      try { await copyToClipboard(text); } catch (err) { /* 不阻断 */ }
      try { await api.ackHandover(data.task_id, 'seen'); } catch (err) { /* 不阻断 */ }
      // 一键新开对话：复制内容到剪贴板 + 清空当前对话（不自动建任务，由用户粘贴接续）。
      const input = document.getElementById('msgInput');
      if (input) { input.value = text; autoGrowInput(); input.focus(); }
      handoverState.dismissed = true;
      renderHandover();
    }
  });
}

/* ──────────────── 工具栏 / 标题栏 / 状态栏 ──────────────── */

/** 更新工具栏的「当前状态」标签（单一状态标签，只表达状态，不表达固定流程）。 */
function updateTaskStatus(status) {
  currentStatus = status;
  // 有未放行的待办时，状态实际是「等用户决定」：文案与视觉档都以 waiting 为准。
  const waiting = Boolean(currentPendingApproval);
  // 非收敛停下同理：后端状态还停在 executing，但对用户而言就是「已停下」（等处理）。
  const stopped = !waiting && Boolean(currentStopReason);
  const label = waiting ? '等待放行' : (stopped ? '已停下' : statusLabel(status));
  const chipState = waiting ? 'waiting' : (stopped ? 'paused' : (STATUS_CHIP_STATE[status] || 'idle'));
  if (taskStatusTextEl) taskStatusTextEl.textContent = label;
  if (taskStatusChipEl) taskStatusChipEl.dataset.state = chipState;
  // 仅执行阶段允许中断（对应后端 EXECUTING → INTERRUPTED）；已停下的任务不该再给「中断」。
  if (interruptBtnEl) interruptBtnEl.disabled = status !== 'executing' || stopped;
  // 计划区块的「改计划」入口依赖状态（仅在 awaiting_confirm 时可改）。
  renderPlanBlock(planState);
  updateStatusTask();
}

/** 乐观把「当前任务」置为执行中（发 /run、/resume 之前调用）。
 *
 * `/run`、`/resume` 都是**同步阻塞**接口（整段 agentic 循环跑完才返回），执行期间
 * 前端拿不到 `executing` → 「⏸ 中断」按钮（仅在 executing 且未停下时可点）会一直灰着。
 * 这里先置位，让中断按钮在执行期间可用；真实终态由响应回来后的 `applyRunResult`
 * 覆盖（后端会把中断后的状态如实回传）。
 */
function markTaskRunning(taskId) {
  if (taskId && taskId !== currentTaskId) return;
  currentPendingApproval = null;
  currentStopReason = '';
  updateTaskStatus('executing');
}

/** 更新标题栏与工具栏的当前任务标题。 */
function updateTitlebar() {
  const label = currentTaskId
    ? (currentTaskTitle || `任务 ${String(currentTaskId).slice(0, 8)}…`)
    : '未创建任务';
  if (titlebarTaskEl) {
    titlebarTaskEl.textContent = label;
    titlebarTaskEl.title = currentTaskId ? `任务 ID：${currentTaskId}` : '当前任务';
  }
  if (toolbarTaskEl) toolbarTaskEl.textContent = label;
}

/** 状态栏：后端连通性。 */
function updateStatusBackend() {
  if (statusDotEl) statusDotEl.classList.toggle('off', backendOnline !== true);
  if (statusBackendTextEl) {
    statusBackendTextEl.textContent = backendOnline === true
      ? '后端已连接'
      : (backendOnline === false ? '后端未连接' : '后端自检中…');
  }
}

/** 状态栏：当前任务状态。 */
function updateStatusTask() {
  if (!statusTaskEl) return;
  statusTaskEl.textContent = currentTaskId
    ? `任务：${statusLabel(currentStatus)}${currentPendingApproval ? ' · 待放行' : ''}`
    : '任务：未创建';
}

/** 状态栏：当前模型。 */
function updateStatusModel() {
  if (statusModelEl) statusModelEl.textContent = `模型：${modelSelect ? modelSelect.value : '—'}`;
}

/** 状态栏：当前工作区。 */
function updateStatusProject() {
  if (statusProjectEl) statusProjectEl.textContent = `📁 ${currentWorkspaceLabel || '—'}`;
}

/** 状态栏：API 密钥状态。 */
function updateStatusKey(status) {
  if (!statusKeyEl) return;
  if (status && status.configured) {
    statusKeyEl.textContent = `⚿ 密钥已配置${status.expires_at ? ' · 有期限' : ''}`;
  } else if (status && status.expired) {
    statusKeyEl.textContent = '⚿ 密钥已过期';
  } else {
    statusKeyEl.textContent = '⚿ 未配置密钥';
  }
}

/* ──────────────── 对话渲染 ──────────────── */

/** 渲染一条消息气泡（只渲染，不写会话记录）。 */
function renderMessage(role, text) {
  const wrap = document.createElement('div');
  wrap.className = `chat-item ${role === 'user' ? 'msg-user' : 'msg-agent'}`;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(bubble);
  mountChatNode(wrap);
}

/** 在对话区追加一条消息（渲染 + 记录）。 */
function appendMessage(role, text) {
  renderMessage(role, text);
  recordEntry({ kind: 'msg', role, text });
  // 计入交接提示计量（用户消息计 turns，所有消息计 chars）。
  accountHandover(role, text);
}

/** 渲染工具调用卡片（只渲染，不写会话记录）。
 *
 * 若输出含 ``` 代码块，用 <pre><code> 渲染（内容经 escapeHtml 转义）；
 * 若该步骤有工作区产物路径，给出「查看产物」入口。
 */
function renderToolCard(title, status, detail, extra) {
  const card = document.createElement('div');
  card.className = 'tool-card chat-item';
  // 步骤标识：同一个 step 只允许存在一张卡（放行后后端会回传**全部**执行结果）。
  if (extra && extra.stepId) card.dataset.stepId = String(extra.stepId);
  const cls = TOOL_STATUS_LABELS[status] ? status : 'pending';
  const path = extra && extra.path ? extra.path : '';
  // 这一轮「为什么这么做」（agentic 的决策理由）：思考同样收进过程块，
  // 否则它既不在正文、也不在过程里，等于被丢掉。
  const thought = extra && extra.thought ? String(extra.thought).trim() : '';
  const code = extractCodeBlock(detail);
  const artifactBtn = path
    ? `<button type="button" class="plan-artifact-btn" data-artifact-path="${escapeHtml(path)}">查看产物</button>`
    : '';
  const codeBlock = code
    ? `<details class="tool-code"><summary>查看代码</summary><pre><code>${escapeHtml(code)}</code></pre></details>`
    : '';
  const thoughtHtml = thought
    ? `<div class="tool-card-thought">${escapeHtml(thought)}</div>`
    : '';
  card.innerHTML = `
    <div class="tool-card-header">
      <span class="tool-card-title">🔧 工具调用 · ${escapeHtml(title)}</span>
      <span class="tool-status ${cls}">${TOOL_STATUS_LABELS[cls]}</span>
    </div>
    ${thoughtHtml}
    <div class="tool-card-detail">${escapeHtml(detail)}</div>
    ${artifactBtn}
    ${codeBlock}
  `;
  const btn = card.querySelector('[data-artifact-path]');
  if (btn) btn.addEventListener('click', () => openArtifact(btn.getAttribute('data-artifact-path'), btn));
  // 工具卡属于「过程」：收进过程块（默认收起），结论/决策/产物入口留在正文。
  mountProcessNode(card);
}

/** 从工具卡标题里回推步骤号（历史记录没存 ``stepId``，标题形如「写入文件 · loop-003」）。
 *
 * 必须与执行结果的 ``step_id`` 用**同一个键**，否则「本地记录」与「后端结果」两份来源
 * 会各渲染一套卡片（回看时工具卡翻倍）。
 */
function stepIdFromTitle(title) {
  const parts = String(title || '').split(' · ');
  return parts.length > 1 ? parts[parts.length - 1].trim() : '';
}

/** 去掉同一步骤的旧节点（工具卡 / 产物行都带 ``data-step-id``）。
 *
 * 「到点暂停」放行后 ``/resume`` 会回传**全部**执行结果，不清理就会把已成功的步骤再渲染
 * 一遍：卡片翻倍、过程块摘要的步数虚增。新节点随后按同一步骤号重新渲染（等于就地更新）。
 *
 * ``selector`` 必须限定节点类型：工具卡与产物行共用同一个步骤号，按步骤号删会互相误伤；
 * 计划区块的行也带 ``data-step-id``，更要靠选择器把它排除在外。
 */
function dropPreviousStepNodes(stepId, selector) {
  if (!stepId) return;
  const root = document.getElementById('chatLog');
  if (!root) return;
  root.querySelectorAll(`${selector}[data-step-id="${String(stepId)}"]`).forEach((el) => el.remove());
}

/** 追加工具调用卡片（渲染 + 记录）。 */
function appendToolCard(title, status, detail, extra) {
  const stepId = extra && extra.stepId ? String(extra.stepId) : '';
  dropPreviousStepNodes(stepId, '.tool-card');
  renderToolCard(title, status, detail, extra);
  recordEntry({
    kind: 'tool',
    stepId,
    title,
    status,
    detail,
    path: extra && extra.path ? extra.path : null,
    thought: extra && extra.thought ? extra.thought : null,
  });
  // 工具卡折进过程块，但「产出了什么、去哪看」不该被折叠藏住 → 正文再留一条产物入口。
  if (extra && extra.path) appendArtifactRow(extra.path, title, stepId);
}

/** 渲染产物入口行（只渲染，不写会话记录）—— 留在正文，不随过程块折叠。 */
function renderArtifactRow(path, title, stepId) {
  const row = document.createElement('div');
  row.className = 'artifact-row chat-item';
  if (stepId) row.dataset.stepId = String(stepId); // 与工具卡共享去重键
  const label = document.createElement('span');
  label.className = 'artifact-label';
  label.textContent = `📄 产物：${title || path}`;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'plan-artifact-btn';
  btn.dataset.artifactPath = path;
  btn.textContent = '查看产物';
  btn.addEventListener('click', () => openArtifact(path, btn));
  row.appendChild(label);
  row.appendChild(btn);
  mountChatNode(row);
}

/** 追加产物入口行（渲染 + 记录）。 */
function appendArtifactRow(path, title, stepId) {
  dropPreviousStepNodes(stepId, '.artifact-row');
  renderArtifactRow(path, title, stepId);
  recordEntry({ kind: 'artifact', stepId: stepId || '', path, title });
}

/* ──────────────── 结论区 ────────────────
 * 跑完给一句人话结论（后端 `task.conclusion`）：agentic 取模型 kind=final 的 `answer`
 * （专门写给用户看的结论，取不到才回退决策理由），固定工作流按执行结果如实汇总。
 * 它**留在正文**（不折进过程块）——
 * 「做了什么、成没成」是默认视图必须一眼可见的东西。
 */

/** 渲染/刷新结论块（只渲染，不写会话记录）。
 *
 * 不写 transcript 是刻意的：结论由后端状态派生（``GET /tasks/{id}`` 也带），
 * 刷新后调 ``applyTaskToView`` 即可重建，不需要（也不该）在会话里记第二份。
 */
function renderConclusion(task) {
  const text = task && typeof task.conclusion === 'string' ? task.conclusion.trim() : '';
  if (!text) {
    if (conclusionBlockEl) {
      conclusionBlockEl.remove();
      conclusionBlockEl = null;
    }
    return;
  }
  if (!conclusionBlockEl || !conclusionBlockEl.isConnected) {
    conclusionBlockEl = document.createElement('div');
    conclusionBlockEl.className = 'conclusion-block chat-item';
    const label = document.createElement('div');
    label.className = 'conclusion-label';
    label.textContent = '✅ 结论';
    const body = document.createElement('div');
    body.className = 'conclusion-body';
    conclusionBlockEl.appendChild(label);
    conclusionBlockEl.appendChild(body);
    mountChatNode(conclusionBlockEl);
  }
  conclusionBlockEl.querySelector('.conclusion-body').textContent = text;
}

/** 渲染评审会纪要卡片（只渲染，不写会话记录）。
 *
 * 纪要由后端 council_build_minutes 规则式收敛产出：一致 / 分歧 / 未决 / 建议决议。
 */
function renderCouncilCard(minutes) {
  const card = document.createElement('div');
  card.className = 'council-card chat-item';
  const section = (title, items) => {
    if (!items || items.length === 0) return '';
    const rows = items
      .map(
        (item) =>
          `<div class="council-item"><span class="council-role">${escapeHtml(item.role)}</span>${escapeHtml(item.content)}</div>`
      )
      .join('');
    return `<div class="council-section"><div class="council-section-title">${title}</div>${rows}</div>`;
  };
  const participants = (minutes.participants || []).map((r) => escapeHtml(r)).join('、');
  const absent = (minutes.absent || []).map((r) => escapeHtml(r)).join('、');
  card.innerHTML = `
    <div class="council-title">🧑‍🤝‍🧑 评审会纪要</div>
    <div class="council-topic">议题：${escapeHtml(minutes.topic || '')}</div>
    <div class="council-meta">
      参会：${participants || '—'}${absent ? ` ｜ 缺席：${absent}` : ''} ｜ 轮数：${minutes.rounds || 1}
    </div>
    ${section('一致', minutes.agreements)}
    ${section('分歧', minutes.disagreements)}
    ${section('未决', minutes.unresolved)}
    <div class="council-decision">建议决议：${escapeHtml(minutes.decision_note || minutes.suggested_decision || '—')}</div>
  `;
  mountChatNode(card);
}

/** 追加评审会纪要（渲染 + 记录），与 transcript 裁剪集合保持一致（审计 P3）。 */
function appendCouncilCard(minutes) {
  renderCouncilCard(minutes);
  recordEntry({ kind: 'council', minutes });
}

/** 手动召开评审会（任务菜单入口）：后端多角色独立表态后收敛为纪要。 */
async function handleCouncil() {
  if (!currentTaskId) {
    appendMessage('agent', '当前无任务，无法召开评审会。');
    return;
  }
  const taskId = currentTaskId;
  appendMessage('agent', '评审会已发起，多个角色正在独立表态…');
  try {
    const minutes = await api.runCouncil(taskId, { rounds: 1 });
    const count = (minutes.participants || []).length;
    appendMessage('agent', `评审会完成：${count} 个角色参会，已生成纪要。`);
    appendCouncilCard(minutes);
  } catch (err) {
    appendMessage('agent', `❌ 评审会失败：${err.message}`);
  }
}

/* ──────────────── 计划区块 ────────────────
 * 把 /plan 的 steps（按 order 排序）渲染成结构化列表：
 *   步骤 id · action · 关键输入摘要 · 依赖 · 高风险标记 · 执行状态 · 查看产物 / 查看代码。
 */

/** 渲染/刷新计划区块（**收进「过程」块**，与工具卡 / 自检 / 思考同一个折叠容器）。
 *
 * ⚠️ 只在 `workflow` 模式渲染。agentic 下执行的步骤由模型逐轮决定，预先给出的
 * DAG 并不是真正跑的东西——把它当主界面展示，用户看到的永远是「一份固定工作流」。
 *
 * 计划**不再有独立槽位**：它属于「过程」，默认随过程块收起；需要用户拍板时
 * （``awaiting_confirm`` / 待放行等）过程块会自动展开，确认入口不会被藏住。
 */
function renderPlanBlock(plan) {
  const body =
    currentProcessBlock && currentProcessBlock.isConnected
      ? currentProcessBlock.querySelector('.process-body')
      : null;
  const existing = body ? body.querySelector('.plan-block') : null;
  const hasSteps = plan && Object.keys(plan.steps || {}).length;
  const hasOrder = plan && (plan.order || []).length;
  if (EXECUTION_MODE !== 'workflow' || (!hasSteps && !hasOrder)) {
    // 折进去的东西要能撤走：否则「改计划 / 换任务」会留下上一份计划。
    if (existing) {
      existing.remove();
      refreshProcessBlock();
    }
    return;
  }
  const steps = plan.steps || {};
  const order = hasOrder ? plan.order : Object.keys(steps);
  const highRisk = new Set(plan.high_risk_actions || []);
  const results = {};
  for (const item of plan.execution_results || []) results[item.step_id] = item;

  const rows = order.map((stepId) => {
    const step = steps[stepId] || { id: stepId, action: '?' };
    const res = results[stepId];
    const status = (res && res.status) || step.status || 'pending';
    const cls = EXEC_STATUS_CLASS[status] || 'pending';
    const risky = highRisk.has(step.action);
    const summary = summarizeInputs(step);
    const deps = (step.depends_on || []).join('、') || '—';
    // 只有**真正产出文件**的步骤才给「查看产物」入口：此前拿 inputs.path 冒充，
    // 于是「列出文件（文件：.）」也带按钮，点开就是「路径不是文件: .」。
    const artifactPath = stepArtifactPath(res) || '';
    const code = extractCodeBlock(res && res.result);
    const artifactBtn = artifactPath
      ? `<button type="button" class="plan-artifact-btn" data-artifact-path="${escapeHtml(artifactPath)}">查看产物</button>`
      : '';
    const codeBlock = code
      ? `<details class="plan-code"><summary>查看代码</summary><pre><code>${escapeHtml(code)}</code></pre></details>`
      : '';
    const output = (artifactBtn || codeBlock)
      ? `<div class="plan-step-output">${artifactBtn}${codeBlock}</div>`
      : '';
    // 动作名一律用人读标题（后端下发）；英文 action 只作最后兜底，不主动展示。
    const actionLabel = (step.title || '').trim() || step.action;
    return `
      <li class="plan-step${risky ? ' is-risk' : ''}" data-step-id="${escapeHtml(stepId)}">
        <div class="plan-step-head">
          <span class="plan-step-id">${escapeHtml(stepId)}</span>
          <span class="plan-step-action">${escapeHtml(actionLabel)}${risky ? '<span class="plan-step-flag">高风险</span>' : ''}</span>
          <span class="tool-status ${cls}">${TOOL_STATUS_LABELS[cls]}</span>
        </div>
        <dl class="plan-step-meta">
          <dt>输入</dt><dd>${escapeHtml(summary)}</dd>
          <dt>依赖</dt><dd>${escapeHtml(deps)}</dd>
        </dl>
        ${output}
      </li>`;
  }).join('');

  const riskList = [...highRisk];
  // 高风险提示也只给人话：从本块正在渲染的 steps 里按 action 反查 title。
  const titleOfAction = (action) => {
    const hit = order
      .map((stepId) => steps[stepId])
      .find((item) => item && item.action === action);
    const title = hit && typeof hit.title === 'string' ? hit.title.trim() : '';
    return title || action;
  };
  const riskNote = riskList.length
    ? `<span class="plan-risk-note">含高风险动作：${riskList.map((a) => escapeHtml(titleOfAction(a))).join('、')}</span>`
    : '';
  const actions = currentStatus === 'awaiting_confirm'
    ? '<div class="plan-actions"><button type="button" class="btn btn-reject" data-plan-act="reject">改计划</button></div>'
    : '';

  const block = document.createElement('details');
  block.className = 'plan-block';
  block.open = true; // 计划自身可折叠；外层「过程」块才是默认收起的那一层
  block.dataset.taskId = currentTaskId || '';
  block.innerHTML = `
    <summary class="plan-summary">
      <span class="plan-title">🗺 执行计划</span>
      <span class="plan-count">${order.length} 个步骤</span>
      ${riskNote}
    </summary>
    <ol class="plan-steps">${rows}</ol>
    ${actions}
  `;
  block.querySelectorAll('[data-artifact-path]').forEach((el) => {
    el.addEventListener('click', () => openArtifact(el.getAttribute('data-artifact-path'), el));
  });
  const rejectBtn = block.querySelector('[data-plan-act="reject"]');
  if (rejectBtn) rejectBtn.onclick = () => handleReject(block);
  // 就地替换（保持它在过程里的位置），否则每次刷新都会多出一份计划。
  if (existing) existing.replaceWith(block);
  else mountProcessNode(block);
  refreshProcessBlock();
}

/* ──────────────── 提醒卡 / 中断卡（按后端状态重建） ──────────────── */

/** 到点暂停：渲染提醒卡（列出待放行的工具与步骤 + 放行/放弃）。 */
function buildReminderCard(pending, taskId) {
  const card = document.createElement('div');
  card.className = 'reminder-card';
  card.dataset.taskId = taskId || currentTaskId || '';
  card.dataset.approvedTools = JSON.stringify((pending && pending.tools) || []);
  const tools = (pending && pending.tools) || [];
  const steps = (pending && pending.steps) || [];
  // 待放行工具 / 步骤都用人读中文名：这里是用户要"拍板"的地方，不该出现英文 action。
  const toolList = tools.map((t) => `<code>${escapeHtml(actionTitle(t))}</code>`).join('、');
  const stepRows = steps
    .map((s) => `<li><span class="reminder-step-id">${escapeHtml(s.step_id)}</span>${escapeHtml(stepTitle(s.step_id, s.action))}</li>`)
    .join('');
  card.innerHTML = `
    <div class="reminder-title">⚠ 到点暂停：有高风险步骤待你放行</div>
    <div class="reminder-detail">计划里有需要你拍板的写 / 删除 / 提交 / 回滚动作，已暂停执行（未写任何文件）。放行后继续，或改计划 / 放弃任务。</div>
    <div class="reminder-section"><span class="reminder-label">待放行工具</span>${toolList || '—'}</div>
    <div class="reminder-section"><span class="reminder-label">待放行步骤</span><ul class="reminder-steps">${stepRows || '<li>—</li>'}</ul></div>
    <div class="approval-actions">
      <button class="btn btn-run" type="button">✔ 放行并继续</button>
      <button class="btn btn-reject" type="button">✎ 改计划</button>
      <button class="btn btn-danger" type="button">✗ 放弃</button>
    </div>
  `;
  card.querySelector('.btn-run').onclick = () => handleApprovePending(card);
  card.querySelector('.btn-reject').onclick = () => handleReject(card);
  card.querySelector('.btn-danger').onclick = () => handleAbort(card);
  return card;
}

/** 手动中断：渲染恢复/放弃操作卡。 */
function buildInterruptCard(taskId) {
  const card = document.createElement('div');
  card.className = 'approval-card';
  card.dataset.taskId = taskId || currentTaskId || '';
  card.innerHTML = `
    <div class="approval-title">⏸ 任务已暂停</div>
    <div class="approval-meta">手动中断不会自动重放计划；恢复只翻转状态，改计划则退回重新规划，放弃则任务终止。</div>
    <div class="approval-actions">
      <button class="btn btn-resume" type="button">▶ 恢复</button>
      <button class="btn btn-reject" type="button">✎ 改计划</button>
      <button class="btn btn-danger" type="button">✗ 放弃</button>
    </div>
  `;
  card.querySelector('.btn-resume').onclick = () => handleResume(card);
  card.querySelector('.btn-reject').onclick = () => handleReject(card);
  card.querySelector('.btn-danger').onclick = () => handleAbort(card);
  return card;
}

/** 取「非收敛停下」的原因（人话）；没有则空串。
 *
 * 只有 agentic 循环会用它：``stopped_reason`` 是 ``final``（正常收尾）/ ``pending_approval``
 * （到点暂停）/ ``proposed``（等扩编决定）时都不算异常停下，那三种另有专门的卡片。
 */
function loopStopReason(task) {
  const loop = task && task.loop_state;
  const reason = loop && loop.stopped_reason ? String(loop.stopped_reason) : '';
  return LOOP_STOP_LABELS[reason] || '';
}

/** 渲染「执行已停下」卡：把后端停下原因翻成人话，并给出唯一可用的操作（放弃）。
 *
 * 这些停下原因（预算耗尽 / 空转 / 决策不可解析）下，任务是**没做完**的；此前前端什么
 * 都不显示 → 用户只看到顶栏「执行中」，既不知道原因也没有出口。
 */
function buildStopCard(task) {
  const reason = loopStopReason(task);
  const results = (task && task.execution_results) || [];
  const failed = results.filter((r) => r.status === 'failed').length;
  const card = document.createElement('div');
  card.className = 'stop-card';
  card.dataset.taskId = (task && task.task_id) || currentTaskId || '';
  card.innerHTML = `
    <div class="stop-title">⚠ 执行已停下</div>
    <div class="stop-detail">${escapeHtml(reason)}${failed ? `，其中 ${failed} 个步骤失败（细节见上方「过程」）` : ''}。需求尚未完成：可以把需求说得更具体后重新发送，或放弃这个任务。</div>
    <div class="approval-actions">
      <button class="btn btn-danger" type="button">✗ 放弃任务</button>
    </div>
  `;
  card.querySelector('.btn-danger').onclick = () => handleAbort(card);
  return card;
}

/** 渲染「待确认交付」卡：收敛后停在 ``verifying`` 的**唯一出口**。
 *
 * 此前既不会自动交付、UI 也没有任何按钮 → 任务永远停在顶栏「验证中」，用户既
 * 不知道算不算做完、也没法收尾（实机踩过）。
 */
function buildDeliverCard(task) {
  const card = document.createElement('div');
  card.className = 'stop-card';
  card.dataset.taskId = (task && task.task_id) || currentTaskId || '';
  card.innerHTML = `
    <div class="stop-title">✓ 执行已收尾，等待你确认交付</div>
    <div class="stop-detail">循环已收尾（结论见上方）。确认后任务进入「已交付」；如果结果不满意，也可以把需求说得更具体后重新发送。</div>
    <div class="approval-actions">
      <button class="btn btn-resume" type="button">✓ 确认交付</button>
    </div>
  `;
  card.querySelector('.btn-resume').onclick = () => handleDeliver(card);
  return card;
}

/** 按任务当前状态重建提醒卡 / 中断卡 / 提议卡（绑定任务 ID，避免跨任务误操作）。 */
function renderActionSlot(task) {
  const slot = document.getElementById('actionSlot');
  if (!slot) return;
  slot.innerHTML = '';
  currentPendingApproval = task && task.pending_approval ? task.pending_approval : null;
  // 只有「任务仍挂在 executing、但循环已经不跑了」才算异常停下。已失败 / 已交付 / 验证中
  // 都有各自的终态文案，不能被这个覆盖（否则用户主动「放弃」后顶栏会显示成「已停下」）。
  currentStopReason = task && task.status === 'executing' ? loopStopReason(task) : '';
  if (currentPendingApproval) {
    slot.appendChild(buildReminderCard(currentPendingApproval, task.task_id));
  } else if (task && task.pending_proposal) {
    // 提议卡优先于中断卡：此刻真正等的是一个"要不要扩编"的决定。
    slot.appendChild(buildProposalCard(task.pending_proposal, task.task_id));
  } else if (task && task.status === 'interrupted') {
    slot.appendChild(buildInterruptCard(task.task_id));
  } else if (currentStopReason) {
    // 非收敛停下：模型没做完就停了，状态仍停在 executing —— 要说清原因并给出出口。
    slot.appendChild(buildStopCard(task));
  } else if (task && task.status === 'verifying') {
    // 收敛后停在验证中 —— 给出交付出口，别让任务无限期悬着。
    slot.appendChild(buildDeliverCard(task));
  }
  // 过程块的摘要与"必须展开"判定都依赖任务状态（待放行 / 需决定 / 失败）。
  refreshProcessBlock(task);
  updateStatusTask();
  // 顶栏「等待放行」是由 currentPendingApproval 派生的：放行完成后必须重算一次标签，
  // 否则 chip 会一直停在「等待放行」，与真实状态（如「验证中」）不符。
  if (currentStatus) updateTaskStatus(currentStatus);
}

/** 副结构提议：渲染「等扩编决定」卡（草案 + 待写入文件 + 模块预览 + 批准/拒绝）。 */
function buildProposalCard(view, taskId) {
  const proposal = (view && view.proposal) || {};
  const card = document.createElement('div');
  card.className = 'proposal-card';
  card.dataset.taskId = taskId || currentTaskId || '';
  card.dataset.proposalId = proposal.proposal_id || '';
  const accepts = (view && view.accepts) || [];
  const files = proposal.impacted_files || [];
  const acceptsHtml = accepts.map((a) => `<code>${escapeHtml(a)}</code>`).join('、');
  const filesHtml = files.map((f) => `<li><code>${escapeHtml(f)}</code></li>`).join('');
  card.innerHTML = `
    <div class="proposal-title">🧬 副结构提议：新增角色 <code>${escapeHtml(proposal.target_module || '—')}</code></div>
    <div class="proposal-detail">${escapeHtml(proposal.change_desc || '')}</div>
    <div class="proposal-section"><span class="proposal-label">为什么现有角色做不到</span>${escapeHtml(view.rationale || '—')}</div>
    <div class="proposal-section"><span class="proposal-label">预期承接动作</span>${acceptsHtml || '—'}</div>
    <div class="proposal-section"><span class="proposal-label">自评风险</span><code>${escapeHtml(proposal.risk || '—')}</code></div>
    <div class="proposal-section"><span class="proposal-label">批准后写入（工作区内）</span><ul class="proposal-files">${filesHtml || '<li>—</li>'}</ul></div>
    <details class="tool-code"><summary>查看角色模块预览</summary><pre>${escapeHtml(proposal.diff_preview || '')}</pre></details>
    <div class="proposal-note">批准只把脚手架写进工作区 <code>proposals/&lt;id&gt;/</code>，<b>不改仓库既有文件</b>；新角色要人工并入 <code>tools/permissions.py</code> 授权后才会被派发。</div>
    <div class="approval-actions">
      <button class="btn btn-run" type="button">✔ 批准并生成脚手架</button>
      <button class="btn btn-reject" type="button">✗ 拒绝</button>
    </div>
  `;
  card.querySelector('.btn-run').onclick = () => handleProposalDecision(card, true);
  card.querySelector('.btn-reject').onclick = () => handleProposalDecision(card, false);
  return card;
}

/** 提交提议决定：批准 → 后端写脚手架；拒绝 → 只记录决定。失败则恢复按钮可点。 */
async function handleProposalDecision(card, approve) {
  const taskId = card.dataset.taskId || currentTaskId;
  if (!taskId) return;
  const buttons = Array.from(card.querySelectorAll('button'));
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const task = approve
      ? await api.approveProposal(taskId)
      : await api.rejectProposal(taskId);
    if (approve) {
      const latest = (task.proposals || [])[task.proposals.length - 1] || {};
      const count = (latest.impacted_files || []).length;
      appendMessage('agent', `已批准扩编提议，脚手架写入工作区（${count} 个文件）。按生成物里的 README 并入授权与协议文档后，新角色才会被派发。`);
    } else {
      appendMessage('agent', '已拒绝扩编提议（未写任何文件）。');
    }
    applyRunResult(task);
  } catch (err) {
    buttons.forEach((b) => { b.disabled = false; });
    appendMessage('agent', `提议决定失败：${err && err.message ? err.message : err}`);
  }
}

/* ──────────────── 文件树渲染 ──────────────── */

// 折叠中的目录路径：按工作区相对路径记住，重建树（含 ↻ 刷新）时不再被重置。
const collapsedDirs = new Set();

/** 递归渲染文件树：目录可折叠，文件可点击打开。 */
function renderFileTree(nodes, container, depth = 0) {
  nodes.forEach((node) => {
    // 交互一律原生 <button>：可聚焦、Enter/Space 触发、读屏语义都由原生元素提供。
    const row = document.createElement('button');
    row.type = 'button';
    row.className = `tree-node depth-${depth}${node.type === 'dir' ? ' tree-dir' : ' tree-file'}`;
    row.title = node.path;

    if (node.type === 'dir') {
      const isCollapsed = collapsedDirs.has(node.path);
      row.innerHTML = `<span class="tree-caret">${isCollapsed ? '▸' : '▾'}</span> 📁 ${escapeHtml(node.name)}`;
      container.appendChild(row);

      // 子节点放进独立容器，便于整块折叠/展开。
      const childBox = document.createElement('div');
      childBox.className = `tree-children${isCollapsed ? ' collapsed' : ''}`;
      container.appendChild(childBox);
      renderFileTree(node.children || [], childBox, depth + 1);

      row.addEventListener('click', () => {
        const collapsed = childBox.classList.toggle('collapsed');
        row.querySelector('.tree-caret').textContent = collapsed ? '▸' : '▾';
        if (collapsed) collapsedDirs.add(node.path);
        else collapsedDirs.delete(node.path);
      });
    } else {
      const sizeLabel = node.size != null
        ? ` <span class="tree-size">${formatSize(node.size)}</span>`
        : '';
      row.innerHTML = `📄 ${escapeHtml(node.name)}${sizeLabel}`;
      row.addEventListener('click', () => {
        setNavOpen(false); // 窄屏：打开文件后收起抽屉，让对话框完整可见
        openWorkspaceFile(node, row);
      });
      container.appendChild(row);
    }
  });
}

/* ──────────────── 文件查看/编辑 ──────────────── */

const fileViewer = document.getElementById('fileViewer');
const fileViewerPath = document.getElementById('fileViewerPath');
const fileViewerMeta = document.getElementById('fileViewerMeta');
const fileViewerDirty = document.getElementById('fileViewerDirty');
const fileViewerSave = document.getElementById('fileViewerSave');
const fileViewerBody = document.getElementById('fileViewerBody');
const fileViewerPanel = fileViewer.querySelector('.file-viewer-panel');

// 当前打开的文件：viewerWorkspacePath 为其在工作区内的相对路径。
let viewerWorkspacePath = null;
let viewerDirty = false;
// 打开查看器前的来源焦点，关闭后归还（模态焦点接管/归还）。
let viewerReturnFocus = null;

/** 切换背景区域的 inert：模态打开时禁止背景获取焦点或响应点击（让 aria-modal 名副其实）。 */
function setBackgroundInert(on) {
  ['titlebar', 'menubar', 'toolbar', 'main', 'bottom-bar', 'statusbar'].forEach((cls) => {
    document.querySelector(`.${cls}`)?.toggleAttribute('inert', on);
  });
}

/** 打开查看器外壳：记录来源焦点、显示、接管背景。内容由调用方填充后再聚焦。
 *
 * Args:
 *    trigger: 触发打开的元素（文件树行 / 计划步骤「查看产物」按钮）。
 */
function showViewer(trigger) {
  viewerReturnFocus = trigger || document.activeElement;
  fileViewer.hidden = false;
  setBackgroundInert(true);
  resetViewer();
}

/** 标记「有未保存修改」。 */
function setViewerDirty(dirty) {
  viewerDirty = dirty;
  fileViewerDirty.hidden = !dirty;
}

/** 复位查看器状态并清空内容（默认只读）。 */
function resetViewer() {
  viewerWorkspacePath = null;
  fileViewerDirty.hidden = true;
  viewerDirty = false;
  fileViewerSave.hidden = true;
  fileViewerSave.disabled = false;
  fileViewerBody.value = '';
  fileViewerBody.readOnly = true;
}

/** 关闭查看器；有未保存修改时先二次确认。关闭后释放背景并归还焦点。 */
function closeFileViewer(force) {
  if (viewerDirty && !force && !window.confirm('有未保存的修改，确定关闭吗？')) return;
  fileViewer.hidden = true;
  setBackgroundInert(false);
  resetViewer();
  if (viewerReturnFocus && typeof viewerReturnFocus.focus === 'function') {
    viewerReturnFocus.focus();
  }
  viewerReturnFocus = null;
}

/** 打开工作区文件：可编辑则显示「保存」；超出可编辑上限则只读截断预览。 */
async function openWorkspaceFile(node, trigger) {
  showViewer(trigger);
  viewerWorkspacePath = node.path;
  fileViewerPath.textContent = node.path;
  fileViewerMeta.textContent = node.size != null ? formatSize(node.size) : '';
  fileViewerBody.value = '加载中…';
  fileViewerPanel.focus(); // 模态接管焦点（读屏据此播报对话框名称）
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
    // 目录 / 不存在的路径都会走到这里：给一句能指向下一步的提示，而不是只倒原文。
    fileViewerBody.value = `打开失败：${err.message}\n\n如果这是一个文件夹，请在右侧「产物」面板里展开查看。`;
    fileViewerBody.readOnly = true;
  }
}

/** 从计划步骤 / 工具卡直接打开对应产物文件（右栏产物面板）。 */
function openArtifact(path, trigger) {
  if (!path) return;
  setNavOpen(false);
  openWorkspaceFile({ path, size: null }, trigger);
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

fileViewerBody.addEventListener('input', () => {
  if (!fileViewerBody.readOnly) setViewerDirty(true);
});
fileViewerSave.addEventListener('click', handleViewerSave);
document.getElementById('fileViewerClose').addEventListener('click', () => closeFileViewer());
fileViewer.addEventListener('click', (e) => {
  if (e.target.dataset && e.target.dataset.close) closeFileViewer();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !fileViewer.hidden) closeFileViewer();
});

/** 格式化文件大小 */
function formatSize(bytes) {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)}GB`;
}

/** 加载并渲染工作区产物文件树 */
async function loadFileTree() {
  const tree = document.getElementById('fileTree');
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
    // 颜色只走 CSS token（审计 P2）：原先内联 #cf222e / #57606a 在暗色底对比度不足
    tree.innerHTML = `<div class="tree-node depth-0 tree-error">❌ ${escapeHtml(err.message)}</div>`
      + '<div class="tree-node depth-0 tree-error-hint">启动后端：python -m uvicorn agent_builder.api.app:create_app --factory --reload --port 8000</div>';
  }
}

/* ──────────────── 任务列表（多任务并存、可回看） ──────────────── */

/** 渲染任务列表：标题 / 状态 / 是否待放行；当前任务高亮。 */
/* ──────────────── 通用确认对话框（破坏性操作）────────────────
 * 复用 .project-dialog 组件与语义 token，保证与「新建文件夹」等弹窗视觉一致。
 * 交互约定：默认聚焦「取消」（破坏性操作的默认动作不应是删除）；
 * 「取消」/ Esc / 点遮罩 都解析为 false，关闭时把焦点归还触发元素。
 */

const confirmDialogEl = document.getElementById('confirmDialog');
const confirmTitleEl = document.getElementById('confirmTitle');
const confirmMessageEl = document.getElementById('confirmMessage');
const confirmOkEl = document.getElementById('confirmOk');
const confirmCancelEl = document.getElementById('confirmCancel');
let confirmResolve = null;
let confirmTrigger = null;

/** 打开确认对话框，返回 Promise<boolean>（确认 = true）。 */
function openConfirmDialog({ title, message, confirmText = '确认' }) {
  if (!confirmDialogEl) return Promise.resolve(false);
  confirmTitleEl.textContent = title;
  confirmMessageEl.textContent = message;
  confirmOkEl.textContent = confirmText;
  confirmTrigger = document.activeElement; // 记录来源焦点，关闭时归还
  confirmDialogEl.hidden = false;
  confirmCancelEl?.focus();
  return new Promise((resolve) => { confirmResolve = resolve; });
}

/** 关闭确认对话框并归还焦点。 */
function settleConfirmDialog(result) {
  if (confirmDialogEl) confirmDialogEl.hidden = true;
  const resolve = confirmResolve;
  confirmResolve = null;
  const trigger = confirmTrigger;
  confirmTrigger = null;
  if (trigger && typeof trigger.focus === 'function') trigger.focus();
  if (resolve) resolve(result);
}

confirmOkEl?.addEventListener('click', () => settleConfirmDialog(true));
confirmCancelEl?.addEventListener('click', () => settleConfirmDialog(false));
confirmDialogEl?.querySelectorAll('[data-close]').forEach((el) => {
  el.addEventListener('click', () => settleConfirmDialog(false));
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && confirmDialogEl && !confirmDialogEl.hidden) settleConfirmDialog(false);
});

/** 最近一次拉取到的任务摘要（删除确认时用于取可读标题）。 */
let latestTaskSummaries = [];

/** 渲染任务列表（GET /task-summaries 的结果）。 */
function renderTaskList(summaries) {
  const list = document.getElementById('taskList');
  if (!list) return;
  latestTaskSummaries = summaries || [];
  list.innerHTML = '';
  if (!summaries || summaries.length === 0) {
    list.innerHTML = '<div class="task-empty">暂无任务</div>';
    return;
  }
  summaries.forEach((summary) => {
    // 行容器：主按钮负责切换，删除按钮独立靠右。
    // HTML 不允许 button 嵌套 button，故用 .task-row 承载两个平级 <button>。
    const row = document.createElement('div');
    row.className = 'task-row';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `task-item${summary.task_id === currentTaskId ? ' is-current' : ''}`;
    btn.dataset.taskId = summary.task_id;
    btn.title = summary.requirement || summary.title || '';
    const title = document.createElement('span');
    title.className = 'task-item-title';
    title.textContent = summary.title || String(summary.task_id).slice(0, 8);
    const meta = document.createElement('span');
    meta.className = 'task-item-meta';
    // 非收敛停下：列表项也要显示「已停下」—— 否则左栏写「执行中」、详情页写「已停下」，两处打架。
    // 判定与详情页同口径（都拿 loop_state.stopped_reason 经 LOOP_STOP_LABELS 翻译）。
    const stopped = summary.status === 'executing'
      && Boolean(LOOP_STOP_LABELS[summary.stopped_reason || '']);
    const status = document.createElement('span');
    status.className = `task-status ${stopped ? 'interrupted' : summary.status}`;
    status.textContent = stopped ? '已停下' : statusLabel(summary.status);
    meta.appendChild(status);
    // 是否待放行（后端 has_pending_approval）：直接标在列表项上。
    if (summary.has_pending_approval) {
      const flag = document.createElement('span');
      flag.className = 'task-flag';
      flag.textContent = '待放行';
      meta.appendChild(flag);
    }
    btn.append(title, meta);
    btn.addEventListener('click', () => switchToTask(summary.task_id));
    // 删除按钮：危险操作语义；点击不冒泡到主按钮（避免顺带触发切换）。
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'task-del';
    del.dataset.deleteTask = summary.task_id;
    del.textContent = '✕';
    const delLabel = summary.title || String(summary.task_id).slice(0, 8);
    del.setAttribute('aria-label', `删除任务：${delLabel}`);
    del.title = '删除任务';
    del.addEventListener('click', (event) => {
      event.stopPropagation();
      handleDeleteTask(summary.task_id);
    });
    row.append(btn, del);
    list.appendChild(row);
  });
}

/** 删除任务：后端 DELETE /tasks/{id} → 清本地会话快照 → 刷新列表。
 *
 * 若删除的是**当前任务**，复用既有「＋ 新任务」重置路径（startNewTask）：先清空
 * currentTaskId 再调用，避免重复去终止一个已被删除的任务。
 */
async function handleDeleteTask(taskId) {
  if (!taskId) return;
  // 破坏性操作：先确认再删（默认聚焦「取消」；Esc / 点遮罩等同取消）。
  const summary = latestTaskSummaries.find((item) => item.task_id === taskId);
  const label = (summary && summary.title) || String(taskId).slice(0, 8);
  const confirmed = await openConfirmDialog({
    title: '删除任务',
    message: `确认删除任务「${label}」？该任务会从列表消失，且不可恢复。`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  try {
    await api.deleteTask(taskId);
  } catch (err) {
    appendMessage('agent', '❌ 删除任务失败：' + err.message);
    return;
  }
  // 同步删除本地会话快照，避免刷新后被 restoreSession 又恢复出来。
  if (Object.prototype.hasOwnProperty.call(taskSessions, taskId)) {
    delete taskSessions[taskId];
  }
  if (taskId === currentTaskId) {
    currentTaskId = null;
    currentTaskTitle = '';
    await startNewTask();  // 复用既有重置路径（清对话区/计划区块/顶栏状态与标题复位）
    return;
  }
  saveSession();
  await loadTaskSummaries();
}

/** 拉取任务摘要列表（GET /task-summaries）。 */
async function loadTaskSummaries() {
  const list = document.getElementById('taskList');
  try {
    const summaries = await api.listTaskSummaries();
    renderTaskList(summaries);
  } catch (err) {
    if (list) list.innerHTML = `<div class="task-empty">任务列表加载失败：${escapeHtml(err.message)}</div>`;
  }
}

/** 终止当前后端任务（切换/新建任务前调用，避免孤儿任务）。 */
async function stopActiveTask() {
  const taskId = currentTaskId;
  if (!taskId) return;
  try {
    await api.abortTask(taskId);
  } catch (err) {
    // 任务可能已终结（delivered/failed 等），终止失败不阻塞切换。
    console.warn('终止旧任务失败，忽略：', err.message);
  }
}

/** 清空中栏对话区 DOM。 */
function clearChatUI() {
  const log = document.getElementById('chatLog');
  if (log) log.innerHTML = '';
  // 过程块 / 结论块随对话流一起被清掉：连同引用一起重置，避免后续扫到已脱离文档的旧块。
  currentProcessBlock = null;
  conclusionBlockEl = null;
  lastProcessTask = null;
}

/** 切换回看某个任务：先终止当前后端任务 → 再清界面 → 恢复该任务会话与状态。 */
async function switchToTask(taskId) {
  if (!taskId || taskId === currentTaskId) return;
  saveSession();               // 先把当前任务会话快照进任务表
  await stopActiveTask();      // 先终止旧任务（防孤儿任务）
  clearChatUI();               // 再清界面
  currentTaskId = taskId;
  currentPendingApproval = null;
  currentStopReason = '';
  const saved = taskSessions[taskId];
  currentTaskTitle = saved ? saved.title || '' : '';
  transcript = saved && saved.entries ? saved.entries.slice(0, MAX_CHAT_ITEMS) : [];
  planState = saved && saved.plan ? saved.plan : null;
  trimmed = false;
  for (const entry of transcript) renderEntry(entry);
  trimChatArea();
  renderPlanBlock(planState);
  updateTitlebar();
  try {
    const task = await api.getTask(taskId);
    applyTaskToView(task);
  } catch (err) {
    appendMessage('agent', `❌ 无法打开任务：${err.message}`);
  }
  await loadTaskSummaries();
}

/** 「＋ 新任务」：先终止当前后端任务 → 再清界面（其它任务历史仍保留，可回看）。 */
async function startNewTask() {
  saveSession();
  await stopActiveTask();   // 先终止后端任务
  clearChatUI();            // 再清界面
  resetActiveView();
  currentTaskId = null;
  currentTaskTitle = '';
  updateTaskStatus('received');
  updateTitlebar();
  setNavOpen(false);
  await loadTaskSummaries();
  const input = document.getElementById('msgInput');
  if (input) input.focus();
}

/* ──────────────── 业务流程 ──────────────── */

/** 发送需求 → 创建任务 → 分解 → 默认直接执行（到点暂停则渲染提醒卡）。 */
async function handleSend() {
  const input = document.getElementById('msgInput');
  const requirement = input.value.trim();
  if (!requirement) return;

  // 防连点：上一次请求仍在途时忽略再次发送，避免重复创建任务。
  if (isSending) return;
  isSending = true;
  const sendBtn = document.getElementById('sendBtn');
  if (sendBtn) sendBtn.disabled = true;

  try {
    // 1. 读取底部栏规划选项（模型 / 推理强度 / 高级 / 副结构自检 / 评审会 / LLM 开关）。
    const useLLM = document.getElementById('llmToggle')?.checked || false;
    const planOptions = readPlanOptions();

    // 2. 先聊天：判断这句话是闲聊（直接回话）还是执行诉求（才建任务）。
    //    此前无条件建任务 → 「你好」也会弹一份执行计划，这是本步要消除的体验。
    const { model, temperature } = planOptions;
    const verdict = await api.chat(requirement, recentChatHistory(), { model, temperature });
    if (verdict.kind !== 'task') {
      appendMessage('user', requirement);
      input.value = '';
      autoGrowInput();
      appendMessage('agent', verdict.reply);
      return;
    }

    // 3. 明确要干活 —— 建任务，进入原有规划 / 执行链路。
    const task = await api.createTask(requirement);
    currentTaskId = task.task_id;
    currentTaskTitle = task.title || '';
    // 新任务：重置当前视图（其它任务历史仍保留）。
    transcript = [];
    trimmed = false;
    planState = null;
    clearChatUI();
    document.getElementById('actionSlot').innerHTML = '';
    renderPlanBlock(null);
    updateTitlebar();
    updateTaskStatus(task.status);

    // 4. 显示用户消息
    appendMessage('user', requirement);
    input.value = '';
    autoGrowInput();

    // 5. 触发分解（附带底部栏规划选项）并应用结果；0 步时渲染「暂无可用计划」卡片。
    const result = await api.planTask(task.task_id, useLLM, planOptions);
    await applyPlanResult(task.task_id, result);
  } catch (err) {
    // 失败时把需求还给用户；但若用户在等待期间已重新输入内容，则不覆盖其新输入（审计 P2）
    if (!input.value.trim()) {
      input.value = requirement;
      autoGrowInput();
    }
    input.focus();
    appendMessage('agent', `❌ 错误：${err.message}`);
  } finally {
    isSending = false;
    if (sendBtn) sendBtn.disabled = false;
    await loadTaskSummaries();
    // 规划/执行完成后触发交接判定（轮次 / token / 字符门控由前端累计上报）。
    checkHandover();
  }
}

/** 取最近若干轮纯文本消息，作为 /chat 的上下文（只含对话消息，不含卡片与过程块）。 */
function recentChatHistory(limit = 8) {
  return transcript
    .filter((entry) => entry && entry.kind === 'msg' && typeof entry.text === 'string')
    .slice(-limit)
    .map((entry) => ({
      role: entry.role === 'user' ? 'user' : 'assistant',
      content: entry.text,
    }));
}

/** 应用一次 /plan 结果（首次发送与卡片「重新规划」共用，避免逻辑分叉）。
 *
 * 0 步且后端给了未决问题时渲染「暂无可用计划」卡片：原样展示后端 pending_questions
 * 并给出可执行入口；不替后端猜测归因，也不再引导用户点击此时必然禁用的「⏸ 中断」。
 *
 * ⚠️ agentic 下 **0 步是正常的**（后端不再预先分解 DAG，步骤由模型逐轮长出来），
 * 只有「后端明确说了为什么没有步骤」时才算真的走不下去。
 */
async function applyPlanResult(taskId, result) {
  // 仅当卡片/结果属于当前任务时才更新顶栏状态标签。
  syncTaskStatus(taskId, result.status);

  // 评审会（若已开启）：先展示多角色收敛出的纪要。
  if (result.council) appendCouncilCard(result.council);

  // 执行形态以后端回显为准（与请求同源，避免前端常量与后端默认值漂移）。
  const mode = result.mode || EXECUTION_MODE;
  const steps = result.steps || {};
  const stepCount = Object.keys(steps).length;

  // 计划区块：结构化渲染 steps / order / parallel_groups / high_risk_actions。
  if (taskId === currentTaskId) {
    planState = {
      steps,
      order: result.order || [],
      parallel_groups: result.parallel_groups || [],
      high_risk_actions: result.high_risk_actions || [],
      execution_results: [],
    };
    renderPlanBlock(planState);
  }

  const questions = result.pending_questions || [];
  // ③ 规划阶段去工作流化：agentic 下后端**不再预先分解 DAG**（步骤由模型逐轮长出来），
  // 所以「0 步」是正常结果、不是「暂无可用计划」，不该拦下执行。
  // 例外：后端**明确给了未决问题**（例如无可用密钥 → 回退固定工作流、又没有步骤），
  // 这时原样展示它们 —— 否则用户会以为任务跑起来了，其实什么都没发生。
  if (stepCount === 0 && (mode === 'workflow' || questions.length)) {
    // 后端已给出真实原因（pending_questions）→ 原样展示，不猜测、不改写。
    appendNoPlanCard(taskId, questions, { canReject: result.status === 'awaiting_confirm' });
    return;
  }
  if (mode === 'workflow') {
    if (questions.length) {
      appendMessage('agent', `计划已生成，发现 ${questions.length} 个待确认点。`);
    }
    appendMessage('agent', `计划已生成，共 ${stepCount} 个步骤，默认直接开始执行…`);
  } else {
    // agentic：没有固定 DAG 可展示，只说一句"开始执行"，不引导用户去找计划块。
    appendMessage('agent', '收到，开始执行。');
  }

  // 默认直接跑：自动调用 /run（有未放行高风险步骤时后端会「到点暂停」）。
  // 先乐观置「执行中」：/run 阻塞到跑完才返回，不置位则整段执行期间中断按钮不可点。
  markTaskRunning(taskId);
  const ran = await api.runTask(taskId);
  applyRunResult(ran);
}

/** 归一化 pending_questions 单项为展示文本（后端当前为字符串数组，兼容对象）。 */
function pendingQuestionText(item) {
  if (typeof item === 'string') return item;
  if (item && typeof item === 'object') {
    return String(item.text || item.question || JSON.stringify(item));
  }
  return String(item ?? '');
}

/** 渲染「暂无可用计划」卡片（只渲染，不写会话记录）。
 *
 * 卡片的操作按钮全部走 cardTaskId(card) 卡片作用域，避免跨任务误操作。
 */
function renderNoPlanCard(taskId, questions, extra) {
  const card = document.createElement('div');
  card.className = 'noplan-card chat-item';
  card.dataset.taskId = taskId || '';
  // 「重新规划」必须先退回 planning：/plan 只接受 PLANNING，而本卡片出现时任务
  // 处于 awaiting_confirm，直接重规划必然 409（非法状态转换）。见 handleReplan。
  card.dataset.canReject = extra && extra.canReject ? '1' : '';
  const rows = (questions || [])
    .map((item) => `<li class="noplan-question">${escapeHtml(pendingQuestionText(item))}</li>`)
    .join('');
  const questionBlock = rows
    ? `<div class="noplan-section"><span class="noplan-label">后端说明</span><ul class="noplan-questions">${rows}</ul></div>`
    : '';
  // 「改计划」仅在 awaiting_confirm 时可调用 /reject。
  const rejectBtn = extra && extra.canReject
    ? '<button class="btn btn-reject" type="button">✎ 改计划</button>'
    : '';
  card.innerHTML = `
    <div class="noplan-title">⚠ 暂无可用计划</div>
    <div class="noplan-detail">勾选底部「LLM 拆分」并配置 API 密钥后重试；或修改需求后重新发送。</div>
    ${questionBlock}
    <div class="approval-actions">
      <button class="btn btn-replan" type="button">↻ 重新规划</button>
      ${rejectBtn}
      <button class="btn btn-danger" type="button">✗ 放弃任务</button>
    </div>
  `;
  card.querySelector('.btn-replan').onclick = () => handleReplan(card);
  const reject = card.querySelector('.btn-reject');
  if (reject) reject.onclick = () => handleReject(card);
  card.querySelector('.btn-danger').onclick = () => handleAbort(card);
  mountChatNode(card);
}

/** 追加「暂无可用计划」卡片（渲染 + 记录，纳入会话条目与裁剪机制）。 */
function appendNoPlanCard(taskId, questions, extra) {
  renderNoPlanCard(taskId, questions, extra);
  recordEntry({
    kind: 'noplan',
    taskId: taskId || null,
    questions: questions || [],
    canReject: Boolean(extra && extra.canReject),
  });
}

/** 「重新规划」：复用当前底部栏规划选项，对卡片所属任务重新调用 /plan。 */
async function handleReplan(card) {
  const taskId = cardTaskId(card);
  if (!taskId) return;
  const button = card.querySelector('.btn-replan');
  if (button) button.disabled = true;
  try {
    // /plan 只接受 PLANNING 状态，而本卡片出现时任务在 awaiting_confirm，
    // 直接重规划必然 409（非法状态转换）。先「改计划」退回 planning 再重新分解；
    // 顺带作废旧计划的放行记录（与 /reject 的语义一致）。
    if (card.dataset.canReject === '1') await api.rejectTask(taskId);
    const useLLM = document.getElementById('llmToggle')?.checked || false;
    const planOptions = readPlanOptions();
    const result = await api.planTask(taskId, useLLM, planOptions);
    clearCardActions(card);
    // 复用与首次发送一致的后续逻辑；若仍为 0 步会重新渲染本卡片，不会递归死锁。
    await applyPlanResult(taskId, result);
  } catch (err) {
    appendMessage('agent', `❌ 重新规划失败：${err.message}`);
  } finally {
    if (button) button.disabled = false;
    await loadTaskSummaries();
    checkHandover();
  }
}

/** 把后端任务状态并进 planState（steps / 高风险动作 / 执行结果）。
 *
 * `steps` **必须回填**：agentic 下步骤由模型逐轮长出来，`applyPlanResult` 拿到的是空
 * `steps`；不回填的话 `stepTitle()` / `actionTitle()` 只能回退英文 action —— 工具卡标题与
 * 「产物」行又会显示 `file_list` 这种机器输出（第 ② 类回归）。
 */
function mergePlanStateFromTask(task) {
  if (!planState || !task) return;
  if (task.steps && Object.keys(task.steps).length) planState.steps = task.steps;
  if (task.high_risk_actions && task.high_risk_actions.length) {
    planState.high_risk_actions = task.high_risk_actions;
  }
  planState.execution_results = task.execution_results || [];
}

/** 应用一次执行结果（/run、/resume、/approve 后共用）。 */
function applyRunResult(task) {
  syncTaskStatus(task.task_id, task.status);
  mergePlanStateFromTask(task);
  renderPlanBlock(planState);
  renderActionSlot(task);
  renderExecutionResults(task);
  renderSelfCheck(task);
  renderRunSummary(task);
  renderConclusion(task);
  updateTitlebar();
}

/** 渲染执行结果卡片（含代码块与「查看产物」入口）。 */
function renderExecutionResults(task) {
  const results = task.execution_results || [];
  let doneCount = 0;
  let failedCount = 0;
  let approvalCount = 0;
  let pendingCount = 0;
  for (const item of results) {
    const cardStatus = EXEC_STATUS_CLASS[item.status] || 'pending';
    if (item.status === 'done') doneCount++;
    else if (item.status === 'failed') failedCount++;
    else if (item.status === 'pending_approval') approvalCount++;
    else pendingCount++;
    // 默认只展示人话摘要（后端 summary）；原始 error / result 一律不倒进卡片。
    const detail = toolCardDetail(item);
    const path = stepArtifactPath(item);
    // thought = 这一轮决策的理由（agentic）；一并收进过程块，别把它丢掉。
    appendToolCard(`${stepTitle(item.step_id, item.action)} · ${item.step_id}`, cardStatus, detail || '(无输出)', {
      path,
      thought: item.thought,
      stepId: item.step_id,
    });
  }
  return { results, doneCount, failedCount, approvalCount, pendingCount };
}

/** 副结构自检报告（开启「执行自检」开关时后端才会返回）。
 *
 * 自检属于「过程」，收进过程块；发现问题时标记 failed → 过程块自动展开，不会漏看。
 */
function renderSelfCheck(task) {
  if (!task.self_check) return;
  const selfCheck = task.self_check;
  const issueCount = (selfCheck.issues || []).length;
  if (selfCheck.passed) {
    appendProcessNote(`执行自检通过：核对 ${selfCheck.checked} 条执行结果，未发现一致性问题。`);
  } else {
    const kinds = (selfCheck.issues || [])
      .map((issue) => `${issue.kind}(${(issue.detail || []).length})`)
      .join('、');
    appendProcessNote(`执行自检发现 ${issueCount} 项问题：${kinds}`, { failed: true });
  }
}

/** 执行结果汇总提示。 */
function renderRunSummary(task) {
  const results = task.execution_results || [];
  if (results.length === 0) return;
  // 已有「结论区」时不再重复播报执行进度 —— 同一件事说两遍反而看不清重点。
  // （需要用户拍板的情况另有提醒卡 / 中断卡兜底，不会被这句省略掉。）
  if (task && task.conclusion) return;
  const doneCount = results.filter((r) => r.status === 'done').length;
  const failedCount = results.filter((r) => r.status === 'failed').length;
  const approvalCount = results.filter((r) => r.status === 'pending_approval').length;
  const pendingCount = results.filter((r) => !['done', 'failed', 'pending_approval'].includes(r.status)).length;
  if (task.status === 'verifying') {
    appendMessage('agent', `所有 ${results.length} 个步骤执行完成（成功 ${doneCount}），进入验证阶段。`);
    return;
  }
  if (task.status === 'interrupted' && task.pending_approval) {
    appendMessage('agent', '执行已「到点暂停」：请在上方提醒卡中放行高风险步骤或放弃任务。');
    return;
  }
  const parts = [`成功 ${doneCount}`];
  if (failedCount) parts.push(`失败 ${failedCount}`);
  if (approvalCount) parts.push(`待审批 ${approvalCount}`);
  if (pendingCount) parts.push(`待执行 ${pendingCount}`);
  appendMessage('agent', `执行进度：${parts.join(' / ')} · 共 ${results.length} 个步骤。`);
}

/** 到点暂停后放行：携带 approved_tools 调用 /resume；仍有未放行步骤会再次挂起。 */
async function handleApprovePending(card) {
  const taskId = cardTaskId(card);
  if (!taskId) return;
  let tools = [];
  try { tools = JSON.parse(card.dataset.approvedTools || '[]'); } catch (err) { tools = []; }
  const button = card.querySelector('.btn-run');
  if (button) button.disabled = true;
  // 放行即离开「等待放行」：先乐观置「执行中」（/resume 同样阻塞），中断按钮才可用。
  markTaskRunning(taskId);
  try {
    const task = await api.resumeTask(taskId, tools);
    appendMessage('agent', tools.length
      ? `已放行高风险工具：${tools.join('、')}，继续执行…`
      : '已继续执行…');
    applyRunResult(task);
  } catch (err) {
    appendMessage('agent', `❌ 放行失败：${err.message}`);
  } finally {
    if (button) button.disabled = false;
    await loadTaskSummaries();
    checkHandover();
  }
}

/** 改计划（/reject）：仅在 awaiting_confirm 时可用，返回重新规划。 */
async function handleReject(card) {
  const taskId = cardTaskId(card);
  if (!taskId) return;
  try {
    const task = await api.rejectTask(taskId);
    syncTaskStatus(taskId, task.status);
    clearCardActions(card);
    appendMessage('agent', '已「改计划」：返回重新规划，可修改需求后重新发送。');
    renderPlanBlock(planState);
    renderActionSlot(null);
    await loadTaskSummaries();
  } catch (err) {
    appendMessage('agent', `❌ 改计划失败：${err.message}`);
  }
}

/** 中断任务（仅执行阶段可用） */
async function handleInterrupt() {
  if (!currentTaskId) {
    appendMessage('agent', '当前无运行中的任务');
    return;
  }
  if (currentStatus !== 'executing') {
    appendMessage('agent', `当前状态「${statusLabel(currentStatus)}」不支持中断，仅执行阶段可中断。`);
    return;
  }
  try {
    const task = await api.interruptTask(currentTaskId, 'user_stop');
    updateTaskStatus(task.status);
    renderActionSlot(task);
    appendMessage('agent', '任务已中断，可恢复继续执行或放弃任务。');
    await loadTaskSummaries();
  } catch (err) {
    appendMessage('agent', `❌ 中断失败：${err.message}`);
  }
}

/** 恢复被中断的任务（手动中断只翻转状态，不自动重放计划）。 */
async function handleResume(card) {
  const taskId = cardTaskId(card);
  if (!taskId) return;
  try {
    const task = await api.resumeTask(taskId);
    syncTaskStatus(taskId, task.status);
    clearCardActions(card);
    appendMessage('agent', '任务已恢复（手动中断不会自动重放计划，可重新发送需求继续）。');
    applyRunResult(task);
    await loadTaskSummaries();
  } catch (err) {
    appendMessage('agent', `❌ 恢复失败：${err.message}`);
  }
}

/** 放弃任务（任意状态可终止） */
async function handleAbort(card) {
  const taskId = cardTaskId(card);
  if (!taskId) return;
  try {
    const task = await api.abortTask(taskId);
    syncTaskStatus(taskId, task.status);
    clearCardActions(card);
    appendMessage('agent', '任务已放弃。');
    renderActionSlot(null);
    await loadTaskSummaries();
  } catch (err) {
    appendMessage('agent', `❌ 放弃失败：${err.message}`);
  }
}

/** 确认交付（仅 verifying 可用）：verifying → delivered，让收敛后的任务真正收尾。 */
async function handleDeliver(card) {
  const taskId = cardTaskId(card);
  if (!taskId) return;
  try {
    const task = await api.deliverTask(taskId);
    syncTaskStatus(taskId, task.status);
    clearCardActions(card);
    appendMessage('agent', '已确认交付，任务完成。');
    renderActionSlot(task);
    await loadTaskSummaries();
  } catch (err) {
    appendMessage('agent', `❌ 确认交付失败：${err.message}`);
  }
}

/** 应用后端任务状态到当前视图（刷新 / 切换任务时调用）。 */
function applyTaskToView(task) {
  currentTaskId = task.task_id;
  if (task.title) currentTaskTitle = task.title;
  updateTaskStatus(task.status);
  updateTitlebar();
  if (planState) {
    mergePlanStateFromTask(task);
  } else if (task.plan || task.steps) {
    // 步骤明细直接取后端 `steps`（GET /tasks/{id} 现返回 Step 明细）。
    // 此前只有 plan.order，换标签页 / 清空会话后回看只剩 step_id 占位，看不到
    // action / 输入 / 人读标题，即「回看不完整」。
    planState = {
      steps: task.steps || {},
      order: (task.plan && task.plan.order) || [],
      parallel_groups: (task.plan && task.plan.parallel_groups) || [],
      high_risk_actions: task.high_risk_actions || [],
      execution_results: task.execution_results || [],
    };
  }
  renderPlanBlock(planState);
  renderActionSlot(task);
  // 结论由后端状态派生（不入 transcript）→ 刷新 / 切换任务时在这里重建。
  renderConclusion(task);
  // 执行过程同样由后端派生：只靠本地 transcript 的话，**不是本机跑的任务点开只有一句结论**
  // （卡片、失败原因、产物入口全看不到）。按 step_id 去重，所以与 transcript 的那份不会打架。
  renderExecutionResults(task);
  if (!transcript.length) reopenTaskEntry(task);
}

/* ──────────────── 事件绑定 ──────────────── */

/** 需求输入框随内容自动增高（高度上限由 CSS 的 max-height 兜底）。 */
function autoGrowInput() {
  const input = document.getElementById('msgInput');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = `${input.scrollHeight}px`;
}

document.getElementById('sendBtn').addEventListener('click', handleSend);
// 中断按钮：缓存引用，避免 updateTaskStatus 每次重查 DOM（审计 P3）。
const interruptBtnEl = document.getElementById('interruptBtn');
interruptBtnEl.addEventListener('click', handleInterrupt);
const msgInputEl = document.getElementById('msgInput');
msgInputEl.addEventListener('input', autoGrowInput);
// Enter 发送、Shift+Enter 换行（多行输入）
msgInputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  }
});

// ＋ 新任务
document.getElementById('newTaskBtn').addEventListener('click', startNewTask);

// 推理强度 slider
const slider = document.getElementById('tempSlider');
const tempValue = document.getElementById('tempValue');
slider.addEventListener('input', () => {
  tempValue.textContent = parseFloat(slider.value).toFixed(1);
});

/* ──────────────── 规划选项（模型 / 推理强度 / 高级 / 执行自检 / 副结构 / 评审会） ────────────────
 * 这几项随 POST /tasks/{id}/plan 透传给后端，在后端真实生效：
 * - model        → LLM 客户端模型名（覆盖 DEEPSEEK_MODEL）
 * - temperature  → LLM 客户端采样温度（前端「推理强度」滑块，0–1）
 * - advanced     → 分解器提示词详细度（default | detailed | concise）
 * - self_check   → 执行阶段追加「计划 ↔ 执行结果」一致性核对（前端叫「执行自检」）
 * - council      → 分解前先召开评审会
 * - sub_arch     → 副结构门控：允许模型提议新角色 / 新工具（前端叫「副结构」）
 * 注：推理强度滑块（slider / tempValue）已在上方声明并绑定实时回显，此处不再重复声明。
 */

const modelSelect = document.getElementById('modelSelect');
const advancedSelect = document.getElementById('advancedSelect');
const selfCheckToggleEl = document.getElementById('selfCheckToggle');
const councilToggleEl = document.getElementById('councilToggle');
// 「副结构」开关：允许模型在运行时提议新角色 / 新工具（副架构扩编）。
const subArchToggleEl = document.getElementById('subArchToggle');
const subArchChipEl = document.getElementById('subArchChip');
const subArchHintEl = document.getElementById('subArchHint');

/** 读取底部栏规划选项，组装成 /plan 的请求字段（缺省项不发送）。 */
function readPlanOptions() {
  const options = {
    advanced: advancedSelect?.value || 'default',
    self_check: selfCheckToggleEl?.checked || false,
    council: councilToggleEl?.checked || false,
    // 副结构门控：关闭时模型只可调用主架构角色；开启后才允许 kind=propose。
    sub_arch: subArchToggleEl?.checked || false,
    // 执行形态：显式下发，避免「前端假设 agentic、后端却是别的默认值」这种漂移。
    mode: EXECUTION_MODE,
  };
  const model = modelSelect?.value || '';
  if (model) options.model = model;
  const temperature = slider ? Number(slider.value) : NaN;
  if (Number.isFinite(temperature)) options.temperature = temperature;
  return options;
}

/** 刷新「副结构」开关的三态与后果提示。
 *
 * 绿色只说明"已开"，不说明"会发生什么"：agentic 循环**必须有可用密钥**才会跑，
 * 所以无密钥时整条禁用并如实说明"当前不会产生提议"，而不是给一个假反馈。
 */
function refreshSubArchChip() {
  const on = Boolean(subArchToggleEl && subArchToggleEl.checked);
  const hasKey = Boolean(apiKeyStatusCache && apiKeyStatusCache.configured);
  if (subArchToggleEl) subArchToggleEl.disabled = !hasKey;
  if (subArchChipEl) {
    subArchChipEl.classList.toggle('is-active', on && hasKey);
    subArchChipEl.classList.toggle('is-disabled', !hasKey);
  }
  if (!subArchHintEl) return;
  if (!on) {
    subArchHintEl.hidden = true;
    subArchHintEl.textContent = '';
    return;
  }
  subArchHintEl.hidden = false;
  subArchHintEl.textContent = hasKey
    ? '副结构已开启：模型可以提议新角色 / 新工具，提议需你批准后才会落盘。'
    : '副结构已开启，但缺少密钥：当前会回退固定工作流，不会产生提议。';
}

subArchToggleEl?.addEventListener('change', refreshSubArchChip);

// 模型 / 高级 / 执行自检 / 评审会：切换时刷新状态栏里的当前规划选项
[modelSelect, advancedSelect, selfCheckToggleEl, councilToggleEl].forEach((el) => {
  el?.addEventListener('change', updateStatusModel);
});

/* ──────────────── API 密钥管理（安全优先） ────────────────
 * 原则：
 * 1. 明文只在输入框中短暂停留，提交后立即清空输入框；
 * 2. 服务端只回传「是否已配置 + 掩码 + 指纹 + TTL/轮换元数据」，前端不再持有明文；
 * 3. 不写入 localStorage / sessionStorage，不做「显示密钥」功能；
 * 4. 支持 TTL（显示剩余有效期）与轮换（复用输入框，提交走 /settings/api-key/rotate）。
 */

const apiKeyInputChip = document.getElementById('apiKeyInputChip');
const apiKeyStatusChip = document.getElementById('apiKeyStatusChip');
const apiKeyInput = document.getElementById('apiKeyInput');
const apiKeyMasked = document.getElementById('apiKeyMasked');
const apiKeyStateText = document.getElementById('apiKeyStateText');
const apiKeyExpiry = document.getElementById('apiKeyExpiry');

const KEY_PLACEHOLDER = '输入 API 密钥';
const KEY_ROTATE_PLACEHOLDER = '输入新密钥以轮换';

// 是否处于「轮换」流程：复用输入框，提交时改走 rotate 端点。
let keyRotateMode = false;

// 最近一次服务端密钥状态：供本地定时器重算剩余时间，无需反复请求后端。
let apiKeyStatusCache = null;

/** 剩余秒数格式化：3d2h / 12h30m / 5m。 */
function formatRemaining(seconds) {
  if (seconds === null || seconds === undefined) return '';
  if (seconds <= 0) return '已过期';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `剩余 ${days}d${hours}h`;
  if (hours) return `剩余 ${hours}h${minutes}m`;
  return `剩余 ${Math.max(1, minutes)}m`;
}

/** 到期信息文案：剩余时间（未过期）/ 到期时刻（已过期）/ 空串（未设置 TTL）。 */
function expiryText(status) {
  if (!status) return '';
  if (status.expired) {
    return status.expires_at ? `（${new Date(status.expires_at).toLocaleString()}）` : '';
  }
  return formatRemaining(status.remaining_s);
}

/** 是否临近到期（10 分钟内）：用于警示色。 */
function isExpiringSoon(status) {
  return Boolean(status && typeof status.remaining_s === 'number' && status.remaining_s <= 600);
}

/** 按缓存的到期时刻本地重算剩余时间，避免到期文案一直停在旧值（审计 P2）。 */
function refreshApiKeyExpiry() {
  const cached = apiKeyStatusCache;
  if (!cached || !cached.expires_at) return;
  const remaining = (new Date(cached.expires_at).getTime() - Date.now()) / 1000;
  if (remaining <= 0) {
    apiKeyExpiry.textContent = expiryText({ expired: true, expires_at: cached.expires_at });
    apiKeyExpiry.classList.add('danger');
    if (cached.configured) {
      // 首次本地发现到期：向后端确认一次（后端已清除明文，会回传 expired 状态）
      loadApiKeyStatus();
    }
    return;
  }
  apiKeyExpiry.textContent = formatRemaining(remaining);
  apiKeyExpiry.classList.toggle('danger', remaining <= 600);
}

/** 按服务端状态渲染密钥区：
 *  已配置 → 掩码 + 剩余有效期 + 轮换/删除；
 *  已过期 → 同时保留输入框（可重设）并提示是哪把钥匙过期了。
 */
function renderApiKeyStatus(status) {
  apiKeyStatusCache = status;
  updateStatusKey(status);
  // 「副结构」开关的可用性取决于有没有可用密钥 → 密钥状态一变就同步刷新。
  refreshSubArchChip();
  const configured = Boolean(status && status.configured);
  const expired = Boolean(status && status.expired);

  // 轮换流程进行中：保持输入框可见、状态条隐藏，且绝不触碰输入框内容。
  // 否则密钥到期定时器触发的后台刷新会打断轮换并清空用户已输入的新密钥（审计 P2）。
  if (keyRotateMode) {
    apiKeyInputChip.hidden = false;
    apiKeyStatusChip.hidden = true;
    apiKeyInput.placeholder = KEY_ROTATE_PLACEHOLDER;
    return;
  }

  apiKeyInputChip.hidden = configured;
  apiKeyStatusChip.hidden = !(configured || expired);

  if (configured) {
    apiKeyStateText.textContent = '已保存';
    apiKeyMasked.textContent = status.masked || '已配置';
  } else if (expired) {
    apiKeyStateText.textContent = '已过期';
    apiKeyMasked.textContent = status.masked || '—';
  } else {
    apiKeyMasked.textContent = '—';
  }
  apiKeyExpiry.textContent = expiryText(status);
  apiKeyExpiry.classList.toggle('danger', expired || isExpiringSoon(status));

  if (!configured) {
    apiKeyInput.value = ''; // 未配置/已过期时不留任何残留明文
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

/** 保存或轮换密钥：先清空输入框，再按服务端状态渲染，绝不回显。 */
async function handleApiKeySave() {
  const raw = apiKeyInput.value;
  if (!raw.trim()) return;
  const rotating = keyRotateMode;
  try {
    const status = rotating
      ? await api.rotateApiKey(raw.trim())
      : await api.setApiKey(raw.trim());
    apiKeyInput.value = ''; // 无论成败都立即清空，避免明文驻留
    keyRotateMode = false;
    apiKeyInput.placeholder = KEY_PLACEHOLDER;
    renderApiKeyStatus(status);
    if (rotating) {
      const from = status.rotated_from_fingerprint || '—';
      const to = status.fingerprint || '—';
      appendMessage('agent', `API 密钥已轮换（旧指纹 ${from} → 新指纹 ${to}，累计 ${status.rotation_count} 次）。`);
    } else {
      appendMessage('agent', 'API 密钥已保存到后端（仅驻留内存，不回显）。如需移除可点击「删除密钥」。');
    }
  } catch (err) {
    apiKeyInput.value = '';
    keyRotateMode = false;
    apiKeyInput.placeholder = KEY_PLACEHOLDER;
    await loadApiKeyStatus(); // 回到服务端真实状态，避免界面停留在轮换态
    appendMessage('agent', `❌ 密钥${rotating ? '轮换' : '保存'}失败：${err.message}`);
  }
}

/** 进入轮换流程：复用输入框，提交时改走 rotate 端点。 */
function startApiKeyRotate() {
  keyRotateMode = true;
  apiKeyStatusChip.hidden = true;
  apiKeyInputChip.hidden = false;
  apiKeyInput.value = '';
  apiKeyInput.placeholder = KEY_ROTATE_PLACEHOLDER;
  apiKeyInput.focus();
  appendMessage('agent', '请输入新密钥并点「保存」完成轮换；旧密钥将被替换，服务不中断。');
}

/** 删除密钥：二次确认，确认文案不包含任何密钥内容。 */
async function handleApiKeyDelete() {
  if (!window.confirm('确定删除已保存的 API 密钥吗？删除后不可恢复，需要重新输入。')) return;
  try {
    const status = await api.deleteApiKey();
    keyRotateMode = false;
    apiKeyInput.placeholder = KEY_PLACEHOLDER;
    renderApiKeyStatus(status);
    appendMessage('agent', 'API 密钥已删除。');
  } catch (err) {
    appendMessage('agent', `❌ 密钥删除失败：${err.message}`);
  }
}

document.getElementById('apiKeySave').addEventListener('click', handleApiKeySave);
document.getElementById('apiKeyRotate').addEventListener('click', startApiKeyRotate);
document.getElementById('apiKeyDelete').addEventListener('click', handleApiKeyDelete);
apiKeyInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    handleApiKeySave();
  }
});

/* ──────────────── 未接线控件的明确提示 ────────────────
 * 技能开关与「＋添加插件」目前没有后端支撑（审计 P3：假控件）。
 * 保留控件形态，但操作时给出可见提示，不再只 console.log 假装可用。 */

const NOT_WIRED_HINT = '该能力尚未接入后端，当前仅作展示。';

/** 首次操作某控件时给出可见提示（同一控件只提示一次，避免刷屏）。 */
function notifyNotWired(name, el) {
  if (el && el.dataset.notWiredNotified === '1') return;
  if (el) el.dataset.notWiredNotified = '1';
  appendMessage('agent', `ℹ️ 「${name}」${NOT_WIRED_HINT}`);
}

// 技能开关
document.querySelectorAll('.sidebar-left .toggle input').forEach((t) => {
  t.addEventListener('change', () => notifyNotWired(t.getAttribute('aria-label') || '技能开关', t));
});

// 「＋ 添加插件」
const pluginAddBtn = document.querySelector('.plugin-add');
pluginAddBtn?.addEventListener('click', () => notifyNotWired('添加插件', pluginAddBtn));

/* ──────────────── 菜单栏 ──────────────── */

const menuPopupEl = document.getElementById('menuPopup');
const MENU_DEFS = {
  file: [
    { label: '新建任务', accel: 'Alt+N', action: 'new-task' },
    { label: '打开本地文件夹…', action: 'open-folder' },
    { label: '新建文件夹…', action: 'new-folder' },
    { sep: true },
    { label: '使用默认工作区', action: 'default-workspace' },
    { label: '刷新产物列表', action: 'refresh-tree' },
  ],
  edit: [
    { label: '聚焦需求输入', accel: 'Alt+I', action: 'focus-input' },
    { label: '清空需求输入', action: 'clear-input' },
  ],
  view: [
    { label: '展开 / 收起侧栏', action: 'toggle-nav' },
    { label: '刷新产物列表', action: 'refresh-tree' },
    { label: '刷新任务列表', action: 'refresh-tasks' },
  ],
  task: [
    { label: '继续放行（高风险步骤）', action: 'resume-pending' },
    { label: '改计划（返回重新规划）', action: 'reject-plan' },
    { label: '中断当前任务', accel: 'Alt+.', action: 'interrupt' },
    { label: '放弃当前任务', action: 'abort' },
    { sep: true },
    { label: '召开评审会', action: 'council' },
    { label: '刷新任务列表', action: 'refresh-tasks' },
  ],
  help: [
    { label: '关于 Agent Builder', action: 'about' },
    { label: '后端连通性自检', action: 'check-health' },
  ],
};

/** 菜单项是否可用（依据当前任务状态）。 */
function menuItemDisabled(action) {
  if (action === 'interrupt') return currentStatus !== 'executing';
  if (action === 'reject-plan') return currentStatus !== 'awaiting_confirm';
  if (action === 'resume-pending') return !currentPendingApproval;
  if (action === 'abort') return !currentTaskId;
  if (action === 'council') return !currentTaskId;
  return false;
}

/** 展开菜单（在菜单栏按钮下方定位）。 */
function openMenu(name, rootBtn) {
  const defs = MENU_DEFS[name] || [];
  menuPopupEl.innerHTML = '';
  menuPopupEl.dataset.menu = name;
  defs.forEach((item) => {
    if (item.sep) {
      const sep = document.createElement('div');
      sep.className = 'menu-sep';
      menuPopupEl.appendChild(sep);
      return;
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'menu-item';
    btn.setAttribute('role', 'menuitem');
    btn.dataset.action = item.action;
    btn.disabled = menuItemDisabled(item.action);
    const label = document.createElement('span');
    label.className = 'mi-label';
    label.textContent = item.label;
    btn.appendChild(label);
    if (item.accel) {
      const accel = document.createElement('span');
      accel.className = 'mi-accel';
      accel.textContent = item.accel;
      btn.appendChild(accel);
    }
    menuPopupEl.appendChild(btn);
  });
  const rect = rootBtn.getBoundingClientRect();
  menuPopupEl.style.left = `${Math.max(4, Math.round(rect.left))}px`;
  menuPopupEl.style.top = `${Math.round(rect.bottom + 2)}px`;
  menuPopupEl.hidden = false;
  document.querySelectorAll('.menu-root').forEach((el) => {
    el.setAttribute('aria-expanded', String(el === rootBtn));
  });
}

function closeMenu() {
  if (menuPopupEl.hidden) return;
  menuPopupEl.hidden = true;
  menuPopupEl.dataset.menu = '';
  document.querySelectorAll('.menu-root').forEach((el) => el.setAttribute('aria-expanded', 'false'));
}

/** 执行菜单动作。 */
async function runMenuAction(action) {
  if (action === 'new-task') await startNewTask();
  else if (action === 'open-folder') await openLocalFolder();
  else if (action === 'new-folder') await startNewFolder();
  else if (action === 'default-workspace') await switchProject('default');
  else if (action === 'refresh-tree') loadFileTree();
  else if (action === 'refresh-tasks') await loadTaskSummaries();
  else if (action === 'focus-input') document.getElementById('msgInput').focus();
  else if (action === 'clear-input') { const input = document.getElementById('msgInput'); input.value = ''; autoGrowInput(); input.focus(); }
  else if (action === 'toggle-nav') setNavOpen(!document.body.classList.contains('nav-open'));
  else if (action === 'resume-pending') {
    const card = document.querySelector('#actionSlot .reminder-card');
    if (card) await handleApprovePending(card);
    else appendMessage('agent', '当前没有待放行的高风险步骤。');
  } else if (action === 'reject-plan') {
    await handleReject(taskScopeEl(currentTaskId));
  } else if (action === 'interrupt') await handleInterrupt();
  else if (action === 'abort') {
    const card = document.querySelector('#actionSlot .approval-card, #actionSlot .reminder-card');
    if (card) await handleAbort(card);
    else if (currentTaskId) await handleAbort(taskScopeEl(currentTaskId));
  } else if (action === 'council') await handleCouncil();
  else if (action === 'check-health') await checkHealth();
  else if (action === 'about') {
    appendMessage('agent', 'Agent Builder：FastAPI 状态机 + 20 角色 / 26 工具的 Meta Agent；密钥仅驻留服务端内存，会话只存 sessionStorage。');
  }
}

/** 菜单栏事件：点根菜单开合，点条目执行并收起。 */
document.getElementById('menubar').addEventListener('click', (e) => {
  const root = e.target.closest('.menu-root');
  if (!root) return;
  const name = root.dataset.menu;
  if (!menuPopupEl.hidden && menuPopupEl.dataset.menu === name) closeMenu();
  else openMenu(name, root);
});
menuPopupEl.addEventListener('click', (e) => {
  const item = e.target.closest('.menu-item');
  if (!item || item.disabled) return;
  closeMenu();
  runMenuAction(item.dataset.action);
});
document.addEventListener('click', (e) => {
  if (menuPopupEl.hidden) return;
  if (!e.target.closest('.menu-popup') && !e.target.closest('.menu-root')) closeMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !menuPopupEl.hidden) closeMenu();
});
window.addEventListener('resize', closeMenu);

/* ──────────────── 窗口控制（视觉装饰） ──────────────── */

/** 窗口控制在网页版只是视觉装饰（不做真实窗口管理），首次点击给出说明。 */
function notifyWindowDecor(name, el) {
  if (el && el.dataset.decorNotified === '1') return;
  if (el) el.dataset.decorNotified = '1';
  appendMessage('agent', `ℹ️ 「${name}」在网页版为视觉装饰，不做真实窗口管理。`);
}

['winMin', 'winMax', 'winClose'].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('click', () => notifyWindowDecor(el.getAttribute('aria-label') || id, el));
});

/* ──────────────── 抽屉侧栏 + 状态栏交互 ──────────────── */

const navMask = document.getElementById('navMask');
const menuBtnEl = document.getElementById('menuBtn');
// 状态栏元素引用（供各 update 函数使用）
const statusBackendEl = document.getElementById('statusBackend');
const statusDotEl = document.getElementById('statusDot');
const statusBackendTextEl = document.getElementById('statusBackendText');
const statusProjectEl = document.getElementById('statusProject');
const statusModelEl = document.getElementById('statusModel');
const statusKeyEl = document.getElementById('statusKey');
const statusTaskEl = document.getElementById('statusTask');
const titlebarTaskEl = document.getElementById('titlebarTask');
const toolbarTaskEl = document.getElementById('toolbarTask');
// 顶栏「当前状态」标签：缓存引用，避免 updateTaskStatus 每次重查 DOM。
const taskStatusChipEl = document.getElementById('taskStatusChip');
const taskStatusTextEl = document.getElementById('taskStatusText');

/** 开合抽屉侧栏（仅窄屏可见），并同步 aria-expanded 与遮罩。 */
function setNavOpen(open) {
  document.body.classList.toggle('nav-open', open);
  navMask.hidden = !open;
  menuBtnEl.setAttribute('aria-expanded', String(open));
}

menuBtnEl.addEventListener('click', () => setNavOpen(!document.body.classList.contains('nav-open')));
navMask.addEventListener('click', () => setNavOpen(false));
// 视口离开窄屏时复位抽屉：否则「拉宽再拉回」后抽屉会停留在打开态（审计 P3）。
window.matchMedia('(max-width: 600px)').addEventListener('change', (e) => {
  if (!e.matches) setNavOpen(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.body.classList.contains('nav-open')) setNavOpen(false);
});

/** 后端连通性自检。 */
async function checkHealth() {
  try {
    await api.health();
    backendOnline = true;
  } catch (err) {
    backendOnline = false;
  }
  updateStatusBackend();
}

// 状态栏交互：连通性自检 / 聚焦模型与密钥 / 工作区详情 / 当前状态
statusBackendEl.addEventListener('click', checkHealth);
statusModelEl.addEventListener('click', () => modelSelect?.focus());
statusKeyEl.addEventListener('click', () => apiKeyInput?.focus());
statusProjectEl.addEventListener('click', () => appendMessage('agent', `当前工作区：${currentWorkspaceLabel || '—'}`));
statusTaskEl.addEventListener('click', () => appendMessage('agent', currentTaskId
  ? `当前任务 ${String(currentTaskId).slice(0, 8)}… · 状态：${statusLabel(currentStatus)}`
  : '当前未创建任务。'));

/* ──────────────── 项目（工作区）─────────────── */

const projectListEl = document.getElementById('projectList');
const workspacePathEl = document.getElementById('workspacePath');
const projectMenu = document.querySelector('.project-menu');
const newProjectWrap = document.querySelector('.new-project-wrap');
const newProjectBtn = document.querySelector('.new-project-btn');
// 新建文件夹对话框
const newFolderDialog = document.getElementById('newFolderDialog');
const newFolderName = document.getElementById('newFolderName');
const newFolderParent = document.getElementById('newFolderParent');
const newFolderConfirm = document.getElementById('newFolderConfirm');
const newFolderCancel = document.getElementById('newFolderCancel');
let newFolderParentPath = '';

/** 展开/收起「新建项目」二级菜单，并同步 aria-expanded。 */
function setProjectMenuOpen(open) {
  projectMenu.hidden = !open;
  newProjectBtn.setAttribute('aria-expanded', String(open));
}

newProjectBtn.addEventListener('click', (e) => {
  e.stopPropagation(); // 阻止冒泡到 document（否则同一击立即被"点击外部"逻辑收起）
  setProjectMenuOpen(projectMenu.hidden);
});
document.addEventListener('click', (e) => {
  if (projectMenu.hidden) return;
  if (!newProjectWrap.contains(e.target)) setProjectMenuOpen(false);
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !projectMenu.hidden) {
    setProjectMenuOpen(false);
    newProjectBtn.focus();
  }
});

/** 渲染最近项目列表与当前工作区路径；点击条目即切换到该项目。 */
function renderProjects(data) {
  const projects = (data && data.projects) || [];
  const currentId = data && data.current_project_id;
  const current = projects.find((item) => item.project_id === currentId);
  currentWorkspaceLabel = current ? current.name : ((data && data.workspace_dir) || '');
  if (workspacePathEl) {
    const label = current ? `${current.name} · ${current.path}` : (data && data.workspace_dir) || '（未知）';
    workspacePathEl.textContent = `工作区 / ${label}`;
    workspacePathEl.title = (data && data.workspace_dir) || '';
  }
  updateStatusProject();
  projectListEl.innerHTML = '';
  if (projects.length === 0) {
    projectListEl.innerHTML = '<div class="project-empty">暂无项目</div>';
    return;
  }
  projects.forEach((project) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `project-item${project.project_id === currentId ? ' is-current' : ''}`;
    btn.dataset.projectId = project.project_id;
    btn.title = project.path;
    const name = document.createElement('span');
    name.className = 'project-item-name';
    name.textContent = project.name;
    const path = document.createElement('span');
    path.className = 'project-item-path';
    path.textContent = project.path;
    btn.append(name, path);
    btn.addEventListener('click', () => switchProject(project.project_id));
    projectListEl.appendChild(btn);
  });
}

/** 拉取项目列表（初始化与每次切换工作区后调用）。 */
async function loadProjects() {
  try {
    renderProjects(await api.listProjects());
  } catch (err) {
    projectListEl.innerHTML =
      `<div class="project-empty">项目列表加载失败：${escapeHtml(err.message)}</div>`;
  }
}

/** 切换到已登记的项目（project_id="default" 为内置默认工作区）。 */
async function switchProject(projectId) {
  try {
    await api.openProject(projectId);
  } catch (err) {
    appendMessage('agent', `❌ 切换工作区失败：${err.message}`);
    return;
  }
  await enterProjectWorkspace();
}

/** 进入当前项目工作区：先终止旧任务 → 再清空对话 → 刷新项目列表与产物树。 */
async function enterProjectWorkspace() {
  saveSession();
  await stopActiveTask();      // 先终止后端任务（防孤儿任务）
  clearChatUI();               // 再清空界面
  setNavOpen(false); // 窄屏：切换后收起抽屉
  setProjectMenuOpen(false);
  clearSession();
  currentTaskId = null;
  currentTaskTitle = '';
  updateTaskStatus('received');
  updateTitlebar();
  await loadProjects();
  loadFileTree();
  await loadTaskSummaries();
  const input = document.getElementById('msgInput');
  if (input) input.focus();
}

/** 「打开本地文件夹」：后端弹系统对话框 → 登记为项目并进入。 */
async function openLocalFolder() {
  let picked;
  try {
    picked = await api.pickDirectory();
  } catch (err) {
    appendMessage('agent', `❌ 无法打开系统文件夹对话框：${err.message}`);
    return;
  }
  if (picked.cancelled) return;
  await registerAndEnter(picked.path);
}

/** 「新建文件夹」第一步：选父目录，再让用户输入子目录名。 */
async function startNewFolder() {
  let picked;
  try {
    picked = await api.pickDirectory();
  } catch (err) {
    appendMessage('agent', `❌ 无法打开系统文件夹对话框：${err.message}`);
    return;
  }
  if (picked.cancelled) return;
  newFolderParentPath = picked.path;
  newFolderParent.textContent = `父目录：${picked.path}`;
  newFolderParent.title = picked.path;
  newFolderName.value = '';
  newFolderDialog.hidden = false;
  newFolderName.focus();
}

/** 「新建文件夹」第二步：后端创建目录 → 登记为项目并进入。 */
async function confirmNewFolder() {
  const name = newFolderName.value.trim();
  if (!name) {
    newFolderName.focus();
    return;
  }
  newFolderConfirm.disabled = true;
  try {
    const created = await api.createWorkspaceFolder(newFolderParentPath, name);
    closeNewFolderDialog();
    await registerAndEnter(created.path);
  } catch (err) {
    newFolderParent.textContent = `创建失败：${err.message}`;
  } finally {
    newFolderConfirm.disabled = false;
  }
}

/** 关闭新建文件夹对话框并复位。 */
function closeNewFolderDialog() {
  newFolderDialog.hidden = true;
  newFolderParentPath = '';
  newFolderName.value = '';
}

/** 登记目录为项目并进入（同路径已登记则复用）。 */
async function registerAndEnter(path) {
  let project;
  try {
    project = await api.createProject(path);
  } catch (err) {
    appendMessage('agent', `❌ 无法使用该目录作为工作区：${err.message}`);
    return;
  }
  await enterProjectWorkspace();
  appendMessage('agent', `📁 已切换到工作区「${project.name}」：${project.path}`);
}

// 二级菜单三个动作
projectMenu.addEventListener('click', (e) => {
  const item = e.target.closest('.project-menu-item');
  if (!item) return;
  setProjectMenuOpen(false);
  if (item.dataset.action === 'open-folder') openLocalFolder();
  else if (item.dataset.action === 'new-folder') startNewFolder();
  else if (item.dataset.action === 'default-workspace') switchProject('default');
});

// 新建文件夹对话框：确认 / 取消 / 点遮罩 / 回车 / Esc
newFolderConfirm.addEventListener('click', confirmNewFolder);
newFolderCancel.addEventListener('click', closeNewFolderDialog);
newFolderDialog.addEventListener('click', (e) => {
  if (e.target.dataset && e.target.dataset.close) closeNewFolderDialog();
});
newFolderName.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    confirmNewFolder();
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !newFolderDialog.hidden) {
    closeNewFolderDialog();
    newProjectBtn.focus();
  }
});

// 刷新按钮 → 重新拉取产物树（后端当前无上传接口）
document.querySelector('.file-refresh-btn')?.addEventListener('click', loadFileTree);

/* ──────────────── 快捷键（非浏览器保留组合） ──────────────── */
document.addEventListener('keydown', (e) => {
  if (!e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.key === 'n' || e.key === 'N') {
    e.preventDefault();
    startNewTask();
  } else if (e.key === 'i' || e.key === 'I') {
    e.preventDefault();
    document.getElementById('msgInput').focus();
  } else if (e.key === '.') {
    e.preventDefault();
    handleInterrupt();
  }
});

// 初始化：先恢复刷新前的会话与任务连接，再检测后端连通性 + 加载文件树/任务/项目/密钥
(async () => {
  // 已恢复历史会话时不再重复插入连接提示，避免每次刷新都多出一条会话记录。
  const restored = await restoreSession();
  try {
    await api.health();
    backendOnline = true;
    if (!restored) appendMessage('agent', '✅ 后端服务已连接，可开始输入需求。');
  } catch (err) {
    backendOnline = false;
    appendMessage('agent', `⚠️ ${err.message}`);
  }
  updateStatusBackend();
  if (!currentStatus) updateTaskStatus('received');
  updateTitlebar();
  updateStatusModel();
  loadFileTree();
  loadProjects();
  loadApiKeyStatus();
  loadTaskSummaries();
  bindHandoverSlot();
  // 每 30s 本地刷新密钥剩余有效期，避免一直停在旧值（审计 P2）
  setInterval(refreshApiKeyExpiry, 30000);
})();
