/* ============================================================
 * mock.js —— 唯一数据源（原型内所有渲染只从这里读取，不散落硬编码）
 *
 * 数据形状 = 后端 HandoverResponse（契约见 api.js 顶部注释）：
 *   should_suggest / strength / reasons / gate{...} / ack{...}
 *   human_open_questions[]（人的未决 · 优先）
 *   machine_open_items[]（机器未决）
 *   next_actions[]
 * ============================================================ */

const MOCK = {
  task: {
    taskId: 't-8f3c21',
    requirement: '给记忆管家增加「对话超过 n 次时检测是否需建立交接提示」的能力',
  },

  /* 触发参数（★ 带 ★ 的为待你确认项）
   * 门控：对话轮次 > turns，或 累计 token ≥ tokens（任一命中即进入判定）
   * 限次：本会话最多提示 maxPrompts 次，超出后不再提示 */
  thresholds: {
    turns: 5,
    turnsEnv: 'AGENT_BUILDER_HANDOVER_TURNS', // ★ 是否要这个变量名/默认值
    tokens: 12000,
    tokensEnv: 'AGENT_BUILDER_HANDOVER_TOKENS', // ★ token 阈值沿用先前约定 12000
    maxPrompts: 2,
    maxPromptsEnv: 'AGENT_BUILDER_HANDOVER_MAX_PROMPTS', // ★ 上限值待定
    chars: 24000,
    charsEnv: 'AGENT_BUILDER_HANDOVER_CHARS', // ★ 字符口径是否仍参与门控待确认（当前仅展示）
  },

  // 任务状态：delivered 时后端判定「不打扰」（已闭环不必交接）
  statuses: {
    executing: 'executing（未交付）',
    delivered: 'delivered（已交付）',
  },

  // 交接处理状态：后端记录「已查看 / 已忽略」，用于限次与去重（前端不持久化卡片）
  ackStates: {
    none: '',
    seen: '已查看',
    ignored: '已忽略',
  },

  // 演示场景：控制台可切换，对应后端判定的不同组合
  scenarios: {
    strong: {
      label: '强烈建议',
      metric: { turns: 7, context_tokens: 13240, context_chars: 26800 },
      human_open_questions: [
        {
          text: '「对话超过 n 次」的 n 未定：按轮数 / 会话条目数 / token 量？',
          source: 'pending_questions',
        },
        {
          text: '需要 LLM 拆分需求（当前无预拆分步骤）',
          source: 'pending_questions',
        },
      ],
      machine_open_items: [
        {
          step_id: 'step-004',
          action: 'web_fetch',
          status: 'failed',
          detail: '来源链接失效，重试 2 次仍失败',
        },
        {
          step_id: 'step-007',
          action: 'file_write',
          status: 'pending_approval',
          detail: '写文件需审批，等待人工放行',
        },
      ],
      next_actions: [
        '把本卡片内容粘贴到新对话首条消息，作为接续上下文',
        '先答复「人的未决」第 1 条（n 的口径），再继续规划',
        'step-004 换来源重试，或确认后剔除该步骤',
      ],
    },

    suggest: {
      label: '建议',
      metric: { turns: 6, context_tokens: 4800, context_chars: 9700 },
      human_open_questions: [],
      machine_open_items: [],
      next_actions: [
        '当前无未决项，可安全开新对话',
        '把本卡片内容粘贴到新对话首条消息，避免上下文被裁掉',
      ],
    },

    none: {
      label: '暂不需要',
      metric: { turns: 2, context_tokens: 2100, context_chars: 4200 },
      human_open_questions: [],
      machine_open_items: [],
      next_actions: [],
    },
  },
};
