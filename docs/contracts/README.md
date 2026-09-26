# 接口契约层（Contracts）

设计 → 编码的桥。本文档目录定义 Agent Builder 的机器可读契约，是 Conductor 状态机、角色通信、工具门卫、副架构变更流程的编码依据。

## 文档清单

| 文件 | 内容 | 编码对应 |
| --- | --- | --- |
| `01-schemas.md` | 6 个核心 JSON Schema（Step/Plan/ToolCall/TaskState/ChangeProposal/RolePerm） | `agent_builder/contracts/` |
| `02-state-machine.md` | 任务生命周期状态机（状态 + 转换 + 触发条件） | `agent_builder/core/conductor.py` |
| `03-message-protocol.md` | 角色间结构化消息类型与字段 | `agent_builder/core/messages.py` |
| `04-error-codes.md` | 错误码体系（6 类 + 处理策略） | `agent_builder/core/errors.py` |

## 设计原则

1. **接口先行**：角色之间只通过本目录定义的消息与 Schema 通信，禁止自由聊天。
2. **单一出口**：所有工具调用统一走工具门卫（见 `01-schemas.md` 的 ToolCall）。
3. **最小权限**：角色→工具授权由 RolePerm 驱动（见 `01-schemas.md`）。
4. **可回滚**：TaskState 携带 `resume_point`，中断可恢复；ChangeProposal 全程可追溯。
