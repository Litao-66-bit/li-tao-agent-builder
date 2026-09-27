# 工具清单（Tools）

本文件记录 `agent_builder/tools/impl/` 下所有已注册工具的规格与用法。
新增工具时在此追加一条；删除工具时同步移除。

## 通用执行流程

所有工具调用统一走 `ToolRegistry.execute(gatekeeper, tool_call)`：

1. **门卫校验**（`ToolGatekeeper.check`）：角色权限 / 沙箱路径 / 高风险审批 → 不通过抛 `E_PERMISSION`
2. **注册表分发**：按 `tool_call.tool` 查 `ToolSpec` + 实现函数
3. **超时兜底**：按 `ToolSpec.timeout_s` 线程池执行，超时抛 `E_TIMEOUT`
4. **结果回填**：`tool_call.result = impl(**args)`

安全逻辑（权限 / 沙箱 / 审批）完全在门卫，工具实现只做纯逻辑。

---

## file_read

| 项 | 值 |
|----|----|
| **名称** | `file_read` |
| **用途** | 读取白名单目录内指定文件的文本内容（UTF-8） |
| **风险等级** | `low` |
| **成本带** | `low` |
| **超时** | 10s |
| **授权角色** | `operator` |
| **审批要求** | 否 |
| **实现文件** | `agent_builder/tools/impl/file_read.py` |

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | 是 | 要读取的文件路径（必须在白名单 `WORKSPACE_DIR` 内） |

### 输出

文件文本内容（字符串）。超过 `MAX_OUTPUT_CHARS`（200,000 字符）时截断并附 `…[已截断，原文 N 字符]` 标记。

### 失败映射

| 场景 | 错误码 | 说明 |
|------|--------|------|
| 路径为空 / 缺少 path | `E_PERMISSION` | 门卫沙箱校验拦截 |
| 路径超出白名单目录 | `E_PERMISSION` | 门卫 `realpath` 校验拦截 |
| 角色未授权 / 工具未在白名单 | `E_PERMISSION` | 门卫权限矩阵拦截 |
| 文件不存在 | `E_VALIDATION` | 实现层校验 |
| 路径不是文件（目录等） | `E_VALIDATION` | 实现层校验 |
| 非 UTF-8 文本（二进制） | `E_TOOL` | `UnicodeDecodeError` 映射 |
| 读取 IO 失败 | `E_TOOL` | `OSError` 映射 |
| 执行超时 | `E_TIMEOUT` | 注册表线程池兜底 |

### 示例调用

```python
from pathlib import Path
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools import ToolGatekeeper, registry

workspace = Path("/your/workspace")
perms = {"operator": RolePerm(role="operator", allowed_tools=["file_read"])}
gk = ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-demo")

call = ToolCall(
    audit_id="a-1",
    role="operator",
    tool="file_read",
    args={"path": str(workspace / "notes.txt")},
)
registry.execute(gk, call)
print(call.result)  # 文件内容
```

---

## file_list

| 项 | 值 |
|----|----|
| **名称** | `file_list` |
| **用途** | 列出白名单目录内指定路径下的文件和子目录 |
| **风险等级** | `low` |
| **成本带** | `low` |
| **超时** | 10s |
| **授权角色** | `operator` |
| **审批要求** | 否 |
| **实现文件** | `agent_builder/tools/impl/file_list.py` |

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | 是 | 要列出的目录路径（必须在白名单 `WORKSPACE_DIR` 内） |

### 输出

每行一个条目，格式 `[DIR]  name/` 或 `[FILE] name (N bytes)`。目录在前、文件在后，各自按名称排序。空目录返回空字符串。超过 `MAX_ENTRIES`（500 条目）时截断并附 `…[已截断，共 N 条目，仅显示前 M 条]` 标记。

### 失败映射

| 场景 | 错误码 | 说明 |
|------|--------|------|
| 路径为空 / 缺少 path | `E_PERMISSION` | 门卫沙箱校验拦截 |
| 路径超出白名单目录 | `E_PERMISSION` | 门卫 `realpath` 校验拦截 |
| 角色未授权 / 工具未在白名单 | `E_PERMISSION` | 门卫权限矩阵拦截 |
| 路径不存在 | `E_VALIDATION` | 实现层校验 |
| 路径不是目录（文件等） | `E_VALIDATION` | 实现层校验 |
| 列目录 IO 失败 | `E_TOOL` | `OSError` 映射 |
| 执行超时 | `E_TIMEOUT` | 注册表线程池兜底 |

### 示例调用

```python
from pathlib import Path
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools import ToolGatekeeper, registry

workspace = Path("/your/workspace")
perms = {"operator": RolePerm(role="operator", allowed_tools=["file_list"])}
gk = ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-demo")

call = ToolCall(
    audit_id="a-1",
    role="operator",
    tool="file_list",
    args={"path": str(workspace)},
)
registry.execute(gk, call)
print(call.result)
# [DIR]  subdir/
# [FILE] a.txt (3 bytes)
# [FILE] b.txt (3 bytes)
```
