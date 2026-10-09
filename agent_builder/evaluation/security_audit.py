"""密钥安全自检 —— 以「控制矩阵」形式输出可重复生成的报告。

用法（在项目根目录）：

    python -m agent_builder.evaluation.security_audit
    python -m agent_builder.evaluation.security_audit --out docs/reports/api-key-security-audit.md

设计约束：
- 不联网（真实连通性探针是可选功能，不参与自检）；
- 不依赖 pytest 执行顺序，同输入必得同输出（不写入时间戳）；
- 每条结论都对应一个可复现的检查（静态断言或进程内对抗探针）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_builder.api.app import create_app
from agent_builder.api.routes import router as api_router
from agent_builder.api.schemas import API_KEY_STATUS_FIELDS, ApiKeyStatusResponse
from agent_builder.api.secrets import ApiKeyStore, reset_api_key_store
from agent_builder.api.security import CLIENT_HEADER_NAME, CLIENT_HEADER_VALUE
from agent_builder.tools.redact import redact_args

# 状态取值：
# enforced = 已有控制且经检查确认有效；gap = 存在缺口；residual = 已明示的残余风险。
STATUS_ENFORCED = "enforced"
STATUS_GAP = "gap"
STATUS_RESIDUAL = "residual"

_LOCAL_HEADERS = {CLIENT_HEADER_NAME: CLIENT_HEADER_VALUE}
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SECRETS_SOURCE = _PROJECT_ROOT / "agent_builder" / "api" / "secrets.py"
_FRONTEND_API = _PROJECT_ROOT / "frontend" / "js" / "api.js"


class _FakeClock:
    """可控时钟：供 TTL 探针使用，避免真实等待。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True, slots=True)
class Finding:
    """一条控制项结论。"""

    threat: str
    control: str
    status: str
    evidence: str


# ── 静态检查 ────────────────────────────────────────────────────


def _cors_options(app: FastAPI) -> dict[str, object]:
    """取出 CORS 中间件的配置项。"""
    for middleware in app.user_middleware:
        kwargs = getattr(middleware, "kwargs", {}) or {}
        if "allow_origins" in kwargs:
            return dict(kwargs)
    return {}


def _sensitive_routes() -> dict[str, bool]:
    """列出敏感端点及其是否挂载 require_local_client。"""
    result: dict[str, bool] = {}
    for route in api_router.routes:
        path = getattr(route, "path", "")
        if not path.startswith(("/settings/api-key", "/workspace/file")):
            continue
        dependant = getattr(route, "dependant", None)
        names = {
            getattr(dep.call, "__name__", "")
            for dep in (getattr(dependant, "dependencies", []) or [])
        }
        methods = sorted(getattr(route, "methods", set()) or set())
        result[f"{methods[0] if methods else '?'} {path}"] = "require_local_client" in names
    return result


