# Agent（角色）评分报告

> 由 `python -m agent_builder.evaluation.scorecards` 生成；角色集合取「执行协议文档 ∪ `permissions.py` ∪ `roles/`」三方并集，同输入必得同输出。

## 结论摘要

- 有实现模块的角色：**17**（满分 17 个）
- 仅权限层角色（无独立模块）：**3** —— 只参与契约/权限两个维度
- 命中安全项的角色：**0**
- 实现类角色均分：**30.00 / 30**

## 评分总表（有实现模块的角色）

| 角色 | 类型 | 契约符合度 | 权限最小化 | 失败语义 | 可观测性 | 测试覆盖 | 依赖清晰度 | 总分 | 建议动作 |
|---|---|---|---|---|---|---|---|---|---|
| `auditor` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `code_worker` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `conductor` | orchestrator | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `data_analyst` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `decomposer` | orchestrator | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `doc_worker` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `fact_checker` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `gatekeeper` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `historian` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `impact_analyzer` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `memory_keeper` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `proposer` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `router` | orchestrator | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `scheduler` | orchestrator | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `searcher` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `summarizer` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `test_runner` | executor | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |

## 权限层角色（无独立实现模块，仅评契约与权限）

| 角色 | 契约符合度 | 权限最小化 | 小计 | 说明 |
|---|---|---|---|---|
| `memory_manager` | 5/5 | 5/5 | **10/10** | 无 |
| `operator` | 5/5 | 5/5 | **10/10** | 无 |
| `sub_architect` | 5/5 | 5/5 | **10/10** | 无 |

## 安全项明细（契约违反 / 权限过宽 / 跨层依赖）

- 无

## 三方一致性（文档 / 权限 / 实现）

- 文档有但无实现模块（未声明权限层）：无
- 权限有但无实现模块（未声明权限层）：无
- 权限有但文档无章节：无
- 权限无但文档有章节（外部实现）：['tool_guardian']
- 实现模块有但文档无章节：无
- 已声明的权限层角色（无独立模块，设计如此）：['memory_manager', 'operator', 'sub_architect']

## 维度判定口径

| 维度 | 判据 |
|---|---|
| 契约符合度 | `docs/execution-protocols.md` 有该角色章节；「授权清单」工具集合 == `permissions.py` 实授；有边界声明与异常处理表 |
| 权限最小化 | 边界声明禁止的动作（写文件/提交代码/执行命令）不得被授权；实授写类工具必须挂 `high_risk_tools`；角色不得悬空 |
| 失败语义 | 执行类：结果声明 pending/failed、代码确产降级结果、含 error 字段；编排类：pending_questions/fallback/FAILED 降级出口 + 契约异常 + 异常处理表 |
| 可观测性 | 执行类：结构化结果类型、字段 ≥ 3、含 status、含 error；编排类：结构化类型 + 契约模型（TaskState/Plan/Step）+ 交付说明 |
| 测试覆盖 | `tests/test_roles_<role>.py` 存在且含 Functional / Edge 用例类、用例 ≥ 10 |
| 依赖清晰度 | 执行类必须通过 `executor_fn` 注入；所有角色不 import `agent_builder.api/tools/llm`；无模块级私有可变状态 |

## 类型判定

- **executor**：有 `roles/<role>.py` 实现模块，且契约未声明「不执行」。
- **orchestrator**：有实现模块，但契约边界声明含「不执行」（只编排/规划/调度/路由），因此不要求注入执行器，失败语义与可观测性按编排类判据评估。
- **permission_only**：仅存在于 `permissions.py`，无独立实现模块；只评契约与权限两个维度。
- **外部实现**：文档标注 `**已实现**为 <路径>` 的角色（如 ToolGuardian）不参与角色评分，其实现由工具评分报告覆盖。

## 复现命令

```powershell
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m pytest tests/test_evaluation_agents.py -q
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m agent_builder.evaluation.scorecards
```
