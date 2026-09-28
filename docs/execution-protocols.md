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

---

## Decomposer（分解器）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `decomposer` |
| 层级 | executor（主架构·规划层） |
| 使命 | 解析需求，拆成原子步骤 DAG（每步 = 一次工具调用或明确动作） |
| 服务对象 | conductor（上游）→ scheduler（下游） |
| 触发时机 | 总指挥确认需求理解后调用（PLANNING 阶段） |
| 交付物 | 步骤 DAG（JSON：steps + order + parallel_groups + pending_questions） |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `config_read` | 读已启用技能/插件清单 | low |
| `memory_read` | 读历史经验辅助拆分 | low |
| `audit_log` | 写审计日志 | low |

**边界声明**：只规划不执行；有歧义不猜 → 写 pending_questions。

### 执行协议

#### 触发条件
conductor 确认需求后调用（TaskState.status = PLANNING）。

#### 分步流程

```
1. 接收需求 + 上下文 → 解析意图、目标、约束、隐含条件
2. 结合技能/插件清单 → 拆成原子步骤（每步 = 一次工具调用或明确动作）
3. 去重 → 相同 action + 相同 inputs 的步骤合并
4. 标注依赖（depends_on）→ 按拓扑排序分组（同层可并行）
5. 歧义检查 → 未定义的依赖引用写入 pending_questions
6. 超 10 步自动分组为 parallel_groups
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 需求信息不足 | 返回"需要补充"信号（pending_questions），不硬拆 |
| 步骤 id 重复 | E_VALIDATION |
| 步骤缺少 id/action | E_VALIDATION |
| 依赖有环 | 环中步骤归入同一组（避免死锁） |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| scheduler | 步骤 DAG | DecomposeResult（steps + order + parallel_groups） |
| conductor | 待确认清单 | pending_questions（如有歧义） |

#### 完成标志

输出步骤 DAG（JSON），无未定义步骤（pending_questions 为空或已转交 conductor）。

---

## Scheduler（调度器）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `scheduler` |
| 层级 | executor（主架构·规划层） |
| 使命 | 步骤 DAG → 执行计划（顺序 + 并行组 + 失败预案） |
| 服务对象 | decomposer（上游）→ 用户（确认）→ 路由者（下游） |
| 触发时机 | 拿到分解器的步骤 DAG 后 |
| 交付物 | Plan（order + parallel_groups + fallback） |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `plan_validate` | 校验生成的计划 DAG | low |
| `config_read` | 读配置（资源上限等） | low |
| `memory_read` | 读历史经验辅助预判失败 | low |
| `audit_log` | 写审计日志 | low |

**边界声明**：只调度不执行；环依赖返回分解器修正。

### 执行协议

#### 触发条件
decomposer 产出步骤 DAG 后调用。

#### 分步流程

```
1. 接收 DecomposeResult（步骤 DAG）
2. 环依赖检测 → 有环返回修正请求
3. 拓扑排序分层 → 每层不超过 MAX_PARALLEL（资源上限裁剪）
4. 并行收益判断 → 组内 2 个快速操作改顺序（收益 < 协调成本）
5. 失败预案 → 每步标 skip/retry（写操作不重试防损坏；测试失败跳过不阻塞）
6. 输出 Plan → 展示给用户确认
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 环依赖 | 返回分解器要求修正（pending_questions） |
| 无步骤 | 返回待确认（等 LLM 拆分） |
| 资源上限冲突 | 优先保关键路径（裁剪到 MAX_PARALLEL） |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| 用户 | 执行计划 | Plan（order + parallel_groups + fallback） |
| 路由者 | 已确认计划 | Plan（confirmed_by_user=true） |
| 分解器 | 修正请求 | pending_questions（环依赖） |

#### 完成标志

输出执行计划（顺序 + 并行组 + 失败预案），展示给用户确认。

---

## Router（路由者）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `router` |
| 层级 | executor（主架构·执行层） |
| 使命 | 匹配执行者 + 派发步骤 + 监控进度 + 回收产出 |
| 服务对象 | scheduler（上游）→ 验证组（下游） |
| 触发时机 | 执行计划获用户确认（EXECUTING 阶段） |
| 交付物 | 执行结果集（RouteResult） |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `plan_validate` | 校验计划格式 | low |
| `config_read` | 读执行者配置 | low |
| `audit_log` | 写审计日志 | low |
| `metric_collect` | 采集执行指标 | low |

