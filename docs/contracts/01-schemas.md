# 01 · 数据 Schema

6 个核心 JSON Schema，是全部角色间通信与工具参数校验的基准。编码时建议用 Pydantic 模型一一对应。

## 1. Step（步骤）

```json
{
  "id": "step-001",
  "action": "web_search | file_write | code_gen | data_query | ...",
  "inputs": { "query": "..." },
  "depends_on": ["step-000"],
  "status": "pending | running | done | failed | skipped",
  "error_code": null,
  "assignee": null
}
```

约束：`depends_on` 中的 id 必须存在；`status` 由执行层更新，`assignee` 由路由者填写。

## 2. Plan（执行计划）

```json
{
  "task_id": "t-20260926-001",
  "order": ["step-001", "step-002", "step-003"],
  "parallel_groups": [["step-002", "step-003"]],
  "fallback": { "step-002": { "action": "skip", "note": "非关键步骤" } },
  "confirmed_by_user": false
}
```

约束：`order` 与 `parallel_groups` 不得违反 Step 的 `depends_on`；`confirmed_by_user` 为 false 时路由者不得启动执行。

## 3. ToolCall（工具调用 · 门卫审计记录）

```json
{
  "audit_id": "ac-0001",
  "role": "code_worker",
  "tool": "file_write",
  "args": { "path": "/workspace/agent.py", "content": "..." },
  "status": "approved | executed | denied",
  "result": null,
  "approval": { "required": true, "granted_by": "user", "ts": "2026-09-26T10:00:00+08:00" },
  "ts": "2026-09-26T09:59:58+08:00"
}
```

约束：所有工具调用必须落此记录（只追加、不可篡改）；`approval.required=true` 且未 `granted_by` 时门卫必须拒绝。

## 4. TaskState（任务状态快照）

```json
{
  "task_id": "t-20260926-001",
  "status": "received | planning | awaiting_confirm | executing | verifying | reworking | delivering | delivered | interrupted | failed",
  "current_stage": "planning",
  "plan": null,
  "retry_count": 0,
  "interrupted": { "resume_point": "step-002", "reason": "user_stop" },
  "created_at": "...",
  "updated_at": "..."
}
```

约束：`status` 转换必须符合 `02-state-machine.md`；`retry_count` 上限 2，超限转 `failed`。

## 5. ChangeProposal（变更提案 · 副架构）

```json
{
  "proposal_id": "cp-017",
  "target_module": "planner",
  "change_desc": "任务拆解由顺序执行改为并行分组，预计耗时 -30%",
  "diff_preview": "+18 -5 planner.py",
  "impacted_files": ["planner.py", "config.json"],
  "risk": "low | mid | high",
  "verification_plan": "回归 tests/test_planner.py + 3 组新用例",
  "cost_estimate": "+0.05 元/次",
  "status": "proposed | approved | rejected | applied | rolled_back",
  "approved_by": null,
  "ts": "..."
}
```

约束：`status=applied` 前，看门人不得触碰任何代码；`rolled_back` 必须保留上一版本号。

## 6. RolePerm（角色 → 工具授权）

```json
{
  "role": "code_worker",
  "allowed_tools": ["file_read", "file_list", "code_search", "file_write", "sandbox_run"],
  "high_risk_tools": ["file_write"],
  "notes": "覆盖已有文件需审批"
}
```

约束：门卫按此表放行；`high_risk_tools` 触发审批门；未列出的工具一律拒绝。
