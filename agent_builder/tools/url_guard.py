"""URL 安全校验 —— 防 SSRF（Server-Side Request Forgery）。

安全边界：web_fetch / web_search 调用前由门卫 _check_url_safety 调用本模块。
职责：
1. scheme 仅允许 http / https；
2. hostname 解析为 IP 后禁止私有/保留网段（RFC1918、链路本地、环回、组播、未指定）；
3. 阻断云元数据端点 169.254.169.254（防凭证泄漏）；
4. 可选域名白名单（默认空 = 允许所有公共域名；配置后仅放行白名单后缀）。

本模块只做纯校验，不发起网络请求。域名解析依赖 socket.getaddrinfo，
生产环境可在调用方预先配置 DNS 缓存以减少重复解析。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# 云元数据端点：必须硬阻断（IMDSv1 凭证可被直接 GET）。
METADATA_HOST = "169.254.169.254"

# 允许的 scheme 白名单。
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# 默认域名白名单后缀：空 = 允许所有公共域名（仍需通过 IP 私有网段校验）。
# 配置示例：(".example.com", ".trusted.io") 表示仅放行这些后缀的域名。
DEFAULT_DOMAIN_ALLOWLIST: tuple[str, ...] = ()


def is_private_ip(ip_str: str) -> bool:
    """判断 IP 是否落在私有/保留/环回/链路本地/组播/未指定网段。

    这些网段一律阻断，防止 SSRF 攻击内网：
    - 私有网段（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16）
    - 环回（127.0.0.0/8、::1）
    - 链路本地（169.254.0.0/16、fe80::/10）
    - 未指定（0.0.0.0、::）
    - 组播（224.0.0.0/4、ff00::/8）
    - 保留（240.0.0.0/4、::ffff:0:0/96 等）
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # 解析失败按危险处理
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    )


def is_domain_allowed(host: str, allowlist: tuple[str, ...] = DEFAULT_DOMAIN_ALLOWLIST) -> bool:
    """域名白名单校验。空白名单 = 允许所有公共域名。"""
    if not allowlist:
        return True
    host_lower = host.lower().lstrip(".")
    return any(
        host_lower == suffix.lstrip(".") or host_lower.endswith("." + suffix.lstrip("."))
        for suffix in allowlist
    )


def resolve_host_ips(host: str) -> list[str]:
    """解析 host 为 IP 字符串列表。失败时返回空列表。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    return list({info[4][0] for info in infos})


def validate_url(
    url: str,
    *,
    domain_allowlist: tuple[str, ...] = DEFAULT_DOMAIN_ALLOWLIST,
) -> str:
    """校验 URL 安全性。

    Args:
        url: 待校验的完整 URL。
        domain_allowlist: 域名白名单后缀，空表示允许所有公共域名。

    Returns:
        校验通过后返回规范化后的 URL（原样返回）。

    Raises:
        ValueError: 以下任一情况：
            - URL 为空或解析失败；
            - scheme 非 http/https；
            - host 为空；
            - host 命中元数据端点 169.254.169.254；
            - host 不在域名白名单；
            - host 解析为私有/保留/环回/链路本地网段 IP。
    """
    if not url or not url.strip():
        raise ValueError("URL 不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(
            f"非法 scheme: {parsed.scheme!r}，仅允许 {sorted(ALLOWED_SCHEMES)}"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"URL 缺少 host: {url}")

    # 硬阻断云元数据端点（无论解析结果如何）。
    if host == METADATA_HOST:
        raise ValueError(f"阻断云元数据端点: {host}")

    # 域名白名单校验。
    if not is_domain_allowed(host, domain_allowlist):
        raise ValueError(f"域名不在白名单: {host}")

    # 如果 host 本身就是 IP，直接校验。
    try:
        ip = ipaddress.ip_address(host)
        if is_private_ip(str(ip)):
            raise ValueError(f"阻断私有/保留网段 IP: {host}")
        return url
    except ValueError:
        # 不是 IP 字面量，继续按域名解析
        pass

    # 域名 → IP 解析后逐个校验。
    ips = resolve_host_ips(host)
    if not ips:
        raise ValueError(f"域名解析失败: {host}")
    for ip_str in ips:
        if is_private_ip(ip_str):
            raise ValueError(f"域名 {host} 解析到私有网段 IP: {ip_str}")
    return url


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_DOMAIN_ALLOWLIST",
    "METADATA_HOST",
    "is_domain_allowed",
    "is_private_ip",
    "resolve_host_ips",
    "validate_url",
]