**边界声明**：只路由不执行；失败重派 1 次；权限不足转发审批门。

### 执行协议

#### 触发条件
执行计划获用户确认（Plan.confirmed_by_user=True）。

#### 分步流程

```
1. 接收已确认的 Plan + steps
2. 按步骤的 action 匹配执行者类型（EXECUTOR_MAP）
3. 派发步骤 → executor_fn 执行（None 则只匹配不执行）
4. 监控进度 → 超时标记失败重派
5. 回收产出 → 汇总执行结果集
6. 分类处理：
   - 成功 → done
   - 失败 + retries < max → 重派
   - 失败 + retries >= max → 上报总指挥（pending_escalation）
   - 权限不足 → 转发审批门（pending_approval）
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 步骤失败 | 重派 1 次；仍失败 → 上报总指挥 |
| 执行者超时 | 标记失败重派 |
| 权限不足 | 转发审批门请求用户（pending_approval） |
| 类型不明 | 默认派给 general_executor |
| 无匹配执行者 | 上报总指挥 |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| 验证组 | 执行结果集 | RouteResult（results + pending_escalation + pending_approval） |
| conductor | 上报清单 | pending_escalation（无匹配/重派失败） |
| 审批门 | 审批请求 | pending_approval（权限不足的步骤） |

#### 完成标志

所有步骤完成（done）或明确失败上报（pending_escalation/pending_approval）。

---

## CodeWorker（代码执行者）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `code_worker` |
| 层级 | executor（主架构·执行层） |
| 使命 | 写/改代码 + 自测 + 变更说明 |
| 服务对象 | router（上游）→ 测试执行者（下游） |
| 触发时机 | 收到路由者分派的代码类步骤 |
| 交付物 | 代码 + 变更说明 + 运行方式 + 自检声明 |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `file_read` | 读代码文件 | low |
| `file_list` | 列目录结构 | low |
| `code_search` | 搜索代码 | low |
| `file_write` | 写/改代码 | **high**（需审批） |
| `sandbox_run` | 沙箱自测 | low |
| `memory_read` | 读上下文 | low |
| `audit_log` | 写审计日志 | low |

**边界声明**：新依赖需批准；任务超出代码范围拒绝；自测2次失败如实上报。

### 执行协议

#### 触发条件
收到路由者分派的代码类步骤（action ∈ CODE_ACTIONS）。

#### 分步流程

```
1. 读取任务上下文与相关代码文件（经记忆管家检索）
2. 校验是否代码类 → 非代码 → rejected
3. 检查新依赖 → 有 → pending_approval（不擅自装包）
4. 写/改代码，遵守既有目录结构与命名规范
5. 自带最小自测（sandbox_run）
   - 自测失败 → 自查修复（最多 2 次）
   - 2 次仍失败 → 如实上报（不交半成品）
6. 产出：代码 + 变更说明 + 运行方式 + 自检声明
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 非代码类步骤 | rejected（拒绝并说明） |
| 需要新依赖 | pending_approval（待批准，不擅自装包） |
| 权限不足 | pending_approval（转发审批门） |
| 自测失败 | 自查修复 2 次；仍失败 → failed（如实上报） |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| router | 执行结果 | CodeResult（status + files_changed + change_desc） |
| 测试执行者 | 代码 + 运行方式 | files_changed + run_instructions |
| 审批门 | 新依赖清单 | pending_dependencies |

#### 完成标志

代码 + 变更说明 + 运行方式 + 自检声明（self_check_passed=True）。

---

## DocWorker（文档执行者）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `doc_worker` |
| 层级 | executor（主架构·执行层） |
| 使命 | 组织结构化文档 + 来源标注 + 不编造 |
| 服务对象 | router（上游）→ 事实核验者（下游） |
| 触发时机 | 收到文档类步骤 |
| 交付物 | 文档 + 来源标注清单 |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `file_read` | 读素材 | low |
| `file_write` | 写文档 | **high**（需审批） |
| `web_fetch` | 抓取素材 | low |
| `citation_check` | 校验来源 | low |
| `memory_read` | 读记忆中的素材 | low |
| `audit_log` | 写审计日志 | low |

