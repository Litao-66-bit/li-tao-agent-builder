/* ============================================================
 * Agent Builder · 桌面版原型 —— 应用逻辑
 *
 * 本文件只做两件事：
 *   ① 复刻产品现有前端的界面结构与交互（消息 / 工具卡 / 审批卡 /
 *      评审会纪要 / 中断卡 / 交接提示 / 文件树 / 项目切换）；
 *   ② 叠加桌面应用 chrome（标题栏窗口控制、菜单栏、状态栏、
 *      右键菜单、模拟原生「选择文件夹」对话框）。
 * 所有网络调用都走 api.js 的桩接口，不发真实请求。
 * ============================================================ */
(function () {
  'use strict';

  const MOCK = window.MOCK;
  const api = window.api;

  const $ = (id) => document.getElementById(id);

  /* ──────────────── 元素引用 ──────────────── */
  const appWindow = $('appWindow');
  const titlebarProjectName = $('titlebarProjectName');
  const titlebarProject = $('titlebarProject');
  const winMin = $('winMin');
  const winMax = $('winMax');
  const winClose = $('winClose');
  const menubar = $('menubar');
  const menuPopup = $('menuPopup');
  const contextMenu = $('contextMenu');
  const stageBar = $('stageBar');
  const taskChip = $('taskChip');
  const projectList = $('projectList');
  const projectMenu = $('projectMenu');
  const newProjectBtn = $('newProjectBtn');
  const workspacePath = $('workspacePath');
  const fileTree = $('fileTree');
  const fileRefreshBtn = $('fileRefreshBtn');
  const chatArea = $('chatArea');
  const chatLog = $('chatLog');
  const handoverSlot = $('handoverSlot');
  const msgInput = $('msgInput');
  const sendBtn = $('sendBtn');
  const interruptBtn = $('interruptBtn');
  const modelSelect = $('modelSelect');
  const tempSlider = $('tempSlider');
  const tempValue = $('tempValue');
  const apiKeyInputChip = $('apiKeyInputChip');
  const apiKeyStatusChip = $('apiKeyStatusChip');
  const apiKeyInput = $('apiKeyInput');
  const apiKeyMasked = $('apiKeyMasked');
  const apiKeyStateText = $('apiKeyStateText');
  const apiKeyExpiry = $('apiKeyExpiry');
  const apiKeySave = $('apiKeySave');
  const apiKeyRotate = $('apiKeyRotate');
  const apiKeyDelete = $('apiKeyDelete');
  const statusBackend = $('statusBackend');
  const statusProject = $('statusProject');
  const statusModel = $('statusModel');
  const statusKey = $('statusKey');
  const statusTask = $('statusTask');
  const fileViewer = $('fileViewer');
  const fileViewerPath = $('fileViewerPath');
  const fileViewerMeta = $('fileViewerMeta');
  const fileViewerBody = $('fileViewerBody');
  const fileViewerSave = $('fileViewerSave');
  const fileViewerClose = $('fileViewerClose');
  const folderPicker = $('folderPicker');
  const folderPickerPath = $('folderPickerPath');
  const folderPickerList = $('folderPickerList');
  const folderPickerConfirm = $('folderPickerConfirm');
  const folderPickerCancel = $('folderPickerCancel');
  const folderPickerX = $('folderPickerX');
  const newFolderDialog = $('newFolderDialog');
  const newFolderName = $('newFolderName');
  const newFolderParent = $('newFolderParent');
  const newFolderConfirm = $('newFolderConfirm');
  const newFolderCancel = $('newFolderCancel');
  const aboutDialog = $('aboutDialog');
  const aboutVersion = $('aboutVersion');
  const aboutClose = $('aboutClose');
  const demoConsole = $('demoConsole');
  const demoConsoleClose = $('demoConsoleClose');
  const desktopIconApp = $('desktopIconApp');
  const taskbarAppBtn = $('taskbarAppBtn');
  const trayClock = $('trayClock');

  // 视口过窄时不再为演示控制台预留右侧桌面栏（否则应用窗口会被挤得过小）
  const narrowMQ = window.matchMedia('(max-width: 1200px)');

  /* ──────────────── 状态 ──────────────── */
  const state = {
    theme: 'auto',
    scenario: 'empty',
    status: 'received',
    taskId: null,
    lastStageIdx: 0,
    handover: null,
    handoverPick: 'human',
    handoverDismissed: false,
    isSending: false,
    openMenu: null,
    contextNode: null,
    viewerPath: null,
    folderSelected: MOCK.folderPicker.selected,
    folderParent: null,
    toggles: { sidebars: true, console: true, statusbar: true },
  };

  /* ──────────────── 通用工具 ──────────────── */
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function formatSize(bytes) {
    if (bytes == null) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function formatNum(n) {
    return Number(n || 0).toLocaleString('zh-CN');
  }

  function scrollChatToBottom() {
    chatArea.scrollTop = chatArea.scrollHeight;
  }

  function currentProject() {
    return MOCK.projects.find((p) => p.current) || MOCK.projects[0];
  }

  /* ════════════════ 窗口 chrome ════════════════ */
  function setWindowMaximized(on) {
    document.body.classList.toggle('window-maximized', on);
    winMax.setAttribute('aria-pressed', String(on));
    winMax.title = on ? '还原' : '最大化';
    closeMenu();
  }

  function toggleMaximized() {
    setWindowMaximized(!document.body.classList.contains('window-maximized'));
  }

  function minimizeWindow() {
    document.body.classList.add('window-closed');
    taskbarAppBtn.setAttribute('aria-pressed', 'false');
    closeMenu();
  }

  function closeWindow() {
    document.body.classList.add('window-closed');
    taskbarAppBtn.setAttribute('aria-pressed', 'false');
    closeMenu();
  }

  function restoreWindow() {
    document.body.classList.remove('window-closed');
    taskbarAppBtn.setAttribute('aria-pressed', 'true');
  }

  function bindWindowChrome() {
    winMin.addEventListener('click', minimizeWindow);
    winMax.addEventListener('click', toggleMaximized);
    winClose.addEventListener('click', closeWindow);
    desktopIconApp.addEventListener('dblclick', restoreWindow);
    desktopIconApp.addEventListener('click', restoreWindow);
    taskbarAppBtn.addEventListener('click', () => {
      if (document.body.classList.contains('window-closed')) restoreWindow();
      else minimizeWindow();
    });
    // 必须 stopPropagation：否则事件冒泡到 document 的「点外部关菜单」监听，
    // 菜单会在打开的同一帧被立刻关闭（表现为「菜单一闪就没」）。
    titlebarProject.addEventListener('click', (event) => {
      event.stopPropagation();
      openMenu('project', titlebarProject);
    });
  }

  /* ════════════════ 主题 ════════════════ */
  function applyTheme(theme) {
    state.theme = theme;
    if (theme === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    try { sessionStorage.setItem('agent-builder-desktop:theme', theme); } catch (err) { /* 忽略隐私模式 */ }
    document.querySelectorAll('#themeRow .demo-chip').forEach((chip) => {
      chip.classList.toggle('is-active', chip.dataset.theme === theme);
    });
    if (state.openMenu === 'view') openMenu('view', findMenuRoot('view'), true);
  }

  /* ════════════════ 菜单栏 ════════════════ */
  function findMenuRoot(name) {
    return menubar.querySelector(`.menu-root[data-menu="${name}"]`);
  }

  function buildMenuItems(name) {
    const items = MOCK.menus[name].slice();
    if (name === 'project') {
      const recent = MOCK.projects.map((p) => ({
        type: 'item',
        label: `${p.current ? '● ' : '　'}${p.name}`,
        action: `open-project:${p.id}`,
      }));
      // 「最近项目」标题之后插入动态项
      return items.slice(0, 1).concat(recent, items.slice(1));
    }
    return items;
  }

  function renderMenuPopup(name) {
    const items = buildMenuItems(name);
    menuPopup.innerHTML = items.map((item) => {
      if (item.type === 'sep') return '<div class="menu-sep"></div>';
      if (item.type === 'heading') return `<div class="menu-heading">${escapeHtml(item.label)}</div>`;
      const checked = item.type === 'check' ? item.checked
        : (item.type === 'radio' ? state.theme === item.value : false);
      const check = (item.type === 'check' || item.type === 'radio')
        ? `<span class="mi-check">${checked ? '✓' : ''}</span>`
        : '<span class="mi-check"></span>';
      const accel = item.accel ? `<span class="mi-accel">${escapeHtml(item.accel)}</span>` : '';
      const value = item.value ? ` data-value="${escapeHtml(item.value)}"` : '';
      return `<button type="button" class="menu-item" role="menuitem" data-action="${escapeHtml(item.action)}"${value}`
        + (item.disabled ? ' disabled' : '')
        + `>${check}<span class="mi-label">${escapeHtml(item.label)}</span>${accel}</button>`;
    }).join('');
  }

  function positionPopup(el, anchorRect) {
    const win = appWindow.getBoundingClientRect();
    let left = anchorRect.left - win.left;
    const maxLeft = win.width - el.offsetWidth - 8;
    left = Math.max(8, Math.min(left, maxLeft));
    let top = anchorRect.bottom - win.top + 2;
    if (top + el.offsetHeight > win.height - 8) {
      top = Math.max(8, anchorRect.top - win.top - el.offsetHeight - 2);
    }
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  }

  function openMenu(name, rootBtn, isDrilldown) {
    if (state.openMenu === name && !isDrilldown) { closeMenu(); return; }
    renderMenuPopup(name);
    menuPopup.hidden = false;
    menubar.querySelectorAll('.menu-root').forEach((b) => {
      b.setAttribute('aria-expanded', String(b === rootBtn));
    });
    positionPopup(menuPopup, rootBtn.getBoundingClientRect());
    state.openMenu = name;
  }

  function closeMenu() {
    menuPopup.hidden = true;
    state.openMenu = null;
    menubar.querySelectorAll('.menu-root').forEach((b) => b.setAttribute('aria-expanded', 'false'));
  }

  function menuLabel(name) {
    return { file: '文件', edit: '编辑', view: '视图', project: '项目', task: '任务', help: '帮助' }[name];
  }

  function bindMenubar() {
    menubar.querySelectorAll('.menu-root').forEach((btn) => {
      btn.addEventListener('click', (event) => {
        event.stopPropagation();
        openMenu(btn.dataset.menu, btn);
      });
      btn.addEventListener('mouseenter', () => {
        if (state.openMenu && state.openMenu !== btn.dataset.menu) openMenu(btn.dataset.menu, btn);
      });
    });

    menuPopup.addEventListener('click', (event) => {
      const item = event.target.closest('.menu-item');
      if (!item || item.disabled) return;
      const name = state.openMenu;
      closeMenu();
      handleMenuAction(item.dataset.action, name, item.dataset.value);
    });

    document.addEventListener('click', (event) => {
      if (!state.openMenu) return;
      if (menuPopup.contains(event.target)) return;
      if (event.target.closest('.menu-root')) return;
      closeMenu();
    });
    menubar.addEventListener('contextmenu', (e) => e.preventDefault());
  }

  /* ── 菜单动作分发 ── */
  function handleMenuAction(action, menuName, value) {
    if (action.startsWith('open-project:')) {
      switchProject(action.split(':')[1]);
      return;
    }
    switch (action) {
      case 'new-task': loadScenario('empty'); msgInput.focus(); break;
      case 'open-folder': openFolderPicker(); break;
      case 'new-folder': openNewFolderDialog(); break;
      case 'refresh-files': loadFileTree(true); break;
      case 'reload-session': loadScenario(state.scenario); break;
      case 'quit': closeWindow(); break;
      case 'copy-handover': copyHandover(); break;
      case 'clear-chat': loadScenario('empty'); break;
      case 'focus-key': apiKeyInputChip.hidden = false; apiKeyInput.focus(); break;
      case 'theme': applyTheme(value); break;
      case 'toggle-sidebars': toggleSidebars(); break;
      case 'toggle-console': toggleConsole(); break;
      case 'toggle-statusbar': toggleStatusbar(); break;
      case 'maximize': toggleMaximized(); break;
      case 'default-workspace': switchProject('default'); break;
      case 'reveal-project':
        appendMessage('agent', `已在文件管理器中定位工作区：${currentProject().path}（原型示意）`);
        break;
      case 'send': handleSend(); break;
      case 'interrupt': handleInterrupt(); break;
      case 'resume': appendMessage('agent', '当前无中断中的任务（原型示意）。'); break;
      case 'abort': appendMessage('agent', '当前无中断中的任务（原型示意）。'); break;
      case 'council': appendMessage('agent', '评审会需在「待确认」阶段的审批卡片上发起。'); break;
      case 'shortcuts': openShortcuts(); break;
      case 'docs': openFile('docs/HANDOVER.md'); break;
      case 'about': openAbout(); break;
      default: appendMessage('agent', `ℹ️ 菜单项「${menuLabel(menuName) || ''} › ${action}」在原型中未接线。`);
    }
  }

  function toggleSidebars() {
    state.toggles.sidebars = !state.toggles.sidebars;
    document.body.classList.toggle('sidebars-hidden', !state.toggles.sidebars);
    syncViewChecks();
  }

  function toggleConsole() {
    state.toggles.console = !state.toggles.console;
    applyConsoleVisibility();
  }

  /** 演示控制台可见性 = 用户偏好 ∩ 视口够宽；可见时给 body 加 console-open，
   *  由 CSS 为窗口右侧预留桌面栏，保证浮层不压住「发送 / 中断」。 */
  function applyConsoleVisibility() {
    const effective = state.toggles.console && !narrowMQ.matches;
    demoConsole.hidden = !effective;
    document.body.classList.toggle('console-open', effective);
    syncViewChecks();
  }

  function toggleStatusbar() {
    state.toggles.statusbar = !state.toggles.statusbar;
    document.body.classList.toggle('statusbar-hidden', !state.toggles.statusbar);
    syncViewChecks();
  }

  function syncViewChecks() {
    MOCK.menus.view.forEach((item) => {
      if (item.type !== 'check') return;
      if (item.action === 'toggle-sidebars') item.checked = state.toggles.sidebars;
      if (item.action === 'toggle-console') item.checked = state.toggles.console;
      if (item.action === 'toggle-statusbar') item.checked = state.toggles.statusbar;
    });
  }

  /* ════════════════ 阶段条 / 任务 / 状态栏 ════════════════ */
  function updateStageBar(status) {
    state.status = status;
    const statusMap = MOCK.STATUS_TO_STAGE;
    let stageIdx = statusMap[status] == null ? -1 : statusMap[status];
    if (stageIdx >= 0) state.lastStageIdx = stageIdx;
    else if (status === 'interrupted') stageIdx = state.lastStageIdx;

    stageBar.innerHTML = MOCK.STAGES.map((name, i) => {
      let cls = 'stage';
      if (status === 'failed') {
        if (i === 0) cls += ' failed';
      } else if (stageIdx >= 0) {
        if (i < stageIdx) cls += ' done';
        else if (i === stageIdx) cls += ' active';
      }
      const current = cls.includes('active') ? ' aria-current="step"' : '';
      return `<li class="${cls}"${current}>${escapeHtml(name)}</li>`;
    }).join('');
    if (status === 'failed') {
      const fail = document.createElement('li');
      fail.className = 'stage failed';
      fail.textContent = '✗ 失败';
      stageBar.appendChild(fail);
    }
    interruptBtn.disabled = status !== 'executing';
    updateStatusbar();
  }

  function updateTaskChip() {
    if (!state.taskId) { taskChip.textContent = '未创建任务'; return; }
    const label = MOCK.STATUS_LABELS[state.status] || state.status;
    taskChip.textContent = `任务 ${state.taskId.slice(0, 8)} · ${label}`;
  }

  function updateStatusbar() {
    const project = currentProject();
    statusProject.textContent = `📁 ${project.name}`;
    statusProject.title = project.path;
    statusModel.textContent = modelSelect.value;
    statusTask.textContent = state.taskId
      ? `任务：${MOCK.STATUS_LABELS[state.status] || state.status}`
      : '任务：未创建';
    titlebarProjectName.textContent = project.name;
    workspacePath.textContent = `工作区 / ${project.name}`;
    workspacePath.title = project.path;
    updateTaskChip();
  }

  /* ════════════════ 对话渲染 ════════════════ */
  function mountChatNode(node) {
    chatLog.appendChild(node);
    scrollChatToBottom();
  }

  function clearChat() {
    chatLog.innerHTML = '';
    handoverSlot.innerHTML = '';
  }

  function renderMessage(role, text) {
    const wrap = document.createElement('div');
    wrap.className = role === 'user' ? 'msg-user' : 'msg-agent';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    wrap.appendChild(bubble);
    mountChatNode(wrap);
  }

  function renderToolCard(title, status, detail) {
    const card = document.createElement('div');
    card.className = 'tool-card';
    const cls = MOCK.TOOL_STATUS_LABELS[status] ? status : 'pending';
    card.innerHTML = `
      <div class="tool-card-header">
        <span class="tool-card-title">🔧 工具调用 · ${escapeHtml(title)}</span>
        <span class="tool-status ${cls}">${MOCK.TOOL_STATUS_LABELS[cls]}</span>
      </div>
      <div class="tool-card-detail">${escapeHtml(detail)}</div>
    `;
    mountChatNode(card);
    return card;
  }

  function renderApprovalCard(questions, taskId, highRiskActions) {
    const risky = (highRiskActions || []).filter(Boolean);
    const card = document.createElement('div');
    card.className = 'approval-card';
    card.dataset.taskId = taskId || state.taskId || '';
    const list = (questions || []).map((q) => `<div>• ${escapeHtml(q)}</div>`).join('');
    const prompt = risky.length
      ? ['计划已生成，是否批准执行？', `本计划含高风险步骤，批准后将授权：${escapeHtml(risky.join('、'))}`]
      : ['计划已生成，是否批准执行？'];
    card.innerHTML = `
      <div class="approval-title">⚠ 需要你的确认</div>
      <div class="approval-detail">${list}</div>
      <div class="approval-meta">${prompt.map(escapeHtml).join('<br>')}</div>
      <div class="approval-meta">确认后进入执行阶段，拒绝则返回重新规划</div>
      <div class="approval-actions">
        <button type="button" class="btn btn-approve">批准</button>
        <button type="button" class="btn btn-reject">拒绝</button>
        <button type="button" class="btn btn-council">🧑‍🤝‍🧑 召开评审会</button>
      </div>
    `;
    mountChatNode(card);
    card.querySelector('.btn-approve').addEventListener('click', () => handleApprove(card));
    card.querySelector('.btn-reject').addEventListener('click', () => handleReject(card));
    card.querySelector('.btn-council').addEventListener('click', () => handleCouncil(card));
    return card;
  }

  function renderCouncilCard(minutes) {
    const card = document.createElement('div');
    card.className = 'council-card';
    const section = (title, items) => {
      if (!items || items.length === 0) return '';
      const rows = items.map((item) =>
        `<div class="council-item"><span class="council-role">${escapeHtml(item.role)}</span>${escapeHtml(item.content)}</div>`
      ).join('');
      return `<div class="council-section"><div class="council-section-title">${title}</div>${rows}</div>`;
    };
    const participants = (minutes.participants || []).map(escapeHtml).join('、');
    const absent = (minutes.absent || []).map(escapeHtml).join('、');
    card.innerHTML = `
      <div class="council-title">🧑‍🤝‍🧑 评审会纪要</div>
      <div class="council-topic">议题：${escapeHtml(minutes.topic || '')}</div>
      <div class="council-meta">参会：${participants || '—'}${absent ? ` ｜ 缺席：${absent}` : ''} ｜ 轮数：${minutes.rounds || 1}</div>
      ${section('一致', minutes.agreements)}
      ${section('分歧', minutes.disagreements)}
      ${section('未决', minutes.unresolved)}
      <div class="council-decision">建议决议：${escapeHtml(minutes.decision_note || minutes.suggested_decision || '—')}</div>
    `;
    return card;
  }

  function renderInterruptCard(taskId) {
    const card = document.createElement('div');
    card.className = 'approval-card';
    card.dataset.taskId = taskId || state.taskId || '';
    card.innerHTML = `
      <div class="approval-title">⏸ 任务已中断</div>
      <div class="approval-meta">恢复则从断点继续执行；放弃则任务终止。</div>
      <div class="approval-actions">
        <button type="button" class="btn btn-resume">▶ 恢复</button>
        <button type="button" class="btn btn-danger">✗ 放弃</button>
      </div>
    `;
    mountChatNode(card);
    card.querySelector('.btn-resume').addEventListener('click', () => handleResume(card));
    card.querySelector('.btn-danger').addEventListener('click', () => handleAbort(card));
    return card;
  }

  function renderEntry(entry) {
    if (entry.kind === 'msg') renderMessage(entry.role, entry.text);
    else if (entry.kind === 'tool') renderToolCard(entry.title, entry.status, entry.detail);
    else if (entry.kind === 'approval') renderApprovalCard(entry.questions, entry.taskId, entry.highRiskActions);
    else if (entry.kind === 'council') mountChatNode(renderCouncilCard(entry.minutes));
    else if (entry.kind === 'interrupt') renderInterruptCard(entry.taskId);
  }

  const appendMessage = (role, text) => renderMessage(role, text);
  const appendToolCard = (title, status, detail) => renderToolCard(title, status, detail);

  /* ════════════════ 交接提示卡片 ════════════════ */
  function strengthLabel(strength) {
    return { strong: '强烈建议开新对话', suggest: '建议开新对话', none: '暂不需要交接' }[strength] || '';
  }

  function effectivePick(data) {
    const hasHuman = (data.human_open_questions || []).length > 0;
    const hasMachine = (data.machine_open_items || []).length > 0;
    if (state.handoverPick === 'human') return hasHuman ? 'human' : 'machine';
    return hasMachine ? 'machine' : 'human';
  }

  function buildHandoverCopyText(data, pick) {
    const lines = [];
    const pickLabel = pick === 'human' ? '人的未决' : '机器未决';
    const g = data.gate || {};
    lines.push(`【交接提示｜${strengthLabel(data.strength)} · 先处理：${pickLabel}】`);
    lines.push(`任务：${data.task_id} · 状态：${data.task_status}`);
    lines.push(
      `触发：对话轮次 ${g.turns} / ${g.turns_threshold}${g.turns_hit ? '（已超）' : ''} · ` +
      `累计 token ${formatNum(g.tokens)} / ${formatNum(g.tokens_threshold)}${g.tokens_hit ? '（已超）' : ''}`
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
    } else {
      const items = data.machine_open_items || [];
      if (items.length) {
        lines.push('一、机器未决');
        items.forEach((item) => lines.push(`- ${item.step_id} ${item.action} [${item.status}] ${item.detail}`));
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

  function renderHandover() {
    const data = state.handover;
    if (!data) { handoverSlot.innerHTML = ''; return; }

    if (state.handoverDismissed) {
      handoverSlot.innerHTML = `<div class="hn-card hn-none hn-dismissed">
        <span>交接提示已忽略（后端已记录 ack=ignored，本会话不再重复提示）</span>
        <button class="btn btn-ghost" type="button" data-hn-act="restore">恢复显示</button></div>`;
      return;
    }

    if (!data.should_suggest) {
      const g = data.gate || {};
      handoverSlot.innerHTML = `
        <article class="hn-card hn-none">
          <div class="hn-head">
            <span class="hn-icon" aria-hidden="true">↔</span>
            <span class="hn-title">暂不需要交接</span>
            <span class="hn-metric">轮次 ${g.turns}/${g.turns_threshold} · token ${formatNum(g.tokens)}/${formatNum(g.tokens_threshold)}</span>
          </div>
          <p class="hn-reason">${escapeHtml((data.reasons || []).join('；'))}</p>
        </article>`;
      return;
    }

    const pick = effectivePick(data);
    const g = data.gate || {};
    const humanCount = (data.human_open_questions || []).length;
    const machineCount = (data.machine_open_items || []).length;
    const copyText = buildHandoverCopyText(data, pick);
    const hasOpen = humanCount || machineCount;

    const humanRows = (data.human_open_questions || [])
      .map((item) => `<li class="hn-item"><span class="hn-src">${escapeHtml(item.source)}</span>${escapeHtml(item.text)}</li>`).join('');
    const machineRows = (data.machine_open_items || [])
      .map((item) => `<li class="hn-item"><span class="hn-step">${escapeHtml(item.step_id)} ${escapeHtml(item.action)}</span><span class="hn-status ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>${escapeHtml(item.detail)}</li>`).join('');
    const actionRows = (data.next_actions || [])
      .map((text) => `<li class="hn-item">${escapeHtml(text)}</li>`).join('');

    handoverSlot.innerHTML = `
      <article class="hn-card hn-${escapeHtml(data.strength)}">
        <div class="hn-head">
          <span class="hn-icon" aria-hidden="true">↔</span>
          <span class="hn-title">交接提示</span>
          <span class="hn-badge hn-badge-${escapeHtml(data.strength)}">${strengthLabel(data.strength)}</span>
        </div>
        <p class="hn-reason">${(data.reasons || []).map(escapeHtml).join(' · ')}</p>
        <div class="hn-gate">
          <span class="hn-gate-item${g.turns_hit ? ' hit' : ''}">对话轮次 ${g.turns} / ${g.turns_threshold}</span>
          <span class="hn-gate-item${g.tokens_hit ? ' hit' : ''}">累计 token ${formatNum(g.tokens)} / ${formatNum(g.tokens_threshold)}</span>
          <span class="hn-gate-item${g.chars_hit ? ' hit' : ''}">字符 ${formatNum(g.chars)} / ${formatNum(g.chars_threshold)}</span>
          <span class="hn-gate-item">已提示 ${g.prompts_shown} / ${g.max_prompts}</span>
        </div>
        ${hasOpen ? `
        <div class="hn-choice">
          <p class="hn-choice-note">人的未决优先级更高：它决定「新对话该先问什么」；机器未决是执行层收尾。请选择先处理哪一类——复制内容只带出你选中的那一类。</p>
          <div class="hn-choice-seg">
            <button class="seg-btn${pick === 'human' ? ' is-active' : ''}" type="button" data-hn-act="pick" data-hn-pick="human" aria-pressed="${pick === 'human'}" ${humanCount ? '' : 'disabled'}>人的未决（优先）· ${humanCount}</button>
            <button class="seg-btn${pick === 'machine' ? ' is-active' : ''}" type="button" data-hn-act="pick" data-hn-pick="machine" aria-pressed="${pick === 'machine'}" ${machineCount ? '' : 'disabled'}>机器未决 · ${machineCount}</button>
          </div>
          <ul class="hn-list">${pick === 'human' ? humanRows : machineRows}</ul>
        </div>` : ''}
        ${actionRows ? `<div class="hn-section"><div class="hn-section-title">接着做什么</div><ol class="hn-list hn-list-ordered">${actionRows}</ol></div>` : ''}
        <details class="hn-preview">
          <summary>预览「复制」内容</summary>
          <pre class="hn-pre">${escapeHtml(copyText)}</pre>
        </details>
        <div class="hn-actions-bar">
          <button class="btn btn-approve" type="button" data-hn-act="copy">复制交接提示</button>
          <button class="btn btn-outline" type="button" data-hn-act="start">一键新开对话</button>
          <button class="btn btn-ghost" type="button" data-hn-act="dismiss">暂不处理</button>
          <span class="hn-note">卡片不写入 transcript；仅后端记 ack</span>
        </div>
      </article>`;
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
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

  function copyHandover() {
    if (!state.handover) { appendMessage('agent', '当前没有可复制的交接提示。'); return; }
    copyToClipboard(buildHandoverCopyText(state.handover, effectivePick(state.handover)))
      .then(() => appendMessage('agent', '已复制交接提示到剪贴板（原型示意）。'))
      .catch(() => appendMessage('agent', '复制失败：浏览器拒绝了剪贴板访问。'));
  }

  function bindHandoverSlot() {
    handoverSlot.addEventListener('click', (event) => {
      const trigger = event.target.closest('[data-hn-act]');
      if (!trigger) return;
      const act = trigger.dataset.hnAct;
      if (act === 'pick') {
        state.handoverPick = trigger.dataset.hnPick;
        renderHandover();
      } else if (act === 'copy') {
        copyHandover();
      } else if (act === 'dismiss') {
        state.handoverDismissed = true;
        api.ackHandover(state.handover.task_id, 'ignored');
        renderHandover();
      } else if (act === 'restore') {
        state.handoverDismissed = false;
        renderHandover();
      } else if (act === 'start') {
        api.ackHandover(state.handover.task_id, 'seen');
        state.handover = null;
        loadScenario('empty');
        appendMessage('agent', '已复制交接提示并开启新对话（原型示意）。');
        msgInput.focus();
      }
    });
  }

  async function checkHandover(turns) {
    if (!state.taskId) return;
    state.handover = await api.getHandover(state.taskId, { turns: turns || 6 });
    state.handover.ack = { status: null };
    state.handoverDismissed = false;
    renderHandover();
  }

  /* ════════════════ 文件树 ════════════════ */
  function renderFileTree(nodes, container, depth) {
    (nodes || []).forEach((node) => {
      const row = document.createElement('div');
      row.className = `tree-node depth-${depth || 0}${node.type === 'dir' ? ' tree-dir' : ' tree-file'}`;
      row.title = node.path;
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      row.dataset.path = node.path;
      row.dataset.type = node.type;

      if (node.type === 'dir') {
        row.innerHTML = `<span class="tree-caret">▾</span> 📁 ${escapeHtml(node.name)}`;
        container.appendChild(row);
        const childBox = document.createElement('div');
        childBox.className = 'tree-children';
        container.appendChild(childBox);
        renderFileTree(node.children || [], childBox, (depth || 0) + 1);
        row.addEventListener('click', () => {
          const collapsed = childBox.classList.toggle('collapsed');
          row.querySelector('.tree-caret').textContent = collapsed ? '▸' : '▾';
        });
      } else {
        const sizeLabel = node.size != null ? ` <span class="tree-size">${formatSize(node.size)}</span>` : '';
        row.innerHTML = `📄 ${escapeHtml(node.name)}${sizeLabel}`;
        row.addEventListener('click', () => openFile(node.path));
        container.appendChild(row);
      }

      row.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); row.click(); }
      });
      row.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        openContextMenu(event, node);
      });
    });
  }

  async function loadFileTree(notify) {
    fileTree.innerHTML = '<div class="tree-node depth-0">⟳ 加载中…</div>';
    try {
      const data = await api.getWorkspaceFiles();
      fileTree.innerHTML = '';
      renderFileTree(data.nodes, fileTree, 0);
      if (notify) appendMessage('agent', '文件树已刷新（原型示意）。');
    } catch (err) {
      fileTree.innerHTML = '<div class="tree-node depth-0 tree-error">✗ 文件树加载失败</div>';
    }
  }

  /* ── 右键菜单 ── */
  function openContextMenu(event, node) {
    const items = node.type === 'dir'
      ? [{ action: 'open-dir', label: '展开 / 折叠' }]
      : [{ action: 'open', label: '打开' }];
    items.push({ action: 'reveal', label: '在文件管理器中显示' });
    items.push({ type: 'sep' });
    items.push({ action: 'rename', label: '重命名（示例）' });
    items.push({ action: 'delete', label: '删除（示例）', danger: true });

    contextMenu.innerHTML = items.map((item) => {
      if (item.type === 'sep') return '<div class="menu-sep"></div>';
      return `<button type="button" class="menu-item" role="menuitem" data-ctx="${item.action}">`
        + '<span class="mi-check"></span>'
        + `<span class="mi-label">${escapeHtml(item.label)}</span></button>`;
    }).join('');
    contextMenu.hidden = false;
    state.contextNode = node;
    positionPopup(contextMenu, { left: event.clientX, right: event.clientX, top: event.clientY, bottom: event.clientY, width: 0, height: 0 });
  }

  function closeContextMenu() {
    contextMenu.hidden = true;
    state.contextNode = null;
  }

  function bindContextMenu() {
    contextMenu.addEventListener('click', (event) => {
      const item = event.target.closest('[data-ctx]');
      if (!item) return;
      const node = state.contextNode;
      const action = item.dataset.ctx;
      closeContextMenu();
      if (!node) return;
      if (action === 'reveal') appendMessage('agent', `已在文件管理器中定位：${node.path}（原型示意）`);
      else if (action === 'rename') appendMessage('agent', `ℹ️ 重命名「${node.name}」在原型中未接线。`);
      else if (action === 'delete') appendMessage('agent', `ℹ️ 删除「${node.name}」为高风险操作，需审批后执行（原型未接线）。`);
      else if (action === 'open-dir') {
        const row = fileTree.querySelector(`.tree-node[data-path="${CSS.escape(node.path)}"]`);
        if (row) row.click();
      } else if (action === 'open') openFile(node.path);
    });
    document.addEventListener('click', (event) => {
      if (contextMenu.hidden) return;
      if (contextMenu.contains(event.target)) return;
      closeContextMenu();
    });
  }

  /* ════════════════ 文件查看器 ════════════════ */
  let viewerTrigger = null;

  async function openFile(path) {
    const data = await api.readFile(path);
    state.viewerPath = path;
    fileViewerPath.textContent = data.path;
    fileViewerMeta.textContent = `${formatSize(data.size)} · ${data.editable ? '可编辑' : '只读'}`;
    fileViewerBody.value = data.content;
    fileViewerBody.readOnly = !data.editable;
    fileViewerSave.hidden = !data.editable;
    viewerTrigger = document.activeElement; // 记录来源焦点，关闭时归还
    fileViewer.hidden = false;
    fileViewerBody.focus();
  }

  function closeViewer() {
    fileViewer.hidden = true;
    state.viewerPath = null;
    if (viewerTrigger && typeof viewerTrigger.focus === 'function') viewerTrigger.focus();
    viewerTrigger = null;
  }

  async function saveViewer() {
    if (!state.viewerPath) return;
    await api.writeFile(state.viewerPath, fileViewerBody.value);
    fileViewerMeta.textContent = `${formatSize(fileViewerBody.value.length)} · 已保存`;
    appendMessage('agent', `已保存文件：${state.viewerPath}`);
  }

  /* ════════════════ 模拟原生「选择文件夹」对话框 ════════════════ */
  let folderPickerResolve = null;

  function renderFolderPicker() {
    folderPickerPath.textContent = MOCK.folderPicker.path;
    folderPickerList.innerHTML = MOCK.folderPicker.folders.map((f) =>
      `<button type="button" class="native-folder${f.path === state.folderSelected ? ' is-selected' : ''}" data-path="${escapeHtml(f.path)}">`
      + `<span class="native-folder-glyph" aria-hidden="true">📁</span>${escapeHtml(f.name)}</button>`
    ).join('');
  }

  function openFolderPicker() {
    state.folderSelected = MOCK.folderPicker.selected;
    renderFolderPicker();
    folderPicker.hidden = false;
    folderPickerConfirm.focus();
    return new Promise((resolve) => { folderPickerResolve = resolve; });
  }

  function closeFolderPicker(result) {
    folderPicker.hidden = true;
    if (folderPickerResolve) { folderPickerResolve(result); folderPickerResolve = null; }
  }

  function bindFolderPicker() {
    folderPickerList.addEventListener('click', (event) => {
      const btn = event.target.closest('.native-folder');
      if (!btn) return;
      state.folderSelected = btn.dataset.path;
      renderFolderPicker();
    });
    folderPickerConfirm.addEventListener('click', () => closeFolderPicker({ cancelled: false, path: state.folderSelected }));
    folderPickerCancel.addEventListener('click', () => closeFolderPicker({ cancelled: true, path: null }));
    folderPickerX.addEventListener('click', () => closeFolderPicker({ cancelled: true, path: null }));
  }

  /* ════════════════ 项目（工作区）════════════════ */
  function renderProjects() {
    projectList.innerHTML = MOCK.projects.map((p) =>
      `<button type="button" class="project-item${p.current ? ' is-current' : ''}" data-project="${escapeHtml(p.id)}" title="${escapeHtml(p.path)}">`
      + `<span class="project-item-name">${p.current ? '● ' : ''}${escapeHtml(p.name)}</span>`
      + `<span class="project-item-path">${escapeHtml(p.path)}</span></button>`
    ).join('');
  }

  async function switchProject(projectId) {
    const data = await api.openProject(projectId);
    renderProjects();
    updateStatusbar();
    appendMessage('agent', `📁 已切换到工作区「${data.project.name}」：${data.project.path}`);
    await loadFileTree();
  }

  function bindProjectPanel() {
    projectList.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-project]');
      if (btn) switchProject(btn.dataset.project);
    });
    newProjectBtn.addEventListener('click', (event) => {
      event.stopPropagation();
      const open = projectMenu.hidden;
      projectMenu.hidden = !open;
      newProjectBtn.setAttribute('aria-expanded', String(open));
    });
    projectMenu.addEventListener('click', async (event) => {
      const item = event.target.closest('.project-menu-item');
      if (!item) return;
      projectMenu.hidden = true;
      newProjectBtn.setAttribute('aria-expanded', 'false');
      const action = item.dataset.action;
      if (action === 'open-folder') {
        const result = await openFolderPicker();
        if (result.cancelled) return;
        await registerAndEnter(result.path);
      } else if (action === 'new-folder') {
        openNewFolderDialog();
      } else if (action === 'default-workspace') {
        await switchProject('default');
      }
    });
    document.addEventListener('click', (event) => {
      if (projectMenu.hidden) return;
      if (event.target.closest('.new-project-wrap')) return;
      projectMenu.hidden = true;
      newProjectBtn.setAttribute('aria-expanded', 'false');
    });
  }

  async function registerAndEnter(path) {
    const name = path.split('\\').pop() || path;
    const data = await api.createWorkspaceFolder(path.replace(/\\[^\\]+$/, ''), name);
    renderProjects();
    updateStatusbar();
    appendMessage('agent', `📁 已切换到工作区「${data.project.name}」：${data.project.path}`);
    await loadFileTree();
  }

  /* ════════════════ 新建文件夹对话框 ════════════════ */
  async function openNewFolderDialog() {
    const result = await openFolderPicker();
    if (result.cancelled) return;
    state.folderParent = result.path;
    newFolderParent.textContent = `父目录：${result.path}`;
    newFolderName.value = '';
    newFolderDialog.hidden = false;
    newFolderName.focus();
  }

  function closeNewFolderDialog() {
    newFolderDialog.hidden = true;
    newProjectBtn.focus();
  }

  async function confirmNewFolder() {
    const name = newFolderName.value.trim();
    if (!name) { newFolderName.focus(); return; }
    const data = await api.createWorkspaceFolder(state.folderParent, name);
    closeNewFolderDialog();
    renderProjects();
    updateStatusbar();
    appendMessage('agent', `📁 已切换到工作区「${data.project.name}」：${data.project.path}`);
    await loadFileTree();
  }

  /* ════════════════ 关于 / 快捷键 ════════════════ */
  function openAbout() { aboutDialog.hidden = false; aboutClose.focus(); }
  function closeAbout() { aboutDialog.hidden = true; }

  function openShortcuts() {
    const rows = MOCK.shortcuts.map(([k, v]) => `${k.padEnd(12, ' ')} ${v}`).join('\n');
    appendMessage('agent', `键盘快捷键：\n${rows}`);
  }

  /* ════════════════ 场景装载 ════════════════ */
  function loadScenario(name) {
    const scenario = MOCK.scenarios[name] || MOCK.scenarios.delivered;
    state.scenario = name;
    state.taskId = scenario.taskId;
    state.lastStageIdx = scenario.lastStageIdx != null
      ? scenario.lastStageIdx
      : (MOCK.STATUS_TO_STAGE[scenario.status] >= 0 ? MOCK.STATUS_TO_STAGE[scenario.status] : 0);
    clearChat();
    scenario.entries.forEach(renderEntry);
    state.handover = scenario.handover ? { ...scenario.handover, gate: { ...scenario.handover.gate } } : null;
    state.handoverDismissed = false;
    state.handoverPick = 'human';
    renderHandover();

    // 先设状态，再刷新阶段条（updateStageBar 内部会读 lastStageIdx）
    state.status = 'received';
    updateStageBar(scenario.status);
    updateStatusbar();

    document.querySelectorAll('#scenarioRow .demo-chip').forEach((chip) => {
      chip.classList.toggle('is-active', chip.dataset.scenario === name);
    });

    scrollChatToBottom();
  }

  /* ════════════════ 交互流程（脚本化）════════════════ */
  async function handleSend() {
    const text = msgInput.value.trim();
    if (!text || state.isSending) return;
    state.isSending = true;
    sendBtn.disabled = true;

    clearChat();
    state.handover = null;
    renderHandover();
    appendMessage('user', text);
    msgInput.value = '';
    autoGrowInput();

    const task = await api.createTask();
    state.taskId = task.task_id;
    updateStageBar('planning');
    appendMessage('agent', `已接收需求，任务 ID：${task.task_id.slice(0, 8)}… 正在分解步骤…`);

    const plan = await api.planTask();
    appendToolCard('searcher · 检索仓库既有实现', 'success', '命中 docs/HANDOVER.md 第 9 节、agent_builder/api/handover.py');
    appendToolCard('web_fetch · 参考同类产品做法', 'success', 'HTTP 200 · 抓取 3 篇，摘要约 1.2k 字');
    appendMessage('agent', `分解完成，发现 ${plan.questions.length} 个待确认点：`);
    renderApprovalCard(plan.questions, task.task_id, plan.high_risk_actions);
    updateStageBar('awaiting_confirm');

    state.isSending = false;
    sendBtn.disabled = false;
  }

  async function handleApprove(card) {
    card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    updateStageBar('executing');
    appendMessage('agent', '计划已批准，进入执行阶段…');

    await api.approveTask();
    appendToolCard('file_write · 新建 agent_builder/api/handover.py', 'success', '写入 4.3 KB，overwrite=true');
    appendToolCard('file_write · 前端交接卡片渲染', 'success', 'frontend/js/app.js +180 行');
    appendToolCard('file_delete · 移除临时草稿', 'success', '删除 docs/_draft-handover.tmp');
    const running = appendToolCard('test_run · pytest tests/test_api_handover.py', 'running', '已运行 6 / 23 例…');
    appendMessage('agent', '执行进度：成功 3 / 进行中 1 / 待执行 1 · 共 5 个步骤。');

    await delay(700);
    running.remove();
    appendToolCard('test_run · pytest tests/test_api_handover.py', 'success', '23 passed');
    appendMessage('agent', '所有 5 个步骤执行完成（成功 5），进入验证阶段。');

    updateStageBar('verifying');
    await delay(600);
    appendMessage('agent', '验证通过：计划与执行结果一致，未发现一致性问题。');
    updateStageBar('delivering');
    await delay(500);
    appendMessage('agent', '交付完成：新增 handover 判定模块与前端卡片，测试全绿；建议开一轮新对话继续「摘要继承」。');
    updateStageBar('delivered');
    checkHandover(7);
  }

  async function handleReject(card) {
    card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    await api.rejectTask();
    appendMessage('agent', '计划已拒绝，返回重新规划…');
    updateStageBar('planning');
  }

  async function handleCouncil(card) {
    // 闸门：一次会议未结束前忽略重复触发（避免同一张确认卡上堆叠多份纪要）
    if (card.dataset.councilBusy === '1') return;
    card.dataset.councilBusy = '1';
    const button = card.querySelector('.btn-council');
    if (button) button.disabled = true;

    try {
      // 进度消息也插在确认卡之前（消息若 append 到末尾，会把确认入口顶出视口）
      const pending = document.createElement('div');
      pending.className = 'msg-agent';
      pending.innerHTML = '<div class="bubble">评审会已发起，多个角色正在独立表态…</div>';
      card.parentNode.insertBefore(pending, card);
      scrollChatToBottom();

      const minutes = await api.runCouncil();
      pending.querySelector('.bubble').textContent =
        `评审会完成：${(minutes.participants || []).length} 个角色参会，已生成纪要。`;

      // 纪要插到确认卡「之前」：让「批准 / 拒绝」始终位于最下方，
      // 否则纪要卡片会把确认入口顶出视口（用户会以为没有确认按钮、无法确认）。
      card.parentNode.insertBefore(renderCouncilCard(minutes), card);

      // 把会议结论写回确认卡（实现「chair 汇总 + 用户拍板」）。
      // 用「按标记更新」而非追加：重复开会时原地改写，不在同一张卡上堆叠。
      const decisionLine = upsertCouncilLine(card, '.council-decision-line', 'approval-detail',
        () => card.querySelector('.approval-detail'), 'after');
      decisionLine.textContent = `评审会建议决议：${minutes.decision_note || minutes.suggested_decision || '—'}`;

      const noteLine = upsertCouncilLine(card, '.council-note-line', 'approval-meta',
        () => card.querySelector('.approval-actions'), 'before');
      noteLine.textContent = '已召开评审会（纪要见上方），请据此拍板：';

      scrollChatToBottom();
    } finally {
      card.dataset.councilBusy = '0';
      if (button) button.disabled = false;
    }
  }

  /** 取（或首次创建）确认卡上的评审会结果行，避免重复开会时重复追加。 */
  function upsertCouncilLine(card, markerClass, baseClass, anchorFn, position) {
    let line = card.querySelector(markerClass);
    if (line) return line;
    line = document.createElement('div');
    line.className = `${baseClass} ${markerClass.slice(1)}`;
    const anchor = anchorFn();
    if (position === 'after') anchor.after(line);
    else card.insertBefore(line, anchor);
    return line;
  }

  async function handleInterrupt() {
    if (state.status !== 'executing') {
      appendMessage('agent', `当前状态「${MOCK.STATUS_LABELS[state.status] || state.status}」不支持中断，仅执行阶段可中断。`);
      return;
    }
    await api.interruptTask();
    updateStageBar('interrupted');
    appendMessage('agent', '任务已中断，可恢复继续执行或放弃任务。');
    renderInterruptCard(state.taskId);
    checkHandover(6);
  }

  async function handleResume(card) {
    card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    await api.resumeTask();
    appendMessage('agent', '任务已恢复，继续执行…');
    updateStageBar('executing');
  }

  async function handleAbort(card) {
    card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    await api.abortTask();
    appendMessage('agent', '任务已放弃。');
    updateStageBar('failed');
  }

  /* ════════════════ 底部设置 ════════════════ */
  function autoGrowInput() {
    msgInput.style.height = 'auto';
    msgInput.style.height = `${Math.min(msgInput.scrollHeight, 132)}px`;
  }

  function renderApiKeyConfigured(configured) {
    apiKeyInputChip.hidden = configured;
    apiKeyStatusChip.hidden = !configured;
    statusKey.textContent = configured ? '⚿ 已配置密钥' : '⚿ 未配置密钥';
  }

  function bindSettings() {
    modelSelect.addEventListener('change', updateStatusbar);
    tempSlider.addEventListener('input', () => { tempValue.textContent = tempSlider.value; });

    msgInput.addEventListener('input', autoGrowInput);
    msgInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); handleSend(); }
    });
    sendBtn.addEventListener('click', handleSend);
    interruptBtn.addEventListener('click', handleInterrupt);
    fileRefreshBtn.addEventListener('click', () => loadFileTree(true));

    apiKeySave.addEventListener('click', () => {
      const value = apiKeyInput.value.trim();
      if (!value) { apiKeyInput.focus(); return; }
      apiKeyMasked.textContent = `${value.slice(0, 3)}••••${value.slice(-2)}`;
      apiKeyStateText.textContent = '已保存';
      apiKeyExpiry.textContent = '';
      apiKeyInput.value = '';
      renderApiKeyConfigured(true);
      appendMessage('agent', 'API 密钥已保存到后端（仅驻留内存，不回显）。如需移除可点击「删除密钥」。');
    });
    apiKeyRotate.addEventListener('click', () => {
      renderApiKeyConfigured(false);
      apiKeyInput.focus();
      appendMessage('agent', '请输入新密钥并点「保存」完成轮换；旧密钥将被替换，服务不中断。');
    });
    apiKeyDelete.addEventListener('click', () => {
      renderApiKeyConfigured(false);
      appendMessage('agent', 'API 密钥已删除。');
    });
    statusKey.addEventListener('click', () => { renderApiKeyConfigured(false); apiKeyInput.focus(); });

    statusProject.addEventListener('click', (event) => {
      event.stopPropagation();
      openMenu('project', titlebarProject);
    });
    statusModel.addEventListener('click', () => modelSelect.focus());
    statusTask.addEventListener('click', () => {
      if (state.taskId) appendMessage('agent', `当前任务 ${state.taskId} · 状态「${MOCK.STATUS_LABELS[state.status] || state.status}」。`);
      else appendMessage('agent', '当前无任务，输入需求后按「发送 ➤」开始。');
    });
  }

  /* ════════════════ 演示控制台 ════════════════ */
  function bindDemoConsole() {
    document.querySelectorAll('#scenarioRow .demo-chip').forEach((chip) => {
      chip.addEventListener('click', () => loadScenario(chip.dataset.scenario));
    });
    document.querySelectorAll('#themeRow .demo-chip').forEach((chip) => {
      chip.addEventListener('click', () => applyTheme(chip.dataset.theme));
    });
    document.querySelectorAll('[data-win]').forEach((chip) => {
      chip.addEventListener('click', () => {
        const act = chip.dataset.win;
        if (act === 'minimize') minimizeWindow();
        else if (act === 'maximize') setWindowMaximized(true);
        else if (act === 'close') closeWindow();
        else if (act === 'restore') { restoreWindow(); setWindowMaximized(false); }
      });
    });
    demoConsoleClose.addEventListener('click', toggleConsole);
  }

  /* ════════════════ 全局键盘 ════════════════ */
  function bindKeyboard() {
    document.addEventListener('keydown', (event) => {
      const ctrl = event.ctrlKey || event.metaKey;

      if (event.key === 'Escape') {
        if (!contextMenu.hidden) { closeContextMenu(); return; }
        if (!folderPicker.hidden) { closeFolderPicker({ cancelled: true, path: null }); return; }
        if (!newFolderDialog.hidden) { closeNewFolderDialog(); return; }
        if (!aboutDialog.hidden) { closeAbout(); return; }
        if (!fileViewer.hidden) { closeViewer(); return; }
        if (state.openMenu) { closeMenu(); return; }
      }
      if (ctrl && event.key.toLowerCase() === 'n') { event.preventDefault(); loadScenario('empty'); msgInput.focus(); return; }
      if (ctrl && event.key.toLowerCase() === 'k') { event.preventDefault(); openFolderPicker(); return; }
      if (ctrl && event.key.toLowerCase() === 'b') { event.preventDefault(); toggleSidebars(); return; }
      if (ctrl && event.key === ',') { event.preventDefault(); renderApiKeyConfigured(false); apiKeyInput.focus(); return; }
      if (ctrl && event.key === 'Enter') { event.preventDefault(); handleSend(); return; }
      if (ctrl && event.key === '.') { event.preventDefault(); handleInterrupt(); return; }
      if (event.key === 'F5') { event.preventDefault(); loadFileTree(true); return; }
      if (event.key === 'F11') { event.preventDefault(); toggleMaximized(); return; }
      if (event.key === 'F10') { event.preventDefault(); openMenu('file', findMenuRoot('file')); }
    });
  }

  /* ════════════════ 初始化 ════════════════ */
  function bindDialogs() {
    fileViewer.querySelectorAll('[data-close]').forEach((el) => el.addEventListener('click', closeViewer));
    fileViewerClose.addEventListener('click', closeViewer);
    fileViewerSave.addEventListener('click', saveViewer);

    newFolderCancel.addEventListener('click', closeNewFolderDialog);
    newFolderConfirm.addEventListener('click', confirmNewFolder);
    newFolderDialog.querySelectorAll('[data-close]').forEach((el) => el.addEventListener('click', closeNewFolderDialog));
    newFolderName.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); confirmNewFolder(); }
    });

    aboutDialog.querySelectorAll('[data-close]').forEach((el) => el.addEventListener('click', closeAbout));
    aboutClose.addEventListener('click', closeAbout);

    syncViewChecks();
    bindWindowChrome();
    bindMenubar();
    bindContextMenu();
    bindFolderPicker();
    bindProjectPanel();
    bindHandoverSlot();
    bindSettings();
    bindDemoConsole();
    bindKeyboard();
    if (typeof narrowMQ.addEventListener === 'function') {
      narrowMQ.addEventListener('change', applyConsoleVisibility);
    }
  }

  function startClock() {
    const tick = () => {
      const now = new Date();
      trayClock.textContent = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
    };
    tick();
    setInterval(tick, 30000);
  }

  function init() {
    aboutVersion.textContent = MOCK.version;
    bindDialogs();

    let saved = 'auto';
    try { saved = sessionStorage.getItem('agent-builder-desktop:theme') || 'auto'; } catch (err) { saved = 'auto'; }
    applyTheme(saved);

    renderProjects();
    loadFileTree();
    loadScenario('empty');
    startClock();
    renderApiKeyConfigured(false);
    applyConsoleVisibility();
  }

  init();
})();
