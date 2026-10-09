# 执行形态 A/B：workflow vs agentic（P3）

> 跑批脚本 `agent_builder/evaluation/mode_ab.py`；原始数据 `docs/reports/mode-ab-runs.json`。
> 规模：4 任务 × 2 档 × 3 次 = **24 次真实运行**（`deepseek-chat`，温度 0.3）。
> 口径优先级：**P0 质量 → P1 成本**（用户定）。
> 任务集为 **v2**：R2/R4 已去掉「先列目录再猜文件名」的噪声（见文末「任务集修订」）；
> v1 原始数据保留在 `docs/reports/mode-ab-runs.before-taskfix.json`。

## 结论摘要（v2）

| 指标 | workflow | agentic | 胜方 |
|---|---:|---:|---|
| 完成率（到达 verifying） | 83%（10/12） | **100%**（12/12） | agentic |
| 执行失败步骤数（合计） | 2 | **1** | agentic |
| 返工数（retries 合计） | 2 | **0** | agentic |
| token 中位数 / 均值 | **754 / 913** | 4866 / 8614 | workflow（~6.5×） |
| 循环轮数中位数（区间） | 0 | 3（1–12） | — |

**判定（质量优先）**：`agentic` 完成率更高、失败更少 → **倾向 `agentic`**；
但成本约为 `workflow` 的 **6.5×**（中位数），且 R1/R4 出现过 9–12 轮的冗长收敛。

逐任务完成率：

| 任务 | workflow | agentic |
|---|---:|---:|
| R1 调研论文 agent | 3/3 | 3/3 |
| R2 只读分析 | 1/3 | **3/3** |
| R3 合并写文件 | 3/3 | 3/3 |
| R4 抄写 TODO 行 | 3/3 | 3/3 |

## 失败根因（**这才是形态差异的真正来源**）

v2 只剩 3 处失败，全部集中在 R2，且**恰好体现了两种形态的本质差别**：

| 档 | 现象 | 报错 |
|---|---|---|
| workflow ×2 | 分解器**臆造**了一个 `sandbox_run` 步骤（需求只是只读分析，不需要跑命令） | `E_VALIDATION: 工具 'sandbox_run' 缺少必填参数: ['command']` |
| agentic ×1 | 模型误用 `data_query` 去读 `.md`（该工具只支持 .csv/.json） | `data_query: 不支持的格式 '.md'` |

关键对比：**同一个「某步失败」，
- `workflow` 的计划是**冻结的** → 那一步失败后无法补救，任务停在 `executing`（rep2 / rep3 两次都这样）；
- `agentic` 每轮重新决策 → 下一轮**自我纠正**（改回 file_read），最终仍收敛到 `verifying`。**

即：agentic 的 100% 不是「不犯错」，而是**错了能改**；代价是更多轮次与 token。

## 任务集修订（v1 → v2）

第一版 R2/R4 要求「列出目录 → 逐个读取 / 搜索」，逼模型在看不到目录内容时**猜文件名**，
结果大量出现 `file_read: 路径不是文件: _mode_ab_out/notes/`、`notes/<file1>.md`、
`file_list: .../notes/*` —— **两档都踩**，噪声盖过形态差异。v2 把文件名全部写死，
每步只涉及显式路径。对照：

| | v1 workflow | v1 agentic | v2 workflow | v2 agentic |
|---|---:|---:|---:|---:|
| 完成率 | 50% | 25% | 83% | 100% |
| 失败步骤 | 7 | 29 | 2 | 1 |
| token 中位数 | 842 | 8477 | 754 | 4866 |

> v1 的「workflow 优」是噪声产物（R2/R4 的猜名字陷阱），**不应采信**；以 v2 为准。

## 局限

- n = 3/档/任务，样本小，只能给方向性判断。
- R2 的 1/3 差异由**分解器臆造步骤**这一个模式造成，换模型 / 换温度未必复现。
- 未测「副结构」档（`sub_arch`）与更多任务类型。

## 复现

```powershell
# 1) 起后端并在前端存好密钥（密钥是内存态，重启即清空）
$env:PYTHONPATH = ".deps"
python -m uvicorn agent_builder.api.app:create_app --factory --port 8000
# 2) 跑批（工作区内临时建 _mode_ab_out/，跑完自动整目录删除）
python -m agent_builder.evaluation.mode_ab --model deepseek-chat --reps 3
```
