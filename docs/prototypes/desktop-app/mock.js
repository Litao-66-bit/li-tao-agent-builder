/* ============================================================
 * Agent Builder · 桌面版原型 —— 假数据（mock）
 * 全部为演示用假数据，不含真实业务逻辑与真实路径。
 * 全局导出：window.MOCK
 * ============================================================ */
(function () {
  'use strict';

  const STAGES = ['需求', '计划', '确认', '执行', '验证', '汇报'];

  /* 状态机 → 工具栏阶段索引（与后端 contracts/state_machine.py 对齐） */
  const STATUS_TO_STAGE = {
    received: 0, planning: 1, awaiting_confirm: 2,
    executing: 3, verifying: 4, reworking: 1,
    delivering: 5, delivered: 5,
    interrupted: -1, failed: -1,
  };

  const STATUS_LABELS = {
    received: '已接收', planning: '规划中', awaiting_confirm: '待确认',
    executing: '执行中', verifying: '验证中', reworking: '返工中',
    delivering: '汇报中', delivered: '已交付', interrupted: '已中断', failed: '已失败',
  };

  const TOOL_STATUS_LABELS = {
    success: '✓ 成功',
    failed: '✗ 失败',
    running: '⟳ 进行中',
    pending: '○ 待执行',
    pending_approval: '⚠ 待审批',
  };

  /* ── 最近项目（工作区）── */
  const projects = [
    {
      id: 'default',
      name: 'workspace',
      path: '<内置默认工作区>/workspace',
      current: false,
    },
    {
      id: 'prj-agentbuilder',
      name: 'li-tao-agent-builder',
      path: 'C:\\Users\\李陶\\…\\work-mode-projects\\6abcee34807a00aa83da2398',
      current: true,
    },
    {
      id: 'prj-handover',
      name: 'handover-card-proto',
      path: 'C:\\Users\\李陶\\Projects\\handover-card-proto',
      current: false,
    },
  ];

  /* ── 工作区文件树 ── */
  const fileTree = [
    {
      type: 'dir', name: 'agent_builder', path: 'agent_builder', children: [
        {
          type: 'dir', name: 'api', path: 'agent_builder/api', children: [
            { type: 'file', name: 'handover.py', path: 'agent_builder/api/handover.py', size: 4301 },
            { type: 'file', name: 'role_briefs.py', path: 'agent_builder/api/role_briefs.py', size: 7264 },
            { type: 'file', name: 'orchestrator.py', path: 'agent_builder/api/orchestrator.py', size: 12708 },
            { type: 'file', name: 'projects.py', path: 'agent_builder/api/projects.py', size: 5942 },
          ],
        },
        {
          type: 'dir', name: 'roles', path: 'agent_builder/roles', children: [
            { type: 'file', name: 'decomposer.py', path: 'agent_builder/roles/decomposer.py', size: 9120 },
            { type: 'file', name: 'router.py', path: 'agent_builder/roles/router.py', size: 6410 },
          ],
        },
        {
          type: 'dir', name: 'tools', path: 'agent_builder/tools', children: [
            {
              type: 'dir', name: 'impl', path: 'agent_builder/tools/impl', children: [
                { type: 'file', name: 'file_delete.py', path: 'agent_builder/tools/impl/file_delete.py', size: 2480 },
                { type: 'file', name: 'council_build_minutes.py', path: 'agent_builder/tools/impl/council_build_minutes.py', size: 3760 },
              ],
            },
          ],
        },
      ],
    },
    {
      type: 'dir', name: 'frontend', path: 'frontend', children: [
        { type: 'file', name: 'index.html', path: 'frontend/index.html', size: 10482 },
        { type: 'file', name: 'styles.css', path: 'frontend/styles.css', size: 25430 },
        {
          type: 'dir', name: 'js', path: 'frontend/js', children: [
            { type: 'file', name: 'app.js', path: 'frontend/js/app.js', size: 51420 },
            { type: 'file', name: 'api.js', path: 'frontend/js/api.js', size: 5870 },
          ],
        },
      ],
    },
    {
      type: 'dir', name: 'docs', path: 'docs', children: [
        { type: 'file', name: 'HANDOVER.md', path: 'docs/HANDOVER.md', size: 32890 },
        {
          type: 'dir', name: 'reports', path: 'docs/reports', children: [
            { type: 'file', name: 'role-brief-ab.md', path: 'docs/reports/role-brief-ab.md', size: 15320 },
          ],
        },
      ],
    },
    { type: 'file', name: 'README.md', path: 'README.md', size: 2140 },
  ];

  /* ── 文件内容（查看器用）── */
  const fileContents = {
    'agent_builder/api/handover.py': [
      '"""交接提示（handover hint）—— 规则式判定，不调用 LLM。',
      '',
      '门控：对话轮次 / 累计 token / 字符 三条件任一命中即进入判定；',
      '判定 = 门控命中 + 存在未决项 + 任务状态综合。',
      '"""',
      '',
      'from dataclasses import dataclass',
      '',
      'MAX_PROMPTS_PER_SESSION = 3',
      'TURNS_THRESHOLD = 5',
      'TOKENS_THRESHOLD = 12000',
      'CHARS_THRESHOLD = 24000',
      '',
      '',
      '@dataclass(frozen=True)',
      'class HandoverDecision:',
      '    should_suggest: bool',
      '    strength: str          # strong | suggest | none',
      '    reasons: tuple[str, ...]',
      '',
      '',
      'def should_suggest_handover(*, turns, tokens, chars, open_items, status):',
      '    """返回是否建议开启下一轮新对话。"""',
      '    gate_hit = turns > TURNS_THRESHOLD or tokens >= TOKENS_THRESHOLD or chars >= CHARS_THRESHOLD',
      '    ...',
    ].join('\n'),
    'docs/HANDOVER.md': [
      '# 项目交接提示词（Agent Builder）',
      '',
      '## 9. 交接提示（handover hint）—— 已实施',
      '',
      '- 归属层：编排层 agent_builder/api/（不碰 roles/）',
      '- 产物：后端结构化交接对象 + 前端卡片（不调 LLM，generated_by=rule）',
      '- 行为：仅提示；用户手动新开对话（不自动清空、不自动建 task）',
      '- 门控：对话轮次 > 5 或 累计 token ≥ 12000 或 字符 ≥ 24000',
      '- 限次：本会话最多提示 3 次；ack 记 seen / ignored',
      '- 卡片不写入 transcript（前端只存主题偏好），刷新即消失',
      '',
      '（本文件为原型内置的节选假数据）',
    ].join('\n'),
    'frontend/js/api.js': [
      '/* Agent Builder 前端 —— 后端 API 封装层 */',
      "const API_BASE = 'http://127.0.0.1:8000';",
      "const CLIENT_HEADER = 'X-Agent-Builder-Client';",
      '',
      'const REQUEST_TIMEOUT_MS = 15000;',
      'const LLM_TIMEOUT_MS = 120000;',
      'const NO_TIMEOUT_MS = 0;',
      '',
      'const api = {',
      "  getHandover: (taskId, { turns = 0, contextTokens = 0, contextChars = 0 } = {}) =>",
      '    api.request(',
      "      'GET',",
      '      `/tasks/${taskId}/handover?turns=${turns}&context_tokens=${contextTokens}&context_chars=${contextChars}`',
      '    ),',
      "  ackHandover: (taskId, status) => api.request('POST', `/tasks/${taskId}/handover/ack`, { status }),",
      '};',
    ].join('\n'),
    'README.md': [
      '# Agent Builder',
      '',
      '一个「能生成 Agent 的 Meta Agent」。',
      '',
      '- 后端：FastAPI + 状态机（received → planning → awaiting_confirm → executing → verifying → delivering → delivered）',
      '- 前端：纯静态 HTML / CSS / JS',
      '- 角色 20 个 / 工具 26 个，六维评分全满分',
      '',
      '> 本文件为原型内置的节选假数据。',
    ].join('\n'),
  };

  /* ── 评审会纪要（后端 council_build_minutes 规则式收敛产出）── */
  const councilMinutes = {
    topic: '交接提示的阈值口径与「一键新开对话」的边界',
    participants: ['conductor', 'decomposer', 'impact_analyzer', 'fact_checker', 'summarizer'],
    absent: ['proposer'],
    rounds: 2,
    agreements: [
      { role: 'conductor', content: '门控用「对话轮次 / 累计 token / 字符」三条件任一命中，避免单一口径漏判。' },
      { role: 'impact_analyzer', content: '判定必须读任务状态，delivered / interrupted 才提示，执行中不打断。' },
    ],
    disagreements: [
      { role: 'fact_checker', content: '字符阈值（24000）与 token 阈值（12000）存在重叠，建议二者取其一，避免双口径漂移。' },
    ],
    unresolved: [
      { role: 'summarizer', content: '「一键新开对话」是否要把历史摘要写入新会话，未达成一致。' },
    ],
    suggested_decision: '先按三条件 OR 门控上线，卡片只提示不自动清空；摘要继承留到第二轮。',
    decision_note: '先按三条件 OR 门控上线，卡片只提示不自动清空；摘要继承留到第二轮。',
  };

  /* ── 交接对象（GET /tasks/{id}/handover 的响应形状）── */
  function handoverData(strength) {
    const strong = strength === 'strong';
    return {
      task_id: 'a1b2c3d4e5f6',
      task_status: 'delivered',
      should_suggest: true,
      strength: strength,
      generated_by: 'rule',
      reasons: strong
        ? ['对话轮次已达 7（阈值 5）', '累计 token 约 14,300（阈值 12,000）', '存在 3 项未决']
        : ['对话轮次已达 6（阈值 5）', '存在 2 项未决'],
      gate: {
        turns: strong ? 7 : 6, turns_threshold: 5, turns_hit: true,
        tokens: strong ? 14300 : 9800, tokens_threshold: 12000, tokens_hit: strong,
        chars: strong ? 26100 : 19400, chars_threshold: 24000, chars_hit: strong,
        prompts_shown: 1, max_prompts: 3,
      },
      human_open_questions: [
        { source: 'decomposer', text: '字符阈值与 token 阈值是否二选一？' },
        { source: 'self_check', text: '「一键新开对话」是否需要把历史摘要写入新会话？' },
      ],
      machine_open_items: [
        { step_id: 'step-4', action: 'test_run', status: 'pending_approval', detail: 'tests/test_api_handover.py 待审批后执行' },
        { step_id: 'step-5', action: 'file_write', status: 'pending', detail: 'docs/reports/handover-acceptance.md 未产出' },
      ],
      next_actions: [
        '先把字符阈值与 token 阈值合并为单一口径，再跑 A/B。',
        '补 tests/test_frontend_session.py 的卡片接线断言。',
        '确认摘要继承方案后，再放开「一键新开对话」。',
      ],
      ack: { status: null },
    };
  }

  /* ── 场景：每个场景 = { status, taskId, entries, handover } ── */
  const msgAgent = (text) => ({ kind: 'msg', role: 'agent', text });
  const msgUser = (text) => ({ kind: 'msg', role: 'user', text });
  const tool = (title, status, detail) => ({ kind: 'tool', title, status, detail });

  const REQUIREMENT =
    '给会话增加「交接提示」：当对话轮次或累计 token 超过阈值时建议新开一轮对话，并能一键把未决项带到新对话。';

  const scenarios = {
    empty: {
      status: 'received',
      taskId: null,
      handover: null,
      entries: [msgAgent('✅ 后端服务已连接，可开始输入需求。')],
    },

    confirm: {
      status: 'awaiting_confirm',
      taskId: 'a1b2c3d4e5f6',
      handover: null,
      entries: [
        msgAgent('✅ 后端服务已连接，可开始输入需求。'),
        msgUser(REQUIREMENT),
        msgAgent('已接收需求，任务 ID：a1b2c3d4… 正在分解步骤…'),
        tool('searcher · 检索仓库既有实现', 'success', '命中 docs/HANDOVER.md 第 9 节、agent_builder/api/handover.py'),
        tool('web_fetch · 参考同类产品做法', 'success', 'HTTP 200 · 抓取 3 篇，摘要约 1.2k 字'),
        msgAgent('分解完成，发现 2 个待确认点：'),
        {
          kind: 'approval',
          taskId: 'a1b2c3d4e5f6',
          questions: [
            '阈值口径以「对话轮次 / 累计 token / 字符」三条件任一命中为准？',
            '「一键新开对话」是否需要同时把历史摘要写入新会话？',
          ],
          highRiskActions: ['file_write', 'file_delete'],
        },
      ],
    },

    executing: {
      status: 'executing',
      taskId: 'a1b2c3d4e5f6',
      handover: null,
      entries: [
        msgUser(REQUIREMENT),
        msgAgent('分解完成，共 5 个步骤，等待确认计划…'),
        msgAgent('计划已批准，进入执行阶段…'),
        tool('file_write · 新建 agent_builder/api/handover.py', 'success', '写入 4.3 KB，overwrite=true'),
        tool('file_write · 前端交接卡片渲染', 'success', '写入 frontend/js/app.js 差量 180 行'),
        tool('file_delete · 移除临时草稿', 'success', '删除 docs/_draft-handover.tmp'),
        tool('test_run · pytest tests/test_api_handover.py', 'running', '已运行 6 / 23 例…'),
        msgAgent('执行进度：成功 3 / 进行中 1 / 待执行 1 · 共 5 个步骤。'),
      ],
    },

    interrupted: {
      status: 'interrupted',
      lastStageIdx: 3,
      taskId: 'a1b2c3d4e5f6',
      handover: handoverData('suggest'),
      entries: [
        msgUser(REQUIREMENT),
        msgAgent('计划已批准，进入执行阶段…'),
        tool('file_write · 新建 agent_builder/api/handover.py', 'success', '写入 4.3 KB'),
        tool('test_run · pytest tests/test_api_handover.py', 'failed', 'E_VALIDATION：阈值常量缺失（不可重试）'),
        msgAgent('任务已中断，可恢复继续执行或放弃任务。'),
        { kind: 'interrupt', taskId: 'a1b2c3d4e5f6' },
      ],
    },

    delivered: {
      status: 'delivered',
      taskId: 'a1b2c3d4e5f6',
      handover: handoverData('strong'),
      entries: [
        msgAgent('✅ 后端服务已连接，可开始输入需求。'),
        msgUser(REQUIREMENT),
        msgAgent('已接收需求，任务 ID：a1b2c3d4… 正在分解步骤…'),
        tool('searcher · 检索仓库既有实现', 'success', '命中 docs/HANDOVER.md 第 9 节'),
        msgAgent('分解完成，发现 2 个待确认点：'),
        {
          kind: 'approval',
          taskId: 'a1b2c3d4e5f6',
          questions: ['阈值口径以三条件任一命中为准？', '是否需要把历史摘要写入新会话？'],
          highRiskActions: ['file_write', 'file_delete'],
        },
        msgAgent('计划已批准，进入执行阶段…'),
        { kind: 'council', minutes: councilMinutes },
        tool('file_write · 新建 agent_builder/api/handover.py', 'success', '写入 4.3 KB，overwrite=true'),
        tool('test_run · pytest tests/test_api_handover.py', 'success', '23 passed'),
        msgAgent('所有 5 个步骤执行完成（成功 5），进入验证阶段。'),
        msgAgent('验证通过：计划与执行结果一致，未发现一致性问题。'),
        msgAgent('交付完成：新增 handover 判定模块与前端卡片，测试全绿；建议开一轮新对话继续「摘要继承」。'),
      ],
    },
  };

  /* ── 菜单定义 ── */
  const menus = {
    file: [
      { type: 'item', label: '新建需求', accel: 'Ctrl+N', action: 'new-task' },
      { type: 'sep' },
      { type: 'item', label: '打开本地文件夹…', accel: 'Ctrl+K', action: 'open-folder' },
      { type: 'item', label: '新建文件夹…', action: 'new-folder' },
      { type: 'sep' },
      { type: 'item', label: '刷新文件树', accel: 'F5', action: 'refresh-files' },
      { type: 'item', label: '重新加载会话', action: 'reload-session' },
      { type: 'sep' },
      { type: 'item', label: '退出', accel: 'Ctrl+W', action: 'quit' },
    ],
    edit: [
      { type: 'item', label: '撤销', accel: 'Ctrl+Z', action: 'undo', disabled: true },
      { type: 'item', label: '重做', accel: 'Ctrl+Y', action: 'redo', disabled: true },
      { type: 'sep' },
      { type: 'item', label: '复制交接提示', accel: 'Ctrl+Shift+C', action: 'copy-handover' },
      { type: 'item', label: '清空对话', action: 'clear-chat' },
      { type: 'sep' },
      { type: 'item', label: 'API 密钥设置', accel: 'Ctrl+,', action: 'focus-key' },
    ],
    view: [
      { type: 'heading', label: '主题' },
      { type: 'radio', label: '跟随系统', group: 'theme', value: 'auto', action: 'theme' },
      { type: 'radio', label: '浅色', group: 'theme', value: 'light', action: 'theme' },
      { type: 'radio', label: '深色', group: 'theme', value: 'dark', action: 'theme' },
      { type: 'sep' },
      { type: 'check', label: '侧栏', accel: 'Ctrl+B', action: 'toggle-sidebars', checked: true },
      { type: 'check', label: '演示控制台', action: 'toggle-console', checked: true },
      { type: 'check', label: '状态栏', action: 'toggle-statusbar', checked: true },
      { type: 'sep' },
      { type: 'item', label: '最大化 / 还原', accel: 'F11', action: 'maximize' },
      { type: 'item', label: '重新加载文件树', action: 'refresh-files' },
    ],
    project: [
      { type: 'heading', label: '最近项目' },
      { type: 'sep' },
      { type: 'item', label: '使用默认工作区', action: 'default-workspace' },
      { type: 'item', label: '在文件管理器中打开', action: 'reveal-project' },
    ],
    task: [
      { type: 'item', label: '发送需求', accel: 'Ctrl+Enter', action: 'send' },
      { type: 'sep' },
      { type: 'item', label: '中断任务', accel: 'Ctrl+.', action: 'interrupt' },
      { type: 'item', label: '恢复任务', action: 'resume' },
      { type: 'item', label: '放弃任务', action: 'abort' },
      { type: 'sep' },
      { type: 'item', label: '召开评审会', action: 'council' },
    ],
    help: [
      { type: 'item', label: '键盘快捷键', action: 'shortcuts' },
      { type: 'item', label: '文档（docs/HANDOVER.md）', action: 'docs' },
      { type: 'sep' },
      { type: 'item', label: '关于 Agent Builder', action: 'about' },
    ],
  };

  const shortcuts = [
    ['Ctrl+N', '新建需求'],
    ['Ctrl+K', '打开本地文件夹'],
    ['Ctrl+Enter', '发送需求'],
    ['Ctrl+.', '中断任务'],
    ['Ctrl+B', '显示 / 隐藏侧栏'],
    ['Ctrl+,', '聚焦 API 密钥'],
    ['F5', '刷新文件树'],
    ['F11', '最大化 / 还原窗口'],
    ['Esc', '关闭菜单 / 对话框'],
  ];

  /* 系统「选择文件夹」对话框的假数据 */
  const folderPicker = {
    path: 'C:\\Users\\李陶',
    folders: [
      { name: 'AppData', path: 'C:\\Users\\李陶\\AppData' },
      { name: 'Documents', path: 'C:\\Users\\李陶\\Documents' },
      { name: 'Downloads', path: 'C:\\Users\\李陶\\Downloads' },
      { name: 'Projects', path: 'C:\\Users\\李陶\\Projects' },
      { name: 'Desktop', path: 'C:\\Users\\李陶\\Desktop' },
    ],
    selected: 'C:\\Users\\李陶\\Projects',
  };

  window.MOCK = {
    appName: 'Agent Builder',
    version: '0.9.0-desktop-proto',
    STAGES,
    STATUS_TO_STAGE,
    STATUS_LABELS,
    TOOL_STATUS_LABELS,
    projects,
    fileTree,
    fileContents,
    councilMinutes,
    scenarios,
    menus,
    shortcuts,
    folderPicker,
  };
})();
