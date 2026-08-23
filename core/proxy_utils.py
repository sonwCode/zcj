from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


def _is_host_port(value: str) -> bool:
    host, separator, port = value.rpartition(":")
    if not separator or not host.strip() or not port.isdigit():
        return False
    return 0 < int(port) <= 65535


def _normalize_authority(authority: str) -> str:
    """Return standard ``user:pass@host:port`` authority.

    Several proxy vendors export ``host:port@user:pass``.  It resembles a URL
    authority but swaps the authentication and endpoint halves, which requests
    later rejects with InvalidURL.  Detect it by the numeric endpoint port.
    """
    value = authority.strip().strip("/")
    if "@" not in value:
        return value
    left, right = value.rsplit("@", 1)
    if _is_host_port(left) and not _is_host_port(right):
        return f"{right}@{left}"
    return value


def normalize_proxy_url(raw: str | None, *, default_scheme: str = "http") -> str:
    """Normalize common proxy input formats into a URL usable by requests/playwright.

    Supported input examples:
    - http://user:pass@host:port
    - host:port
    - user:pass@host:port
    - host:port:user:pass
    - socks5://host:port
    """
    value = str(raw or "").strip().strip("\ufeff")
    if not value:
        return ""

    if "://" in value:
        scheme, authority = value.split("://", 1)
        scheme = scheme.lower().strip()
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            return ""
        value = f"{scheme}://{_normalize_authority(authority)}"
    else:
        parts = value.split(":")
        if len(parts) == 4 and "@" not in value:
            host, port, username, password = [part.strip() for part in parts]
            if host and port and username and password:
                value = f"{username}:{password}@{host}:{port}"

        value = f"{default_scheme}://{_normalize_authority(value)}"

    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname:
            return ""
        port = parsed.port
        if port is None or not (0 < port <= 65535):
            return ""
    except ValueError:
        return ""

    return value


def redact_proxy_credentials(message: str, proxy_url: str) -> str:
    """Hide proxy passwords from request-library exception text."""
    text = str(message or "")
    normalized = normalize_proxy_url(proxy_url)
    if not normalized:
        return text
    try:
        parsed = urlsplit(normalized)
        if parsed.username is None:
            return text
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        masked = f"{parsed.scheme}://{parsed.username}:****@{host}{port}"
        return text.replace(normalized, masked)
    except ValueError:
        return text


def mask_proxy_url(raw: str | None) -> str:
    """Return a log-safe proxy URL while preserving its endpoint."""
    normalized = normalize_proxy_url(raw)
    if not normalized:
        return str(raw or "").strip()
    try:
        parsed = urlsplit(normalized)
        if parsed.username is None and parsed.password is None:
            return normalized
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"***@{hostname}{port}", parsed.path, parsed.query, parsed.fragment))
    except ValueError:
        return str(raw or "").strip()


def infer_proxy_region(raw: str | None) -> str:
    """Read vendor username parameters such as ``region-JP``."""
    normalized = normalize_proxy_url(raw)
    if not normalized:
        return ""
    try:
        username = urlsplit(normalized).username or ""
    except ValueError:
        return ""
    match = re.search(r"(?:^|-)region-([a-z]{2})(?:-|$)", username, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def pin_711proxy_session(
    raw: str | None,
    *,
    region: str,
    session_id: str,
    session_minutes: int = 180,
) -> str:
    """Bind a 711Proxy URL to one country and one sticky exit session."""
    normalized = normalize_proxy_url(raw)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    host = str(parsed.hostname or "").lower()
    if host != "711proxy.com" and not host.endswith(".711proxy.com"):
        return normalized

    country = re.sub(r"[^A-Za-z]", "", str(region or "")).upper()
    sticky_id = re.sub(r"[^A-Za-z0-9]", "", str(session_id or ""))[:11]
    if len(country) != 2 or not sticky_id:
        return normalized

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username or not password:
        return normalized

    for modifier in (
        r"-region-[^-]+",
        r"-st-[^-]+",
        r"-city-[^-]+",
        r"-session-[^-]+",
        r"-sessTime-\d+",
        r"-sessAuto-\d+",
    ):
        username = re.sub(modifier, "", username, flags=re.IGNORECASE)
    minutes = min(max(int(session_minutes or 180), 1), 180)
    username = (
        f"{username}-region-{country}-session-{sticky_id}-sessTime-{minutes}"
    )

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    authority = (
        f"{quote(username, safe='-._~')}:{quote(password, safe='-._~')}@"
        f"{hostname}:{parsed.port}"
    )
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment))