def static_findings(app: FastAPI) -> list[Finding]:
    """静态检查：配置与源码层面。"""
    findings: list[Finding] = []
    cors = _cors_options(app)
    origins = list(cors.get("allow_origins", []))
    findings.append(
        Finding(
            threat="T1",
            control="CORS 来源白名单（不含通配）",
            status=STATUS_ENFORCED if origins and "*" not in origins else STATUS_GAP,
            evidence=f"allow_origins={origins}",
        )
    )
    findings.append(
        Finding(
            threat="T1",
            control="CORS 不共享凭据",
            status=STATUS_ENFORCED if cors.get("allow_credentials") is False else STATUS_GAP,
            evidence=f"allow_credentials={cors.get('allow_credentials')}",
        )
    )
    guarded = _sensitive_routes()
    all_guarded = bool(guarded) and all(guarded.values())
    findings.append(
        Finding(
            threat="T2",
            control="敏感端点强制本机客户端标识头",
            status=STATUS_ENFORCED if all_guarded else STATUS_GAP,
            evidence="; ".join(
                f"{route}={'ok' if ok else 'missing'}" for route, ok in sorted(guarded.items())
            ),
        )
    )
    fields = sorted(ApiKeyStatusResponse.model_fields)
    no_plaintext_fields = set(fields) == set(API_KEY_STATUS_FIELDS)
    findings.append(
        Finding(
            threat="T3",
            control="状态响应模型无明文字段",
            status=STATUS_ENFORCED if no_plaintext_fields else STATUS_GAP,
            evidence=f"ApiKeyStatusResponse={fields}",
        )
    )
    source = _SECRETS_SOURCE.read_text(encoding="utf-8")
    leaky_tokens = [t for t in ("write_text", "open(", "pickle", "json.dump") if t in source]
    findings.append(
        Finding(
            threat="T4",
            control="密钥存储模块无任何落盘调用",
            status=STATUS_ENFORCED if not leaky_tokens else STATUS_GAP,
            evidence=f"secrets.py 落盘相关调用={leaky_tokens or '无'}",
        )
    )
    redacted = redact_args({"api_key": "sk-abcdefghijklmnopqrstuvwxyz012345"})
    findings.append(
        Finding(
            threat="T5",
            control="审计参数按键名脱敏",
            status=STATUS_ENFORCED
            if "abcdefghijklmnopqrstuvwxyz" not in json.dumps(redacted)
            else STATUS_GAP,
            evidence=f"redact_args(api_key)={redacted['api_key']}",
        )
    )
    api_js = _FRONTEND_API.read_text(encoding="utf-8")
    uses_ipv4 = "http://127.0.0.1:8000" in api_js
    sends_header = CLIENT_HEADER_NAME in api_js
    findings.append(
        Finding(
            threat="T2",
            control="前端指向 IPv4 且携带标识头",
            status=STATUS_ENFORCED if uses_ipv4 and sends_header else STATUS_GAP,
            evidence=f"API_BASE 用 127.0.0.1={uses_ipv4}; 携带标识头={sends_header}",
        )
    )
    findings.append(
        Finding(
            threat="T7",
            control="传输加密（HTTPS）",
            status=STATUS_RESIDUAL,
            evidence="开发环境为 127.0.0.1 明文 HTTP；正式部署必须置于 HTTPS 之后",
        )
    )
    findings.append(
        Finding(
            threat="T2",
            control="与本机同权限级进程的隔离",
            status=STATUS_RESIDUAL,
            evidence="HTTP 层无法隔离同 OS 用户进程；真正边界是操作系统账号",
        )
    )
    return findings


# ── 进程内对抗探针 ──────────────────────────────────────────────


