/* ============================================================
 * api.js —— 桩接口层（签名/时延/错误形态 = 未来真实 API 的形状）
 *
 * 未来真实接口（待实现）：
 *   GET  /tasks/{task_id}/handover?turns={int}&context_tokens={int}&context_chars={int}
 *        请求头：X-Agent-Builder-Client: web（本机标识头，见后端 require_local_client）
 *        200 → HandoverResponse {
 *                task_id, should_suggest, strength: 'strong'|'suggest'|'none', reasons[],
 *                gate{turns, turns_threshold, turns_hit, tokens, tokens_threshold, tokens_hit,
 *                     prompts_shown, max_prompts, exhausted},
 *                ack{status: 'none'|'seen'|'ignored', at},
 *                human_open_questions[{text, source}], machine_open_items[{step_id, action, status, detail}],
 *                next_actions[], task_status, generated_by: 'rule'
 *              }
 *   POST /tasks/{task_id}/handover/ack  { status: 'seen'|'ignored' }
 *        请求头同上；200 → 更新后的 ack（用于限次与去重；前端不持久化卡片本身）
 *
 * 口径（已确认决策）：
 *   - 门控：对话轮次 > 5 **或** 累计 token ≥ 12000（任一命中即进入判定）★ 待最终确认
 *   - 判定：门控命中 + 未决项 + 任务状态（综合）；未决分「人的未决（优先）/ 机器未决」
 *   - 限次：本会话最多提示 maxPrompts 次，超出后不再提示
 *   - 记录：已查看 / 已忽略（后端记 ack）；卡片**不写入 transcript**
 *   - 本版不调 LLM（generated_by='rule'）
 * ============================================================ */

const api = (() => {
  // 原型专用开关：模拟「后端 TaskEntry / 会话级已知状态」
  // （真实实现：turns/context_tokens 由前端上报，open_items 与 ack 来自 TaskEntry）
  const draft = {
    scenario: 'strong',
    status: 'executing',
    promptsShown: 0,
    ack: 'none',
    fail: false,
  };

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // 后端判定（原型内模拟；参数与组合方式 ★ 待定稿）
  function decide({ metric, human, machine, status, thresholds, promptsShown, ack }) {
    const turnsHit = metric.turns > thresholds.turns;
    const tokensHit = metric.context_tokens >= thresholds.tokens;
    const gate = turnsHit || tokensHit;
    const exhausted = promptsShown >= thresholds.maxPrompts;
    const openItems = human.length + machine.length;

    let strength = 'none';
    if (status === 'delivered') {
      strength = 'none';
    } else if (exhausted) {
      strength = 'none';
    } else if (ack === 'ignored') {
      strength = 'none';
    } else if (gate && openItems > 0) {
      strength = 'strong';
    } else if (gate) {
      strength = 'suggest';
    }

    const reasons = [];
    if (status === 'delivered') {
      reasons.push('任务已交付（delivered），无需交接');
    } else if (exhausted) {
      reasons.push(`本会话已提示 ${promptsShown} 次，达上限（${thresholds.maxPrompts}）`);
    } else if (ack === 'ignored') {
      reasons.push('上一次交接提示已被忽略（不重复打扰）');
    } else {
      const hits = [];
      if (turnsHit) hits.push(`对话轮次 ${metric.turns} > ${thresholds.turns}`);
      if (tokensHit) hits.push(`累计 token ${metric.context_tokens} ≥ ${thresholds.tokens}`);
      reasons.push(hits.length ? `门控命中：${hits.join(' 或 ')}` : '门控未命中（轮次与 token 均未达阈值）');
      if (human.length) reasons.push(`人的未决 ${human.length} 项（优先确认）`);
      if (machine.length) reasons.push(`机器未决 ${machine.length} 项`);
      if (openItems === 0) reasons.push('无未决项');
    }
    return { should_suggest: strength !== 'none', strength, reasons };
  }

  /** GET /tasks/{id}/handover —— 读取交接判定与结构化交接对象（幂等，无副作用）。 */
  async function fetchHandover(taskId, options = {}) {
    const { turns = 0, contextTokens = 0, contextChars = 0 } = options;
    await delay(360); // TODO: replace with fetch(`/tasks/${taskId}/handover?...`)
    if (draft.fail) {
      const err = new Error('后端未响应（原型模拟：接口失败）');
      err.code = 'SIMULATED_FAILURE';
      throw err;
    }

    const t = MOCK.thresholds;
    const scenario = MOCK.scenarios[draft.scenario];
    const metric = { turns, context_tokens: contextTokens, context_chars: contextChars };
    const decision = decide({
      metric,
      human: scenario.human_open_questions,
      machine: scenario.machine_open_items,
      status: draft.status,
      thresholds: t,
      promptsShown: draft.promptsShown,
      ack: draft.ack,
    });

    return {
      task_id: taskId,
      task_status: draft.status,
      generated_by: 'rule',
      metric,
      gate: {
        turns: metric.turns,
        turns_threshold: t.turns,
        turns_hit: metric.turns > t.turns,
        tokens: metric.context_tokens,
        tokens_threshold: t.tokens,
        tokens_hit: metric.context_tokens >= t.tokens,
        chars: metric.context_chars,
        chars_threshold: t.chars,
        prompts_shown: draft.promptsShown,
        max_prompts: t.maxPrompts,
        exhausted: draft.promptsShown >= t.maxPrompts,
      },
      ack: { status: draft.ack, at: draft.ack === 'none' ? null : '2026-10-04T10:12:00Z' },
      human_open_questions: scenario.human_open_questions,
      machine_open_items: scenario.machine_open_items,
      next_actions: scenario.next_actions,
      ...decision,
    };
  }

  /** POST /tasks/{id}/handover/ack —— 记录「已查看 / 已忽略」（用于限次与去重）。 */
  async function ackHandover(taskId, status) {
    await delay(120); // TODO: replace with fetch(..., { method: 'POST', body: JSON.stringify({ status }) })
    draft.ack = status;
    return { task_id: taskId, ack: { status, at: '2026-10-04T10:12:00Z' } };
  }

  return {
    fetchHandover,
    ackHandover,
    __draft: draft, // 原型专用：控制台改它来模拟后端已知状态
  };
})();
