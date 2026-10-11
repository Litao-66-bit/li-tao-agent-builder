"""可选：把 API 密钥**以收紧权限落盘**，让后端重启后不再静默失效（卡 47）。

## 为什么要有它

实测（2026-10-11）踩了 8 次：后端一重启，内存里的密钥就没了，
`GET /settings/api-key` 变 ``configured:false``，下一次 ``/run`` 直接报 ``E_MODEL``
（"未配置可用的模型密钥"）。用户完全看不出发生了什么 —— 这是典型的**静默失效**。

## 为什么默认**关闭**（这很重要）

`agent_builder/api/secrets.py` 的安全约束第 1 条是"密钥只保存在进程内存中，**不写入磁盘**"，
并且有对抗性用例在守（`test_api_key_security.py::TestNotPersisted`：
"写入密钥后全盘搜索无命中"）。**把"落盘"直接改成默认行为会推翻那条防线**，
属于安全策略变更，不该由一张功能卡顺手做掉。

所以本模块的口径是：

- **默认关闭**（``AGENT_BUILDER_PERSIST_API_KEY`` 未设置 / ``0``）→ 行为与今天完全一致：只驻内存；
- **显式打开**（环境变量 = ``1``，或用户在前端点「记住密钥」）→ 才落盘，且：
  1. 落点在工作区**之外**的 ``<用户主目录>/.agent-builder/api_key.json``，
     由 ``.gitignore`` 与目录权限共同保护，绝不进仓库、绝不进工作区产物树；
  2. 文件权限收紧到 **0600**（Windows 上退化为"仅当前用户可访问"的 ACL，尽力而为）；
  3. **原子写**（临时文件 + ``replace``），不出现"写了一半"的密钥文件；
  4. **绝不写日志**：日志只记路径与动作，永不记密钥内容；
  5. 回显只走 ``masked`` / ``fingerprint``（复用 ``validate_api_key`` 派生），文件里**没有**明文回显路径。

## 与 `project_log` / `approval_store` 的区别

那两个落**工作区**（属于项目数据）；这个落**用户主目录**（属于本机凭据），
因为它与具体项目无关，且不该出现在工作区产物树里被 agent 读到。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 打开"密钥落盘"的环境变量（值 "1" / "true" / "yes" → 打开）。
PERSIST_ENV = "AGENT_BUILDER_PERSIST_API_KEY"

#: 落点目录 / 文件名（工作区之外的本机凭据目录）。
DEFAULT_SECRET_DIRNAME = ".agent-builder"
DEFAULT_SECRET_FILE = "api_key.json"

#: 文件模式：仅属主可读写。
_SECRET_FILE_MODE = 0o600


def persist_enabled() -> bool:
    """是否允许把密钥落盘（默认关闭；见模块文档）。"""
    raw = (os.environ.get(PERSIST_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def secret_dir() -> Path:
    """密钥文件所在目录。

    可用 ``AGENT_BUILDER_SECRET_DIR`` 覆盖（测试隔离用）。
    """
    override = os.environ.get("AGENT_BUILDER_SECRET_DIR")
    if override:
        return Path(override)
    return Path.home() / DEFAULT_SECRET_DIRNAME


def secret_path() -> Path:
    """密钥文件路径。"""
    return secret_dir() / DEFAULT_SECRET_FILE


@dataclass(frozen=True, slots=True)
class StoredSecret:
    """从磁盘读回的密钥记录。"""

    api_key: str
    fingerprint: str = ""


def _restrict_permissions(path: Path) -> None:
    """把文件权限收紧到"仅属主可访问"（尽力而为，失败不影响可用性）。

    POSIX：``chmod 0o600``。Windows：``os.chmod`` 只能翻转只读位，
    所以额外用 ACL（``icacls``）把继承权限断开、只留当前用户 —— 不引入第三方依赖。
    """
    try:
        os.chmod(path, _SECRET_FILE_MODE)
    except OSError:
        logger.warning("密钥文件权限收紧失败（忽略）：%s", path, exc_info=True)
        return
    if os.name != "nt":
        return
    try:  # pragma: no cover - 平台相关，Windows 上才有意义
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("密钥文件 ACL 收紧失败（忽略）：%s", path, exc_info=True)


def load_secret() -> StoredSecret | None:
    """读回落盘的密钥；文件不存在 / 读坏 / 形状不对都返回 None（绝不抛）。

    **不检查 ``persist_enabled()``** —— 那是调用方（启动流程）的判定；
    模块本身只负责"文件里有什么"。否则关掉开关后连"读"都不肯做，无法给出准确状态。
    """
    path = secret_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("密钥文件不是合法 JSON（忽略）：%s", path)
        return None
    if not isinstance(payload, dict):
        return None
    key = payload.get("api_key")
    if not isinstance(key, str) or not key.strip():
        return None
    print_ = payload.get("fingerprint")
    return StoredSecret(
        api_key=key.strip(),
        fingerprint=print_ if isinstance(print_, str) else "",
    )


def save_secret(api_key: str, *, fingerprint: str = "") -> bool:
    """把密钥**原子**写入收紧权限的文件；返回是否成功。失败只记日志。

    ⚠️ 调用方必须先 ``validate_api_key()`` 校验过；本函数不做格式校验（避免出现两套口径）。
    **日志只记路径，绝不记密钥内容。**
    """
    path = secret_path()
    body = json.dumps(
        {"api_key": api_key, "fingerprint": fingerprint}, ensure_ascii=False, indent=2
    ) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(path.parent)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8", newline="")
        _restrict_permissions(tmp)
        tmp.replace(path)
    except OSError:
        logger.warning("密钥落盘失败（忽略）：%s", path, exc_info=True)
        return False
    logger.info("密钥已落盘（内容不记录）：%s", path)
    return True


def delete_secret() -> None:
    """删除落盘的密钥（文件不存在也算成功）。日志只记路径。"""
    try:
        secret_path().unlink()
    except OSError:
        return
    logger.info("已删除落盘密钥：%s", secret_path())


__all__ = [
    "DEFAULT_SECRET_DIRNAME",
    "DEFAULT_SECRET_FILE",
    "PERSIST_ENV",
    "StoredSecret",
    "delete_secret",
    "load_secret",
    "persist_enabled",
    "save_secret",
    "secret_dir",
    "secret_path",
]
