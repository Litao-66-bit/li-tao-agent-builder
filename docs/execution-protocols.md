# 执行协议（Execution Protocols）

每个角色的触发条件、分步流程、异常处理、交接规范。

---

## Conductor（总指挥）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `conductor` |
| 层级 | conductor（总指挥层） |
| 使命 | 编排任务全程，维护状态机五阶段闭环 |
| 服务对象 | 用户（最终交付）+ 执行层（分派）+ 记忆管家（写入经验） |
| 触发时机 | 用户提交新任务时启动，贯穿任务全程 |
| 交付物 | 最终答复给用户 + 经验写入记忆管家 |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `plan_validate` | 校验计划 DAG | low |
| `approval_request` | 发起审批请求 | low |
| `change_notify` | 变更通知编程者 | low |
| `audit_log` | 写审计日志 | low |
| `memory_read` | 读记忆/经验 | low |
| `config_read` | 读架构配置 | low |

**边界声明**：不可直接写文件、提交代码、执行命令。只编排不执行。

### 执行协议

#### 触发条件
用户提交新任务（TaskState.status = RECEIVED）。

#### 分步流程

```
1. 接收需求
   - 解析 task_id + requirement
   - 状态转 PLANNING（REQ_CONFIRMED）
   - 调用分解器进入规划

2. 规划完成
   - 状态转 AWAITING_CONFIRM（PLAN_READY）
   - 等待用户确认
   - 决策：用户确认 → EXECUTING；用户拒绝 → 回 PLANNING

3. 执行
   - 分派任务给执行层
   - 监控步骤状态
   - 决策：全部完成 → VERIFYING；用户中断 → INTERRUPTED

4. 验证
   - 校验执行结果
   - 决策：通过 → DELIVERING；失败 + retry < 2 → REWORKING；失败 + retry >= 2 → FAILED

5. 交付
   - 最终答复给用户
   - 状态转 DELIVERED

6. 收尾
   - 记忆写入（经验/教训）
   - 日志归档
   - 运行数据上报审计员
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 步骤超时 | 记录错误 → 重试 1 次 → 仍失败降级（跳过非关键步骤或询问用户） |
| 工具拒绝 | 降级 or 升级编程者 |
| 验证 2 轮失败 | 交付部分结果 + 失败原因（不硬扛） |
| 用户中断 | 保存 resume_point → 可续跑 |
| 不可恢复错误 | 状态转 FAILED（INTERNAL_ERROR / ABORT） |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| 用户 | 最终答复 | 文本（结果 + 失败原因） |
| 记忆管家 | 经验写入请求 | 记忆条目（sensitive=true 时加密） |
| 审计员 | 运行数据 | 审计日志条目 |

#### 完成标志

五阶段全部闭环，TaskState.status = DELIVERED，产物交付、状态归档。