**边界声明**：素材不足请求补充；不编造版本号/人名/数据；来源不可考标存疑。

### 执行协议

#### 触发条件
收到路由者分派的文档类步骤（action ∈ DOC_ACTIONS）。

#### 分步流程

```
1. 收集素材/代码/结论（检索执行者或记忆管家的产出）
2. 校验是否文档类 → 非文档 → rejected
3. 检查素材是否充分 → 不足 → pending（请求补充而非编造）
4. 组织结构化文档，每个事实标注来源
5. 未知内容明确写"待补充"，不编造版本号/人名/数据
6. 来源不可考 → 删除或标存疑（verified=False）
7. 产出：文档 + 来源标注清单
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 非文档类步骤 | rejected（拒绝并说明） |
| 素材不足 | pending（请求补充，不编造） |
| 权限不足 | pending_approval（转发审批门） |
| 来源不可考 | 标存疑（verified=False）或删除 |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| router | 执行结果 | DocResult（document + sources） |
| 事实核验者 | 文档 + 来源 | document + sources（fact + source + verified） |
| conductor | 待补充清单 | pending_supplements |

#### 完成标志

文档 + 来源标注清单（每个事实有来源，未知内容标"待补充"）。

---

## DataAnalyst（数据分析者）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `data_analyst` |
| 层级 | executor（主架构·执行层） |
| 使命 | 读数据 + 工具计算（绝不心算）+ 结论分类 |
| 服务对象 | router（上游）→ 事实核验者（下游） |
| 触发时机 | 收到数据分析类步骤 |
| 交付物 | 结论 + 计算过程 + 口径说明 |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `data_query` | 读数据 | low |
| `sandbox_run` | 工具计算 | low |
| `file_read` | 读数据文件 | low |
| `memory_read` | 读上下文 | low |
| `audit_log` | 写审计日志 | low |
| `citation_check` | 校验来源 | low |

**边界声明**：绝不心算；样本不足出质量报告；口径冲突以用户指定为准。

### 执行协议

#### 触发条件
收到路由者分派的数据类步骤（action ∈ DATA_ACTIONS）。

#### 分步流程

```
1. 读取数据文件，记录清洗规则（去重/补缺失/异常值处理）
2. 校验是否数据类 → 非数据 → rejected
3. 检查数据质量 → 缺失率超阈值 → quality_report（不下结论）
4. 用工具计算（sandbox_run / data_query，绝不心算），保留计算过程
5. 输出结论，区分"事实/推断/待验证"，写明口径
6. 口径冲突 → 以用户指定口径为准并标注差异
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 非数据类步骤 | rejected（拒绝并说明） |
| 数据缺失超阈值 | quality_report（不下结论） |
| 权限不足 | failed（记录错误） |
| 口径冲突 | 以用户指定口径为准，标注差异 |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| router | 执行结果 | DataResult（conclusions + calc_process） |
| 事实核验者 | 结论 + 口径 | conclusions（content + type + caliber） |
| conductor | 质量报告 | quality_report（样本不足时） |

#### 完成标志

结论 + 计算过程 + 口径说明（每条结论标明 fact/inference/pending_verification）。

---

## Searcher（检索执行者）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `searcher` |
| 层级 | executor（主架构·执行层） |
| 使命 | 多路关键词检索 + 去重排序 + 来源标注 |
| 服务对象 | router（上游）→ 各执行者（引用） |
| 触发时机 | 收到信息检索类步骤 |
| 交付物 | `[{claim, source_url, snippet, confidence, verified}]` |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `web_search` | 网页搜索 | low |
| `web_fetch` | 抓取页面 | low |
| `citation_check` | 校验来源 | low |
| `memory_read` | 读记忆 | low |
| `audit_log` | 写审计日志 | low |

**边界声明**：结果不足换关键词再搜 1 轮；无来源标"未查证"；工具被拒换合法工具或上报。

### 执行协议

#### 触发条件
收到路由者分派的检索类步骤（action ∈ SEARCH_ACTIONS）。

#### 分步流程

