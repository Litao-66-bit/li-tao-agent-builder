# 密钥安全自检报告

> 本报告由 `python -m agent_builder.evaluation.security_audit` 生成，同一输入必得同一输出。

## 结论摘要

- 检查项合计：**18**
- 已生效控制：**16**
- 缺口：**0**
- 明示残余风险：**2**

## 控制矩阵

| 威胁 | 控制项 | 状态 | 依据 |
|---|---|---|---|
| T1 | CORS 不共享凭据 | 已生效 | allow_credentials=False |
| T1 | CORS 来源白名单（不含通配） | 已生效 | allow_origins=['http://127.0.0.1:8080', 'http://localhost:8080'] |
| T1 | 恶意网页跨站读密钥状态被阻断 | 已生效 | GET /settings/api-key (Origin: evil.example) → 403 |
| T2 | 与本机同权限级进程的隔离 | 残余风险 | HTTP 层无法隔离同 OS 用户进程；真正边界是操作系统账号 |
| T2 | 前端指向 IPv4 且携带标识头 | 已生效 | API_BASE 用 127.0.0.1=True; 携带标识头=True |
| T2 | 敏感端点强制本机客户端标识头 | 已生效 | DELETE /settings/api-key=ok; GET /settings/api-key=ok; GET /workspace/file=ok; GET /workspace/files=ok; POST /settings/api-key=ok; POST /settings/api-key/rotate=ok; PUT /workspace/file=ok |
| T2 | 无旧密钥时轮换被拒（语义为「替换」而非「首次设置」） | 已生效 | 空存储轮换 → 400：当前没有可轮换的密钥；请先用 POST /settings/api-key 设置 |
| T2 | 缺标识头访问密钥端点被阻断 | 已生效 | GET /settings/api-key (无标识头) → 403 |
| T2 | 轮换端点受本机客户端标识头保护 | 已生效 | POST /settings/api-key/rotate (无标识头) → 403 |
| T3 | 保存响应不回显明文 | 已生效 | POST 响应体字段=['configured', 'expired', 'expires_at', 'fingerprint', 'masked', 'remaining_s', 'rotated_at', 'rotated_from_fingerprint', 'rotation_count']; 含明文=False |
| T3 | 查询响应不回显明文 | 已生效 | GET 响应体含明文=False |
| T3 | 状态响应模型无明文字段 | 已生效 | ApiKeyStatusResponse=['configured', 'expired', 'expires_at', 'fingerprint', 'masked', 'remaining_s', 'rotated_at', 'rotated_from_fingerprint', 'rotation_count'] |
| T4 | TTL 到期即失效并清除明文 | 已生效 | ttl=10s：到期前 configured=True，到期后 configured=False，get()=None |
| T4 | 密钥存储模块无任何落盘调用 | 已生效 | secrets.py 落盘相关调用=无 |
| T4 | 重建存储单例后密钥失效（不落盘） | 已生效 | 重启后 configured=False |
| T5 | 审计参数按键名脱敏 | 已生效 | redact_args(api_key)=sk-a***[redacted] |
| T6 | 非法密钥（空/含空白/含控制字符）被拒 | 已生效 | 提交 6 个非法样本，全部返回 400=True |
| T7 | 传输加密（HTTPS） | 残余风险 | 开发环境为 127.0.0.1 明文 HTTP；正式部署必须置于 HTTPS 之后 |

## 已实施的修复

1. **CORS 收紧**：`allow_origins` 由 `["*"]` 改为本机前端白名单，`allow_credentials` 置为 `False`（与通配组合本身不合规）。
2. **敏感端点本机鉴权**：`/settings/api-key*` 与 `/workspace/file*` 挂 `require_local_client`，必须携带 `X-Agent-Builder-Client: web`；叠加 `OriginGuardMiddleware` 拦截带非白名单 `Origin` 的请求。
3. **非可逆指纹**：状态响应新增 `fingerprint = sha256(key)[:8]`，用于核对「是不是同一把钥匙」，不可反推原文。
4. **可选真实校验**：`POST /settings/api-key?verify=true` 对 `{base_url}/models` 做一次轻量探针，失败返回 400 且**不落库、不改变已有密钥**。
5. **TTL（到期自动失效）**：`POST /settings/api-key` 可带 `ttl_s`（>0，上限 30 天）；到期第一时刻即清除明文、`configured=false`、`get()` 返回 null，并保留掩码/指纹用于提示是哪把钥匙过期了。
6. **密钥轮换**：`POST /settings/api-key/rotate` 用新密钥替换旧密钥，要求当前存在有效密钥（否则 400），响应回带旧指纹 `rotated_from_fingerprint` 与累计轮换次数；格式/TTL/探针任一失败都不改变现有密钥。
7. **文档化**：dev 为明文 HTTP，正式部署必须 HTTPS。

## 已知限制与残余风险

- 密钥及其 TTL / 轮换元数据仅存进程内存，重启后端即失效，需重新输入。
- 到期清理只保证明文从运行时存储移除；进程内存快照（core dump / 调试器）不在本层防范范围内。
- 与本机后端同 OS 用户/同权限级的本地进程仍可伪造标识头；真正的隔离边界是操作系统账号。
- 开发环境为 `127.0.0.1` 明文 HTTP；生产部署必须在 HTTPS 之后，并叠加真实鉴权。

## 复现命令

```powershell
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m pytest tests/test_api_key_security.py tests/test_api_key_ttl_rotation.py -q
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -m agent_builder.evaluation.security_audit
```