def dynamic_findings() -> list[Finding]:
    """进程内对抗探针：模拟真实利用路径，逐条判定是否被阻断。"""
    findings: list[Finding] = []
    reset_api_key_store()
    client = TestClient(create_app())

    cross = client.get(
        "/settings/api-key",
        headers={"Origin": "http://evil.example", **_LOCAL_HEADERS},
    )
    findings.append(
        Finding(
            threat="T1",
            control="恶意网页跨站读密钥状态被阻断",
            status=STATUS_ENFORCED if cross.status_code == 403 else STATUS_GAP,
            evidence=f"GET /settings/api-key (Origin: evil.example) → {cross.status_code}",
        )
    )

    missing = client.get("/settings/api-key")
    findings.append(
        Finding(
            threat="T2",
            control="缺标识头访问密钥端点被阻断",
            status=STATUS_ENFORCED if missing.status_code == 403 else STATUS_GAP,
            evidence=f"GET /settings/api-key (无标识头) → {missing.status_code}",
        )
    )

    probe_key = "sk-audit-0123456789abcdef0123456789abcdef"
    saved = client.post(
        "/settings/api-key",
        json={"api_key": probe_key},
        headers=_LOCAL_HEADERS,
    )
    no_echo = probe_key not in saved.text
    findings.append(
        Finding(
            threat="T3",
            control="保存响应不回显明文",
            status=STATUS_ENFORCED if no_echo and saved.status_code == 200 else STATUS_GAP,
            evidence=f"POST 响应体字段={sorted(saved.json())}; 含明文={not no_echo}",
        )
    )

    leaked = probe_key in client.get("/settings/api-key", headers=_LOCAL_HEADERS).text
    findings.append(
        Finding(
            threat="T3",
            control="查询响应不回显明文",
            status=STATUS_ENFORCED if not leaked else STATUS_GAP,
            evidence=f"GET 响应体含明文={leaked}",
        )
    )

    reset_api_key_store()  # 模拟进程重启：内存态丢弃
    after_restart = client.get("/settings/api-key", headers=_LOCAL_HEADERS).json()
    findings.append(
        Finding(
            threat="T4",
            control="重建存储单例后密钥失效（不落盘）",
            status=STATUS_ENFORCED if after_restart["configured"] is False else STATUS_GAP,
            evidence=f"重启后 configured={after_restart['configured']}",
        )
    )

    invalid_keys = ["", "   ", "sk-abc def", "sk-abc\ndef", "sk-abc\x00def", "sk-abc\tdef"]
    rejected = all(
        client.post(
            "/settings/api-key", json={"api_key": bad}, headers=_LOCAL_HEADERS
        ).status_code
        == 400
        for bad in invalid_keys
    )
    reset_api_key_store()
    findings.append(
        Finding(
            threat="T6",
            control="非法密钥（空/含空白/含控制字符）被拒",
            status=STATUS_ENFORCED if rejected else STATUS_GAP,
            evidence=f"提交 {len(invalid_keys)} 个非法样本，全部返回 400={rejected}",
        )
    )

    # T4：TTL 到期即失效（注入假时钟，不等待真实时间）
    clock = _FakeClock()
    ttl_store = ApiKeyStore(clock=clock)
    ttl_store.set_key("sk-audit-ttl-0123456789abcdef", ttl_s=10)
    alive_before = ttl_store.is_configured
    clock.advance(10)
    expired_ok = (
        alive_before
        and ttl_store.is_expired
        and not ttl_store.is_configured
        and ttl_store.get() is None
    )
    findings.append(
        Finding(
            threat="T4",
            control="TTL 到期即失效并清除明文",
            status=STATUS_ENFORCED if expired_ok else STATUS_GAP,
            evidence=(
                f"ttl=10s：到期前 configured={alive_before}，"
                f"到期后 configured={ttl_store.is_configured}，get()={ttl_store.get()}"
            ),
        )
    )

    # T2：轮换端点同样受本机客户端标识头保护
    rotate_unguarded = client.post(
        "/settings/api-key/rotate", json={"api_key": "sk-audit-rotate-0123456789abcdef"}
    )
    findings.append(
        Finding(
            threat="T2",
            control="轮换端点受本机客户端标识头保护",
            status=STATUS_ENFORCED if rotate_unguarded.status_code == 403 else STATUS_GAP,
            evidence=f"POST /settings/api-key/rotate (无标识头) → {rotate_unguarded.status_code}",
        )
    )

    # T2：轮换要求存在旧密钥，且失败不改变现状
    reset_api_key_store()
    no_key_rotate = client.post(
        "/settings/api-key/rotate",
        json={"api_key": "sk-audit-rotate-0123456789abcdef"},
        headers=_LOCAL_HEADERS,
    )
    findings.append(
        Finding(
            threat="T2",
            control="无旧密钥时轮换被拒（语义为「替换」而非「首次设置」）",
            status=STATUS_ENFORCED if no_key_rotate.status_code == 400 else STATUS_GAP,
            evidence=f"空存储轮换 → {no_key_rotate.status_code}：{no_key_rotate.json().get('detail')}",
        )
    )
    reset_api_key_store()
    return findings


def collect() -> list[Finding]:
    """汇总全部结论（顺序稳定，保证同输入同输出）。"""
    findings = static_findings(create_app()) + dynamic_findings()
    return sorted(findings, key=lambda f: (f.threat, f.control))


# ── 报告渲染 ────────────────────────────────────────────────────