```
1. 拆解检索需求为多路关键词（中英文各一路）
2. 校验是否检索类 → 非检索 → rejected
3. 经工具门卫调用检索工具
4. 去重（按 source_url + claim）、按权威性（置信度）排序
5. 结果不足（< MIN_RESULTS）→ 换关键词再搜 1 轮（最多 MAX_ROUNDS 轮）
6. 关键声明无来源 → 标"未查证"（verified=False）
7. 输出带来源链接与置信度的清单
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 非检索类步骤 | rejected（拒绝并说明） |
| 工具被门卫拒绝 | failed（查明原因，换合法工具或上报） |
| 结果不足 | 换关键词再搜 1 轮 |
| 关键声明无来源 | 标"未查证"（verified=False） |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| router | 执行结果 | SearchResult（items + keywords_used + rounds） |
| 各执行者 | 检索清单 | items（claim + source_url + snippet + confidence + verified） |

#### 完成标志

`[{claim, source_url, snippet, confidence, verified}]`（区分"已查证/单方声称"）。

---

## ToolGuardian（工具门卫）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `tool_guardian` |
| 层级 | governance（主架构·工具层） |
| 使命 | 校验工具白名单 + 参数安全 + 审计日志 |
| 服务对象 | 所有角色（唯一出口，不可绕过） |
| 触发时机 | 任何角色发起工具调用 |
| 交付物 | 工具结果 或 拒绝原因 |

### 实现说明

**已实现**为 `agent_builder/tools/gatekeeper.py` 的 `ToolGatekeeper` 类。

核心校验流程：
1. 角色权限校验（角色未注册 / 工具未列入白名单 → E_PERMISSION，永不重试）
2. 高风险工具审批门（file_write / git_commit / rollback 需审批）
3. 文件类工具沙箱路径校验（realpath 必须在白名单目录内）
4. 网络类工具 URL 安全校验（防 SSRF，禁私有网段）
5. 写审计日志（谁调的、参数、结果、耗时）

### 校验分支

| 校验类型 | 适用工具 | 方法 |
|---|---|---|
| 沙箱 path/repo_path | file_write, file_read, file_list, code_search, data_query, git_commit, rollback, git_log | `_check_sandbox_path` |
| 可选 path | sandbox_run | `_check_sandbox_path_optional` |
| 混合 target | test_run | `_check_target_safety` |
| URL 安全 | web_fetch, web_search | `_check_url_safety` |
| 纯角色校验 | 其余工具 | （无额外校验） |

### 决策规则

| 规则 | 处理 |
|---|---|
| 命中禁止规则 | 拒绝 + 返回拒绝原因 |
| 高风险动作（删除/覆盖/外发） | 转审批门等用户确认 |
| 怀疑注入 | 拒绝 + 标记事件上报审计员 |

### 完成标志

工具结果（executed）或拒绝原因（denied）。

---

## TestRunner（测试执行者）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `test_runner` |
| 层级 | executor（主架构·验证层） |
| 使命 | 跑用例 + 权限边界检查 + 测试报告 |
| 服务对象 | router（上游）→ conductor（决定返工或放行） |
| 触发时机 | 收到待验证产出 |
| 交付物 | 测试报告（passed/failed + 可复现日志） |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `test_run` | 跑测试 | low |
| `sandbox_run` | 沙箱内执行 | low |
| `file_read` | 读测试代码 | low |
| `code_search` | 搜索代码 | low |
| `memory_read` | 读上下文 | low |
| `audit_log` | 写审计日志 | low |
| `citation_check` | 校验来源 | low |

**边界声明**：不修改被测代码；失败打回执行层；环境异常重试 1 次。

### 执行协议

#### 触发条件
收到待验证产出（action ∈ TEST_ACTIONS）。

#### 分步流程

```
1. 按验收标准写/跑用例（沙箱内）
2. 校验是否验证类 → 非验证 → rejected
3. 权限边界检查：路径、命令白名单是否被踩
4. 输出测试报告（passed/failed + 可复现日志）
5. 失败 → 附失败原因与日志打回执行层（不修改被测代码）
6. 环境异常 → 重试 1 次，仍异常 → env_failure 上报
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 非验证类步骤 | rejected（拒绝并说明） |
| 测试失败 | failed（附失败原因与日志，打回执行层） |
| 权限不足 | env_failure（不重试） |
| 环境异常 | 重试 1 次；仍异常 → env_failure（上报） |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| conductor | 测试报告 | TestReport（passed/failed + repro_log） |
| 执行层 | 失败原因 + 日志 | cases（name + status + log） |

