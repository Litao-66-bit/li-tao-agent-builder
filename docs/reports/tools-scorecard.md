# 工具评分报告

> 由 `python -m agent_builder.evaluation.tool_report` 生成；清单取自 `registry.list_tools()`（运行时真源），同输入必得同输出。

## 结论摘要

- 工具总数：**27**
- 满分（30/30）：**27**
- 命中安全项的工具：**0**
- 均分：**30.00 / 30**

## 评分总表

| 工具 | 元数据 | Schema | 最小权限 | 风险分级 | 错误语义 | 测试 | 总分 | 建议动作 |
|---|---|---|---|---|---|---|---|---|
| `approval_request` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `audit_log` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `change_notify` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `citation_check` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `code_search` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `config_read` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `council_build_minutes` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `council_check_opinion` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `data_query` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `diff_preview` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `file_delete` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `file_edit` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `file_list` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `file_read` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `file_write` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `git_commit` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `git_log` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `memory_forget` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `memory_read` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `memory_write` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `metric_collect` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `plan_validate` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `rollback` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `sandbox_run` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `test_run` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `web_fetch` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |
| `web_search` | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | **30** | 无 |

## 安全项明细（风险等级错标 / 越权声明 / 悬空工具）

- 无

## 文档漂移（docs/tools.md ↔ registry）

- 未收录进文档的工具：无
- 角色声明与 permissions.py 不一致：
  - 无

## 维度判定口径

| 维度 | 判据 |
|---|---|
| 元数据完整性 | ToolSpec 七要素齐全且取值合法（name/description/parameters/risk_level/timeout_s/cost_band/allowed_roles） |
| Schema 质量 | `type=object` + `properties` 非空 + `required` 存在 + `additionalProperties=false` + 每个属性有 description |
| 最小权限 | 无越权声明（`allowed_roles` ⊆ `permissions.py` 实授）、无悬空工具；「实授但未声明」仅记备注 |
| 风险分级正确 | 写/提交/回滚类必须非 low 且进 `high_risk_tools`；`risk_level=high` 必须进审批门 |
| 错误语义 | 走 `validation_error`/`tool_error`/`permission_error`，无兜底吞异常，有 Raises 文档 |
| 测试覆盖 | `tests/test_tools_<name>.py` 覆盖成功/失败，且含数值参数时覆盖边界 |

## 真源与边界

- **执行真源**：`agent_builder/tools/permissions.py` 的 `allowed_tools` / `high_risk_tools`（门卫据此放行与要求审批）。
- **工具清单真源**：`registry.list_tools()`。
- **元数据真源**：各 impl 模块内的 `ToolSpec`；`spec.risk_level` 与 `spec.allowed_roles` 是声明性元数据，不参与运行时判定，因此二者与执行真源的一致性靠本报告与回归测试守住。

## 复现命令

```powershell
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m pytest tests/test_evaluation_tools.py -q
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m agent_builder.evaluation.tool_report
```
