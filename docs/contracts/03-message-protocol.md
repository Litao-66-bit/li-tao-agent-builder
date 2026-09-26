# 03 · 消息协议

角色之间只通过结构化消息通信（禁止自由聊天）。每条消息带统一头 + 类型体。

## 统一消息头

```json
{
  "msg_id": "m-0001",
  "type": "<见下表>",
  "from": "<角色名>",
  "to": "<角色名 | conductor | user>",
  "task_id": "t-...",
  "correlation_id": "c-...",
  "ts": "2026-09-26T10:00:00+08:00"
}
```

`correlation_id` 贯穿一次任务/一次返工循环，用于追踪与审计。

## 消息类型清单

| type | 方向 | 用途 | 关键字段 |
| --- | --- | --- | --- |
| `task_assign` | Conductor/路由者 → 执行者 | 分派步骤 | `step_id`, `payload` |
| `tool_call` | 任意角色 → 工具门卫 | 请求工具执行 | `tool`, `args` |
| `tool_result` | 工具门卫 → 请求方 | 返回结果或拒绝 | `audit_id`, `status`, `result`/`error_code` |
| `plan_proposal` | 调度器 → Conductor → 用户 | 展示执行计划 | `plan` |
| `plan_confirm` | 用户 → Conductor | 确认/修改计划 | `confirmed`, `changes?` |
| `report` | 执行者/验证者 → 汇总方 | 交付阶段性产出 | `content`, `sources[]` |
| `rework_request` | 验证组 → Conductor | 打回返工 | `target_step`, `reason`, `test_log` |
| `approval_request` | 门卫/看门人 → 用户 | 高风险动作求批 | `approval_type`, `target`, `detail`, `expires_at` |
| `approval_result` | 用户 → 请求方 | 审批结果 | `approved`, `comment`, `ts` |
| `interrupt` | 用户/门卫 → Conductor | 请求中断 | `reason`, `resume_point?` |
| `metric_report` | 主架构 → 审计员 | 上报运行指标 | `metrics{}`, `window` |
| `change_proposal` | 审计员/方案生成者 → 编程者 | 提交变更提案 | `proposal` |
| `change_notify` | 记录员 → 编程者 | 变更完成通知 | `proposal_id`, `diff_summary`, `result` |

## 规则

1. 任何角色不得发送未在表中的消息类型；新类型必须经契约层评审后加入。
2. `tool_call` 只能发给工具门卫；门卫是唯一执行者。
3. `approval_request` 必须带 `expires_at`（默认 24h），过期自动拒绝。
4. 所有消息落审计日志（`audit_log`），`correlation_id` 用于串联一次任务全链路。