#### 完成标志

测试报告（passed/failed 计数 + 可复现日志）。

---

## FactChecker（事实核验者）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `fact_checker` |
| 层级 | executor（主架构·验证层） |
| 使命 | 核对引用来源 + 复核关键数据 + 核验报告 |
| 服务对象 | router（上游）→ conductor（决定返工或放行） |
| 触发时机 | 收到含事实声明的产出 |
| 交付物 | 核验报告（通过/存疑/证伪 + 依据） |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `citation_check` | 校验引用来源 | low |
| `web_fetch` | 抓取来源链接验证 | low |
| `file_read` | 读文档 | low |
| `code_search` | 搜索代码验证 | low |
| `memory_read` | 读上下文 | low |
| `audit_log` | 写审计日志 | low |

**边界声明**：存疑不放行；证伪打回修改；来源失效重试 1 次仍失败标存疑。

### 执行协议

#### 触发条件
收到含事实声明的产出（action ∈ VERIFY_ACTIONS）。

#### 分步流程

```
1. 逐一核对引用来源是否真实存在
2. 复核关键数字、版本号、人名
3. 来源链接失效 → 用缓存/原站再查 1 次，仍失败标存疑
4. 输出核验报告（通过/存疑/证伪 + 依据）
5. 无法核实 → 标"存疑"不放行
6. 证伪 → 打回修改
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 非核验类步骤 | rejected（拒绝并说明） |
| 来源链接失效 | 重试 1 次；仍失败 → 标存疑 |
| 权限不足 | failed（记录错误） |
| 证伪 | 打回修改（falsified） |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| conductor | 核验报告 | FactReport（passed/suspicious/falsified + items） |
| 执行层 | 证伪清单 | items（claim + source + evidence） |

#### 完成标志

核验报告（通过/存疑/证伪 + 依据，存疑不放行）。

---

## MemoryKeeper（记忆管家）

### 角色规格

| 属性 | 值 |
|---|---|
| 角色名 | `memory_keeper` |
| 层级 | executor（主架构·记忆层） |
| 使命 | 内容分级 + 按类型存取 + 遗忘策略 |
| 服务对象 | 所有角色（写入/检索请求） |
| 触发时机 | 收到写入/检索请求 |
| 交付物 | 写入确认 或 检索结果 |

### 授权清单

| 工具 | 用途 | 风险 |
|---|---|---|
| `memory_read` | 检索记忆 | low |
| `memory_write` | 写入记忆（敏感加密） | low |
| `memory_forget` | 清理过期记忆 | low |
| `audit_log` | 写审计日志 | low |

**边界声明**：敏感信息加密存储；未确认结论不写入长期记忆；检索无结果返回"无记录"不编造。

### 执行协议

#### 触发条件
收到写入/检索/遗忘请求（action ∈ MEMORY_ACTIONS）。

#### 分步流程

```
1. 写入前校验内容分级（会话级 session / 知识级 knowledge / 敏感级 sensitive）
2. 敏感信息 → 加密存储（memory_write 内部 base64 编码）
3. 未确认结论 → 拒绝写入长期记忆（tier=knowledge 时检查标记）
4. 按类型存取：短期=会话隔离；长期=语义检索
5. 执行遗忘策略（过期清理、去重）
6. 检索无结果 → 返回"无记录"而非编造
```

#### 异常处理

| 异常 | 处理路径 |
|---|---|
| 非记忆类步骤 | rejected（拒绝并说明） |
| 未确认结论写入长期 | rejected（拒绝写入） |
| 检索无结果 | done + message="无记录" |
| 权限不足 | failed（记录错误） |

#### 内容分级

| 级别 | 用途 | 存储方式 |
|---|---|---|
| `session` | 会话级（短期） | 会话隔离，会话结束清理 |
| `knowledge` | 知识级（长期） | 语义检索，需确认结论 |
| `sensitive` | 敏感级 | 加密存储（base64 编码） |

#### 交接

| 接收者 | 交付物 | 格式 |
|---|---|---|
| 请求方 | 写入确认 | WriteResult（entry_id + tier + encrypted） |
| 请求方 | 检索结果 | SearchResult（entries 或 "无记录"） |

#### 完成标志

写入确认（entry_id + tier + encrypted）或检索结果（entries 或"无记录"）。