def render_markdown(findings: list[Finding]) -> str:
    """渲染 Markdown 报告。"""
    total = len(findings)
    enforced = sum(1 for f in findings if f.status == STATUS_ENFORCED)
    gaps = [f for f in findings if f.status == STATUS_GAP]
    residual = [f for f in findings if f.status == STATUS_RESIDUAL]
    lines = [
        "# 密钥安全自检报告",
        "",
        "> 本报告由 `python -m agent_builder.evaluation.security_audit` 生成，同一输入必得同一输出。",
        "",
        "## 结论摘要",
        "",
        f"- 检查项合计：**{total}**",
        f"- 已生效控制：**{enforced}**",
        f"- 缺口：**{len(gaps)}**",
        f"- 明示残余风险：**{len(residual)}**",
        "",
        "## 控制矩阵",
        "",
        "| 威胁 | 控制项 | 状态 | 依据 |",
        "|---|---|---|---|",
    ]
    label = {STATUS_ENFORCED: "已生效", STATUS_GAP: "缺口", STATUS_RESIDUAL: "残余风险"}
    for finding in findings:
        lines.append(
            f"| {finding.threat} | {finding.control} | {label[finding.status]} | {finding.evidence} |"
        )
    lines += [
        "",
        "## 已实施的修复",
        "",
        (
            "1. **CORS 收紧**：`allow_origins` 由 `[\"*\"]` 改为本机前端白名单，"
            "`allow_credentials` 置为 `False`（与通配组合本身不合规）。"
        ),
        (
            "2. **敏感端点本机鉴权**：`/settings/api-key*` 与 `/workspace/file*` "
            "挂 `require_local_client`，必须携带 `X-Agent-Builder-Client: web`；"
            "叠加 `OriginGuardMiddleware` 拦截带非白名单 `Origin` 的请求。"
        ),
        (
            "3. **非可逆指纹**：状态响应新增 `fingerprint = sha256(key)[:8]`，"
            "用于核对「是不是同一把钥匙」，不可反推原文。"
        ),
        (
            "4. **可选真实校验**：`POST /settings/api-key?verify=true` 对 "
            "`{base_url}/models` 做一次轻量探针，失败返回 400 且"
            "**不落库、不改变已有密钥**。"
        ),
        (
            "5. **TTL（到期自动失效）**：`POST /settings/api-key` 可带 `ttl_s`（>0，"
            "上限 30 天）；到期第一时刻即清除明文、`configured=false`、`get()` 返回 null，"
            "并保留掩码/指纹用于提示是哪把钥匙过期了。"
        ),
        (
            "6. **密钥轮换**：`POST /settings/api-key/rotate` 用新密钥替换旧密钥，"
            "要求当前存在有效密钥（否则 400），响应回带旧指纹 `rotated_from_fingerprint` "
            "与累计轮换次数；格式/TTL/探针任一失败都不改变现有密钥。"
        ),
        "7. **文档化**：dev 为明文 HTTP，正式部署必须 HTTPS。",
        "",
        "## 已知限制与残余风险",
        "",
        "- 密钥及其 TTL / 轮换元数据仅存进程内存，重启后端即失效，需重新输入。",
        (
            "- 到期清理只保证明文从运行时存储移除；"
            "进程内存快照（core dump / 调试器）不在本层防范范围内。"
        ),
        "- 与本机后端同 OS 用户/同权限级的本地进程仍可伪造标识头；真正的隔离边界是操作系统账号。",
        "- 开发环境为 `127.0.0.1` 明文 HTTP；生产部署必须在 HTTPS 之后，并叠加真实鉴权。",
        "",
        "## 复现命令",
        "",
        "```powershell",
        "& \"$env:USERPROFILE\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" -m pytest tests/test_api_key_security.py tests/test_api_key_ttl_rotation.py -q",
        "& \"$env:USERPROFILE\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" -m agent_builder.evaluation.security_audit",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：生成报告。"""
    parser = argparse.ArgumentParser(description="密钥安全自检")
    parser.add_argument(
        "--out",
        default="docs/reports/api-key-security-audit.md",
        help="Markdown 报告输出路径（默认 docs/reports/api-key-security-audit.md）",
    )
    parser.add_argument(
        "--json",
        default="docs/reports/api-key-security-audit.json",
        help="JSON 报告输出路径（默认 docs/reports/api-key-security-audit.json）",
    )
    args = parser.parse_args(argv)

    findings = collect()
    markdown_path = _PROJECT_ROOT / args.out
    json_path = _PROJECT_ROOT / args.json
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_markdown(findings), encoding="utf-8")
    json_path.write_text(
        json.dumps([asdict(f) for f in findings], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    gaps = [f for f in findings if f.status == STATUS_GAP]
    print(f"检查项 {len(findings)} 条，缺口 {len(gaps)} 条")
    print(f"Markdown: {markdown_path}")
    print(f"JSON: {json_path}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
