# 02 · 任务状态机

总指挥（Conductor）维护的唯一状态权威来源。任何状态转换必须符合本表。

## 状态一览

```
received → planning → awaiting_confirm → executing → verifying → delivering → delivered
                                    ↑            │              │
                              (用户修改)          ↓              ↓
                                             interrupted ←─ reworking ──→ executing
                                                │  │            │
                                                │  └─→ resumed   └─→ failed (超过 2 轮)
                                                └───→ failed
```

## 转换表

| 当前状态 | 事件 | 目标状态 | 触发者 |
| --- | --- | --- | --- |
| `received` | 需求已确认、分解器产出 DAG | `planning` | Conductor |
| `planning` | 调度器产出执行计划 | `awaiting_confirm` | 调度器 |
| `awaiting_confirm` | 用户确认计划 | `executing` | 用户 |
| `awaiting_confirm` | 用户要求修改 | `planning` | 用户 |
| `executing` | 全部步骤完成 | `verifying` | 路由者 |
| `executing` | 用户停止 / 高风险动作挂起 | `interrupted` | 用户 / 门卫 |
| `verifying` | 测试/核验通过 | `delivering` | 验证组 |
| `verifying` | 验证失败且 retry_count < 2 | `reworking` | 验证组 |
| `verifying` | 验证失败且 retry_count ≥ 2 | `failed` | 验证组 |
| `reworking` | 返工完成重新提交 | `executing` | Conductor |
| `interrupted` | 用户恢复（带 resume_point） | `executing` | 用户 |
| `interrupted` | 用户放弃 / 不可恢复错误 | `failed` | 用户 / Conductor |
| `delivering` | 汇报员交付最终报告 | `delivered` | 汇报员 |
| 任意 | 不可恢复内部错误 | `failed` | Conductor |

## 规则

1. `retry_count` 每次进入 `reworking` 时 +1，上限 2。
2. 进入 `interrupted` 时必须记录 `resume_point`（中断时正在执行的步骤或审批门位置）。
3. 任何状态都可因 `E_INTERNAL` 转 `failed`，并附带错误码与日志引用。
4. 状态变更必须写审计日志（audit_log），格式见 `01-schemas.md` TaskState。

## 副架构的介入

副架构不直接转换任务状态；它只通过 `change_proposal` 请求修改主架构本身，且必须走审批门（编程者同意）后由看门人应用。
