/* ============================================================
 * app.js —— 状态 + 视图 + 交互（state / data / view 分离：数据在 mock.js，接口在 api.js）
 *
 * 交互：场景切换 / 任务状态 / 对话轮次与累计 token / 已提示次数 / 未决类别选择 /
 *       复制交接提示 / 一键新开对话 / 暂不处理（可恢复）/ 失败重试 / 主题
 *
 * 两条硬约束：
 *   1. 卡片内容全部来自 api.fetchHandover() 的结构化交接对象，前端不自行判定；
 *   2. 交接卡片**不写入 transcript**（只有主题偏好写 localStorage），刷新即消失，
 *      「已查看 / 已忽略」由后端 ack 记录，用于限次与去重。
 * ============================================================ */

(() => {
  'use strict';

  const SLOT_ID = 'handoverSlot';
  const THEME_KEY = 'handover-card:theme'; // 仅主题偏好；卡片本身不持久化

  const ICONS = {
    handover:
      '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 5.5h9l-2.5-2.5M14 10.5H5l2.5 2.5"/></svg>',
    copy: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V4a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5"/></svg>',
    warn: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.5 14.5 13.5h-13z"/><path d="M8 6.5v3M8 11.8h.01"/></svg>',
    clock:
      '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="5.5"/><path d="M8 5v3.2l2 1.3"/></svg>',
    info: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="5.5"/><path d="M8 7.2v3.4M8 5.2h.01"/></svg>',
    plus: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3.5v9M3.5 8h9"/></svg>',
  };

  // ── 状态（唯一可变源；UI 只读它渲染）──
  const state = {
    scenario: 'strong',
    status: 'executing',
    fail: false,
    turns: MOCK.scenarios.strong.metric.turns,
    contextTokens: MOCK.scenarios.strong.metric.context_tokens,
    contextChars: MOCK.scenarios.strong.metric.context_chars,
    promptsShown: 0,
    pick: 'human', // 用户选择先处理哪一类未决（人的未决优先）
    dismissed: false,
    started: false,
    loading: false,
    error: null,
    data: null,
  };

  // ── 工具 ──
  function fmtNum(n) {
    return Number(n || 0).toLocaleString('zh-CN');
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function strengthLabel(strength) {
    return { strong: '强烈建议开新对话', suggest: '建议开新对话', none: '暂不需要交接' }[strength] || '';
  }

  /** 生效的选择：人手为空则落到机器，反之亦然（避免选中一个空类别）。 */
  function effectivePick(data) {
    const hasHuman = data.human_open_questions.length > 0;
    const hasMachine = data.machine_open_items.length > 0;
    if (state.pick === 'human') return hasHuman ? 'human' : 'machine';
    return hasMachine ? 'machine' : 'human';
  }

  /** 组装「复制到新对话首条消息」的文本（结构化对象的纯文本投影）。
   *  只带出用户选中的那一类未决；另一类只留一行数量提示。 */
  function buildCopyText(data, pick) {
    const lines = [];
    const pickLabel = pick === 'human' ? '人的未决' : '机器未决';
    const g = data.gate;
    lines.push(`【交接提示｜${strengthLabel(data.strength)} · 先处理：${pickLabel}】`);
    lines.push(`任务：${MOCK.task.taskId} · ${MOCK.task.requirement}`);
    lines.push(`任务状态：${MOCK.statuses[data.task_status] || data.task_status}`);
    lines.push(
      `触发：对话轮次 ${g.turns} / ${g.turns_threshold}${g.turns_hit ? '（已超）' : ''} · ` +
        `累计 token ${fmtNum(g.tokens)} / ${fmtNum(g.tokens_threshold)}${g.tokens_hit ? '（已超）' : ''}`
    );
    lines.push(`判定：${data.reasons.join('；')}`);
    lines.push('');

    const ordinals = ['一', '二', '三'];
    let section = 0;
    if (pick === 'human') {
      if (data.human_open_questions.length) {
        lines.push(`${ordinals[section++]}、人的未决（优先）`);
        data.human_open_questions.forEach((item, i) => {
          lines.push(`${i + 1}. ${item.text}（来源：${item.source}）`);
        });
        lines.push('');
      }
      if (data.machine_open_items.length) {
        lines.push(`（另有机器未决 ${data.machine_open_items.length} 项未选，可在卡片中切换后再次复制）`);
        lines.push('');
      }
    } else {
      if (data.machine_open_items.length) {
        lines.push(`${ordinals[section++]}、机器未决`);
        data.machine_open_items.forEach((item) => {
          lines.push(`- ${item.step_id} ${item.action} [${item.status}] ${item.detail}`);
        });
        lines.push('');
      }
      if (data.human_open_questions.length) {
        lines.push(
          `（另有人的未决 ${data.human_open_questions.length} 项未选——注意：人的未决优先级更高）`
        );
        lines.push('');
      }
    }
    if (data.next_actions.length) {
      lines.push(`${ordinals[section++]}、建议下一步`);
      data.next_actions.forEach((text, i) => {
        lines.push(`${i + 1}. ${text}`);
      });
      lines.push('');
    }
    lines.push('（本提示由本地规则式判定生成，未调用模型；粘贴到新对话首条消息即可接续。）');
    return lines.join('\n');
  }

  // ── 视图 ──
  function renderSkeleton() {
    return `
      <article class="hn-card hn-skeleton" aria-busy="true" aria-label="正在请求后端判定">
        <div class="hn-head">
          <span class="hn-icon">${ICONS.clock}</span>
          <span class="sk sk-title"></span>
          <span class="sk sk-badge"></span>
        </div>
        <span class="sk sk-line"></span>
        <span class="sk sk-line short"></span>
      </article>`;
  }

  function renderError(err) {
    const message = err && err.message ? err.message : String(err);
    return `
      <article class="hn-card hn-error" role="alert">
        <div class="hn-head">
          <span class="hn-icon hn-icon-danger">${ICONS.warn}</span>
          <h2 class="hn-title">交接判定获取失败</h2>
        </div>
        <p class="hn-reason">${escapeHtml(message)}</p>
        <p class="hn-hint">后端返回失败时前端不做本地兜底判定（避免与后端结论不一致）。</p>
        <div class="hn-actions-bar">
          <button class="btn btn-primary" data-act="retry" type="button">重试</button>
        </div>
      </article>`;
  }

  function renderDismissed() {
    return `
      <div class="hn-dismissed">
        <span>交接提示已忽略（后端已记录 ack=ignored，本会话不再重复提示）</span>
        <button class="hn-link" data-act="restore" type="button">恢复显示</button>
      </div>`;
  }

  function renderStarted() {
    return `
      <div class="hn-started">
        <span class="hn-icon">${ICONS.handover}</span>
        <span>已开启新对话（原型模拟）：交接提示已复制并写入新对话首条消息，卡片已收起、未写入 transcript。</span>
        <button class="hn-link" data-act="back" type="button">返回当前对话</button>
      </div>`;
  }

  function renderNone(data) {
    const g = data.gate;
    return `
      <article class="hn-card hn-none">
        <div class="hn-head">
          <span class="hn-icon">${ICONS.handover}</span>
          <h2 class="hn-title">暂不需要交接</h2>
          <span class="hn-metric">轮次 ${g.turns}/${g.turns_threshold} · token ${fmtNum(g.tokens)}/${fmtNum(g.tokens_threshold)}</span>
        </div>
        <p class="hn-reason">${escapeHtml(data.reasons.join('；'))}</p>
      </article>`;
  }

  function renderGate(data) {
    const g = data.gate;
    const ackLabel = MOCK.ackStates[data.ack.status] || '';
    return `
      <div class="hn-gate">
        <span class="hn-gate-item${g.turns_hit ? ' hit' : ''}">对话轮次 ${g.turns} / ${g.turns_threshold}</span>
        <span class="hn-gate-item${g.tokens_hit ? ' hit' : ''}">累计 token ${fmtNum(g.tokens)} / ${fmtNum(g.tokens_threshold)}</span>
        <span class="hn-gate-item">已提示 ${g.prompts_shown} / ${g.max_prompts}</span>
        ${ackLabel ? `<span class="hn-ack hn-ack-${escapeHtml(data.ack.status)}">${ackLabel}</span>` : ''}
      </div>`;
  }

  /** 未决项选择区：提示「人的未决优先级更高」，由用户选择先处理哪一类。 */
  function renderPicker(data) {
    const humanCount = data.human_open_questions.length;
    const machineCount = data.machine_open_items.length;
    if (!humanCount && !machineCount) return '';
    const pick = effectivePick(data);

    const humanRows = data.human_open_questions
      .map(
        (item) => `
          <li class="hn-item">
            <span class="hn-src">${escapeHtml(item.source)}</span>
            <span class="hn-text">${escapeHtml(item.text)}</span>
          </li>`
      )
      .join('');
    const machineRows = data.machine_open_items
      .map(
        (item) => `
          <li class="hn-item">
            <span class="hn-step">${escapeHtml(item.step_id)} ${escapeHtml(item.action)}</span>
            <span class="hn-status ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
            <span class="hn-text">${escapeHtml(item.detail)}</span>
          </li>`
      )
      .join('');

    return `
      <div class="hn-choice">
        <p class="hn-choice-note">
          <span class="hn-icon">${ICONS.info}</span>
          <span>人的未决优先级更高：它决定「新对话该先问什么」；机器未决是执行层的收尾。请选择先处理哪一类——复制内容只带出你选中的那一类。</span>
        </p>
        <div class="seg hn-choice-seg" role="group" aria-label="选择先处理哪一类未决">
          <button class="seg-btn${pick === 'human' ? ' is-active' : ''}" type="button"
                  data-act="pick" data-pick="human" aria-pressed="${pick === 'human'}"
                  ${humanCount ? '' : 'disabled'}>人的未决（优先）· ${humanCount}</button>
          <button class="seg-btn${pick === 'machine' ? ' is-active' : ''}" type="button"
                  data-act="pick" data-pick="machine" aria-pressed="${pick === 'machine'}"
                  ${machineCount ? '' : 'disabled'}>机器未决 · ${machineCount}</button>
        </div>
        <ul class="hn-list">${pick === 'human' ? humanRows : machineRows}</ul>
      </div>`;
  }

  function nextActionsSection(items) {
    if (!items.length) return '';
    const rows = items
      .map((text) => `<li class="hn-item"><span class="hn-text">${escapeHtml(text)}</span></li>`)
      .join('');
    return `
      <section class="hn-section">
        <h3 class="hn-section-title">接着做什么</h3>
        <ol class="hn-list hn-list-ordered">${rows}</ol>
      </section>`;
  }

  function renderCard(data) {
    const copyText = buildCopyText(data, effectivePick(data));
    return `
      <article class="hn-card hn-${escapeHtml(data.strength)}" aria-labelledby="hnTitle">
        <div class="hn-head">
          <span class="hn-icon">${ICONS.handover}</span>
          <h2 class="hn-title" id="hnTitle">交接提示</h2>
          <span class="hn-badge hn-badge-${escapeHtml(data.strength)}">${strengthLabel(data.strength)}</span>
        </div>

        <p class="hn-reason">${data.reasons.map(escapeHtml).join(' · ')}</p>
        ${renderGate(data)}
        <p class="hn-hint">
          门控：对话轮次 &gt; ${data.gate.turns_threshold} 或累计 token ≥ ${fmtNum(data.gate.tokens_threshold)}
          （${MOCK.thresholds.turnsEnv} / ${MOCK.thresholds.tokensEnv}）· 任务状态 ${escapeHtml(data.task_status)}
          · 字符口径 ${fmtNum(data.gate.chars)} / ${fmtNum(data.gate.chars_threshold)} 仅展示（是否参与门控待确认）
        </p>

        ${renderPicker(data)}
        ${nextActionsSection(data.next_actions)}

        <details class="hn-preview">
          <summary>预览「复制」内容</summary>
          <pre class="hn-pre">${escapeHtml(copyText)}</pre>
        </details>

        <div class="hn-actions-bar">
          <button class="btn btn-primary" data-act="copy" type="button">
            <span class="btn-ico">${ICONS.copy}</span><span class="btn-label">复制交接提示</span>
          </button>
          <button class="btn btn-outline" data-act="start" type="button">
            <span class="btn-ico">${ICONS.plus}</span><span>一键新开对话</span>
          </button>
          <button class="btn btn-ghost" data-act="dismiss" type="button">暂不处理</button>
          <span class="hn-note">卡片不写入 transcript；仅后端记 ack</span>
        </div>
      </article>`;
  }

  function render() {
    const slot = document.getElementById(SLOT_ID);
    if (!slot) return;
    if (state.loading) {
      slot.innerHTML = renderSkeleton();
      return;
    }
    if (state.error) {
      slot.innerHTML = renderError(state.error);
      return;
    }
    if (state.started) {
      slot.innerHTML = renderStarted();
      return;
    }
    if (state.dismissed) {
      slot.innerHTML = renderDismissed();
      return;
    }
    if (!state.data) {
      slot.innerHTML = '';
      return;
    }
    slot.innerHTML = state.data.should_suggest ? renderCard(state.data) : renderNone(state.data);
  }

  // ── 数据 ──
  async function load() {
    state.loading = true;
    state.error = null;
    render();
    Object.assign(api.__draft, {
      scenario: state.scenario,
      status: state.status,
      promptsShown: state.promptsShown,
      fail: state.fail,
    });
    try {
      const data = await api.fetchHandover(MOCK.task.taskId, {
        turns: state.turns,
        contextTokens: state.contextTokens,
        contextChars: state.contextChars,
      });
      state.data = data;
      state.error = null;
      // 默认选「人的未决」（优先）；该类为空则落到机器未决。
      state.pick = data.human_open_questions.length ? 'human' : 'machine';
    } catch (err) {
      state.data = null;
      state.error = err;
    } finally {
      state.loading = false;
      render();
    }
  }

  // ── 交互 ──
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // file:// 下剪贴板 API 可能不可用 → 退化方案
    return new Promise((resolve, reject) => {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', 'readonly');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand('copy');
        resolve();
      } catch (err) {
        reject(err);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  /** 只更新「已查看 / 已忽略」标记，避免复制后整体重绘打断按钮反馈。 */
  function updateAckChip(status) {
    const container = document.querySelector('.hn-gate');
    if (!container) return;
    let chip = container.querySelector('.hn-ack');
    const label = MOCK.ackStates[status] || '';
    if (!label) {
      if (chip) chip.remove();
      return;
    }
    if (!chip) {
      chip = document.createElement('span');
      container.appendChild(chip);
    }
    chip.className = 'hn-ack hn-ack-' + status;
    chip.textContent = label;
  }

  function toast(message) {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => el.classList.remove('show'), 2000);
  }

  function bindSlot() {
    const slot = document.getElementById(SLOT_ID);
    if (!slot) return;
    slot.addEventListener('click', (event) => {
      const trigger = event.target.closest('[data-act]');
      if (!trigger) return;
      const act = trigger.getAttribute('data-act');

      if (act === 'retry') {
        load();
      } else if (act === 'pick') {
        state.pick = trigger.getAttribute('data-pick');
        render();
      } else if (act === 'dismiss') {
        // 记录「已忽略」：后端 ack + 本会话不再重复提示
        api.ackHandover(MOCK.task.taskId, 'ignored').then(() => {
          state.dismissed = true;
          render();
          toast('已记录「已忽略」，本会话不再重复提示');
        });
      } else if (act === 'restore') {
        // 原型专用：清除 ack 以恢复提示（真实接口可能需要 DELETE /handover/ack）
        api.__draft.ack = 'none';
        state.dismissed = false;
        load();
      } else if (act === 'copy') {
        const text = buildCopyText(state.data, effectivePick(state.data));
        copyText(text).then(
          () => {
            const label = trigger.querySelector('.btn-label');
            if (label) label.textContent = '已复制';
            trigger.classList.add('is-done');
            setTimeout(() => {
              if (label) label.textContent = '复制交接提示';
              trigger.classList.remove('is-done');
            }, 1600);
            // 记录「已查看」（后端 ack，用于限次与去重）；只更新 ack 标记，不整体重绘
            api.ackHandover(MOCK.task.taskId, 'seen').then(() => updateAckChip('seen'));
            toast('交接提示已复制，可粘贴到新对话首条消息');
          },
          () => toast('复制失败，请手动选中预览内容复制')
        );
      } else if (act === 'start') {
        const text = buildCopyText(state.data, effectivePick(state.data));
        copyText(text).catch(() => {});
        api.ackHandover(MOCK.task.taskId, 'seen').then(() => {
          state.started = true;
          render();
          toast('已开启新对话（原型模拟）：交接提示已复制到新对话首条消息');
        });
      } else if (act === 'back') {
        state.started = false;
        render();
      }
    });
  }

  function setPressed(container, attr, value) {
    container.querySelectorAll('[data-' + attr + ']').forEach((btn) => {
      const on = btn.getAttribute('data-' + attr) === value;
      btn.classList.toggle('is-active', on);
      btn.setAttribute('aria-pressed', String(on));
    });
  }

  function bindConsole() {
    const scenarioBar = document.getElementById('scenarioBar');
    const statusBar = document.getElementById('statusBar');
    const turnsSlider = document.getElementById('turnsSlider');
    const turnsValue = document.getElementById('turnsValue');
    const tokensSlider = document.getElementById('tokensSlider');
    const tokensValue = document.getElementById('tokensValue');
    const promptsSlider = document.getElementById('promptsSlider');
    const promptsValue = document.getElementById('promptsValue');

    scenarioBar.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-scenario]');
      if (!btn) return;
      state.scenario = btn.getAttribute('data-scenario');
      state.dismissed = false;
      const preset = MOCK.scenarios[state.scenario].metric;
      state.turns = preset.turns;
      state.contextTokens = preset.context_tokens;
      state.contextChars = preset.context_chars;
      turnsSlider.value = String(state.turns);
      turnsValue.textContent = String(state.turns);
      tokensSlider.value = String(state.contextTokens);
      tokensValue.textContent = fmtNum(state.contextTokens);
      setPressed(scenarioBar, 'scenario', state.scenario);
      load();
    });

    statusBar.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-status]');
      if (!btn) return;
      state.status = btn.getAttribute('data-status');
      setPressed(statusBar, 'status', state.status);
      load();
    });

    // 三个数值控件：拖动时只更新读数（回车/松手才向「后端」重新判定）
    turnsSlider.addEventListener('input', () => {
      state.turns = Number(turnsSlider.value);
      turnsValue.textContent = String(state.turns);
    });
    turnsSlider.addEventListener('change', () => {
      state.dismissed = false;
      load();
    });
    tokensSlider.addEventListener('input', () => {
      state.contextTokens = Number(tokensSlider.value);
      tokensValue.textContent = fmtNum(state.contextTokens);
    });
    tokensSlider.addEventListener('change', () => {
      state.dismissed = false;
      load();
    });
    promptsSlider.addEventListener('input', () => {
      state.promptsShown = Number(promptsSlider.value);
      promptsValue.textContent = String(state.promptsShown);
    });
    promptsSlider.addEventListener('change', () => {
      state.dismissed = false;
      load();
    });

    document.getElementById('failToggle').addEventListener('change', (event) => {
      state.fail = event.target.checked;
      load();
    });

    document.getElementById('themeBar').addEventListener('click', (event) => {
      const btn = event.target.closest('[data-theme-mode]');
      if (!btn) return;
      applyTheme(btn.getAttribute('data-theme-mode'));
    });
  }

  // ── 主题（原型控制台的演示开关；产品本身跟随系统）──
  function resolveTheme(mode) {
    if (mode === 'light' || mode === 'dark') return mode;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light';
  }

  function applyTheme(mode) {
    document.documentElement.setAttribute('data-theme', resolveTheme(mode));
    setPressed(document.getElementById('themeBar'), 'theme-mode', mode);
    try {
      localStorage.setItem(THEME_KEY, mode);
    } catch (err) {
      /* 忽略：隐私模式下不可写 */
    }
  }

  function initTheme() {
    let mode = 'system';
    try {
      mode = localStorage.getItem(THEME_KEY) || 'system';
    } catch (err) {
      mode = 'system';
    }
    applyTheme(mode);
    if (window.matchMedia) {
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        let current = 'system';
        try {
          current = localStorage.getItem(THEME_KEY) || 'system';
        } catch (err) {
          current = 'system';
        }
        if (current === 'system') applyTheme('system');
      });
    }
  }

  // ── 初始化 ──
  document.addEventListener('DOMContentLoaded', () => {
    bindSlot();
    bindConsole();
    initTheme();
    setPressed(document.getElementById('scenarioBar'), 'scenario', state.scenario);
    setPressed(document.getElementById('statusBar'), 'status', state.status);
    document.getElementById('turnsSlider').value = String(state.turns);
    document.getElementById('turnsValue').textContent = String(state.turns);
    document.getElementById('tokensSlider').value = String(state.contextTokens);
    document.getElementById('tokensValue').textContent = fmtNum(state.contextTokens);
    document.getElementById('promptsSlider').value = String(state.promptsShown);
    document.getElementById('promptsValue').textContent = String(state.promptsShown);
    load();
  });
})();
