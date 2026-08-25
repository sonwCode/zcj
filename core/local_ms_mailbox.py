"""Local Microsoft mailbox pool provider.

The importer accepts GuJumpgate Hotmail rows (email/password/client_id/
refresh_token) and the older Xinlan/BH Mailer "common" account rows.
Microsoft accounts with Client Id + refresh token are read through Microsoft
Graph; rows without OAuth material fall back to IMAP only when inbound server
fields are present and usable.
"""

from __future__ import annotations

import csv
import email as email_lib
import hashlib
import imaplib
import json
import re
import secrets
import ssl
import string
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import getaddresses
from pathlib import Path
from typing import Any

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link
from core.proxy_utils import normalize_proxy_url


GRAPH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_CONSUMERS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
DEFAULT_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".local_ms_mailbox_pool_state.json"


@dataclass(frozen=True)
class LocalMicrosoftMailboxEntry:
    email: str
    password: str = ""
    login_account: str = ""
    imap_host: str = ""
    imap_port: str = ""
    imap_account_type: str = ""
    imap_security: str = ""
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_security: str = ""
    note: str = ""
    proxy_mode: str = ""
    proxy: str = ""
    label: str = ""
    recovery_email: str = ""
    recovery_password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    totp_secret: str = ""
    mailbox_url: str = ""
    source_format: str = "xinlan_common"
    raw: str = ""

    @property
    def key(self) -> str:
        return self.email.strip().lower()

    @property
    def graph_ready(self) -> bool:
        return bool(self.client_id and self.refresh_token)

    @property
    def imap_ready(self) -> bool:
        return bool(self.imap_host and (self.login_account or self.email) and self.password)

    @property
    def url_ready(self) -> bool:
        return bool(self.mailbox_url)

    def credentials(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "login_account": self.login_account,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "imap_account_type": self.imap_account_type,
            "imap_security": self.imap_security,
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
            "recovery_email": self.recovery_email,
            "recovery_password": self.recovery_password,
            "totp_secret": self.totp_secret,
            "mailbox_url": self.mailbox_url,
        }


@dataclass(frozen=True)
class InlineMailboxPoolBuildResult:
    pool_text: str
    source_count: int
    expanded_count: int
    alias_count: int


@dataclass(frozen=True)
class LocalMailboxPoolSplitResult:
    pool_text: str
    total_count: int
    unused_count: int
    used_count: int
    blocked_count: int
    duplicate_count: int
    invalid_count: int


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _int_config(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_local_ms_mailbox_proxy(
    config: dict | None,
    registration_proxy: str | None = None,
) -> str | None:
    """Resolve the mailbox route independently from the signup route.

    Microsoft token/Graph traffic is not tied to the target site's sticky
    registration session. Reusing that route made a failed registration proxy
    consume mailbox rows before an OTP request could reach Microsoft.
    """
    values = dict(config or {})
    explicit = normalize_proxy_url(values.get("local_ms_mailbox_proxy"))
    if explicit:
        return explicit
    if _truthy(values.get("local_ms_use_registration_proxy")):
        return normalize_proxy_url(registration_proxy or values.get("proxy"))
    return None


class MicrosoftMailboxNetworkError(RuntimeError):
    """A transport failure that must not be mistaken for an invalid mailbox RT."""

    code = "mailbox_network_error"
    retryable = True


_MAILBOX_NETWORK_HTTP_STATUSES = {407, 502, 503, 504}
_TRANSIENT_MAILBOX_NETWORK_MARKERS = (
    "mailbox_network_error",
    "proxyerror",
    "unable to connect to proxy",
    "tunnel connection",
    "connect tunnel",
    "max retries exceeded",
    "connectionpool",
    "proxy_network_error",
)


def _safe_text(value: object) -> str:
    return str(value or "").strip().strip("\ufeff")


def _csv_split(line: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([line], delimiter=delimiter, quotechar='"', skipinitialspace=True))
    except Exception:
        return line.split(delimiter)


def split_local_ms_pool_line(line: str) -> list[str]:
    text = str(line or "").strip().strip("\ufeff")
    if not text:
        return []
    if "----" in text:
        return [item.strip() for item in text.split("----")]
    if "\t" in text:
        return [item.strip() for item in text.split("\t")]
    if "，" in text:
        return [item.strip() for item in _csv_split(text, "，")]
    if "," in text:
        return [item.strip() for item in _csv_split(text, ",")]
    return [item.strip() for item in re.split(r"\s+", text) if item.strip()]


def split_xinlan_common_line(line: str) -> list[str]:
    return split_local_ms_pool_line(line)


def _is_gujumpgate_hotmail_header(parts: list[str]) -> bool:
    normalized = [str(part or "").strip().lower() for part in parts[:4]]
    if len(normalized) < 4:
        return False
    return (
        normalized[0] in {"account", "email", "mail", "账号", "郵箱", "邮箱"}
        and normalized[1] in {"password", "pass", "pwd", "密码", "密碼"}
        and normalized[2] in {"id", "clientid", "client_id", "client id"}
        and normalized[3] in {"token", "refreshtoken", "refresh_token", "refresh token"}
    )


def _looks_like_gujumpgate_hotmail_row(parts: list[str]) -> bool:
    return len(parts) == 4 and "@" in _safe_text(parts[0]) and bool(_safe_text(parts[2])) and bool(_safe_text(parts[3]))


def _looks_like_graph_alias_row(parts: list[str]) -> bool:
    return (
        len(parts) == 5
        and "@" in _safe_text(parts[0])
        and "@" in _safe_text(parts[1])
        and bool(_safe_text(parts[3]))
        and bool(_safe_text(parts[4]))
    )


def _looks_like_mailbox_url_row(parts: list[str]) -> bool:
    return len(parts) >= 2 and "@" in _safe_text(parts[0]) and bool(re.match(r"^https?://", _safe_text(parts[1]), re.I))


def _looks_like_gmail_totp_card_row(parts: list[str]) -> bool:
    if len(parts) < 5:
        return False
    email = _safe_text(parts[0]).lower()
    recovery = _safe_text(parts[2]).lower()
    totp_secret = _safe_text(parts[3]).replace(" ", "")
    year = _safe_text(parts[4])
    return (
        email.endswith("@gmail.com")
        and "@" in recovery
        and bool(re.fullmatch(r"[a-z2-7]{16,64}", totp_secret.lower()))
        and bool(re.fullmatch(r"20\d{2}", year))
    )


def _unsupported_gmail_totp_card_count(text: str) -> int:
    count = 0
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//") or line.startswith("'"):
            continue
        if _looks_like_gmail_totp_card_row(split_local_ms_pool_line(line)):
            count += 1
    return count


def parse_local_ms_pool_rows(text: str) -> list[LocalMicrosoftMailboxEntry]:
    entries: list[LocalMicrosoftMailboxEntry] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//") or line.startswith("'"):
            continue
        parts = split_local_ms_pool_line(line)
        if not parts:
            continue
        if _is_gujumpgate_hotmail_header(parts):
            continue
        if _looks_like_mailbox_url_row(parts):
            entry = LocalMicrosoftMailboxEntry(
                email=_safe_text(parts[0]),
                login_account=_safe_text(parts[0]),
                mailbox_url=_safe_text(parts[1]),
                source_format="mailbox_url",
                raw=line,
            )
            if entry.key in seen:
                continue
            seen.add(entry.key)
            entries.append(entry)
            continue
        if _looks_like_gmail_totp_card_row(parts):
            continue
        if _looks_like_graph_alias_row(parts):
            entry = LocalMicrosoftMailboxEntry(
                email=_safe_text(parts[0]),
                login_account=_safe_text(parts[1]),
                password=_safe_text(parts[2]),
                client_id=_safe_text(parts[3]),
                refresh_token=_safe_text(parts[4]),
                source_format="graph_plus_alias",
                raw=line,
            )
            if entry.key in seen:
                continue
            seen.add(entry.key)
            entries.append(entry)
            continue
        if _looks_like_gujumpgate_hotmail_row(parts):
            email = _safe_text(parts[0])
            entry = LocalMicrosoftMailboxEntry(
                email=email,
                password=_safe_text(parts[1]),
                login_account=email,
                client_id=_safe_text(parts[2]),
                refresh_token=_safe_text(parts[3]),
                source_format="gujumpgate_hotmail",
                raw=line,
            )
            if entry.key in seen:
                continue
            seen.add(entry.key)
            entries.append(entry)
            continue
        padded = parts + [""] * max(0, 19 - len(parts))
        email = _safe_text(padded[0])
        if "@" not in email:
            continue
        entry = LocalMicrosoftMailboxEntry(
            email=email,
            password=_safe_text(padded[1]),
            login_account=_safe_text(padded[2]) or email,
            imap_host=_safe_text(padded[3]),
            imap_port=_safe_text(padded[4]),
            imap_account_type=_safe_text(padded[5]),
            imap_security=_safe_text(padded[6]),
            smtp_host=_safe_text(padded[7]),
            smtp_port=_safe_text(padded[8]),
            smtp_security=_safe_text(padded[9]),
            note=_safe_text(padded[10]),
            proxy_mode=_safe_text(padded[11]),
            proxy=_safe_text(padded[12]),
            label=_safe_text(padded[13]),
            recovery_email=_safe_text(padded[14]),
            recovery_password=_safe_text(padded[15]),
            client_id=_safe_text(padded[16]),
            refresh_token=_safe_text(padded[17]),
            totp_secret=_safe_text(padded[18]),
            source_format="xinlan_common",
            raw=line,
        )
        if entry.key in seen:
            continue
        seen.add(entry.key)
        entries.append(entry)
    return entries


def parse_xinlan_common_rows(text: str) -> list[LocalMicrosoftMailboxEntry]:
    return parse_local_ms_pool_rows(text)


def split_unused_local_ms_pool_rows(
    text: str,
    *,
    state_file: str = "",
) -> LocalMailboxPoolSplitResult:
    """Return deduplicated rows whose email has never been reserved."""
    state_path = Path(state_file or DEFAULT_STATE_FILE)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {"used": {}}
    used_keys = {str(key or "").strip().lower() for key in (state.get("used") or {})}
    blocked_keys = {str(key or "").strip().lower() for key in (state.get("blocked") or {})}
    seen: set[str] = set()
    unused_lines: list[str] = []
    total_count = 0
    used_count = 0
    blocked_count = 0
    duplicate_count = 0
    invalid_count = 0

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//") or line.startswith("'"):
            continue
        entries = parse_local_ms_pool_rows(line)
        if not entries:
            invalid_count += 1
            continue
        entry = entries[0]
        total_count += 1
        if entry.key in seen:
            duplicate_count += 1
            continue
        seen.add(entry.key)
        if entry.key in blocked_keys:
            blocked_count += 1
            continue
        if entry.key in used_keys:
            used_count += 1
            continue
        unused_lines.append(entry.raw or line)

    return LocalMailboxPoolSplitResult(
        pool_text="\n".join(unused_lines),
        total_count=total_count,
        unused_count=len(unused_lines),
        used_count=used_count,
        blocked_count=blocked_count,
        duplicate_count=duplicate_count,
        invalid_count=invalid_count,
    )


GMAIL_ALIAS_DOMAINS = {"gmail.com", "googlemail.com"}
MICROSOFT_PLUS_ALIAS_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}
PLUS_ALIAS_DOMAINS = GMAIL_ALIAS_DOMAINS | MICROSOFT_PLUS_ALIAS_DOMAINS


def is_gmail_address(email: str) -> bool:
    text = _safe_text(email).lower()
    if "@" not in text:
        return False
    return text.rsplit("@", 1)[1] in GMAIL_ALIAS_DOMAINS


def is_plus_alias_address(email: str) -> bool:
    text = _safe_text(email).lower()
    if "@" not in text:
        return False
    return text.rsplit("@", 1)[1] in PLUS_ALIAS_DOMAINS


def is_microsoft_plus_alias_address(email: str) -> bool:
    text = _safe_text(email).lower()
    if "@" not in text:
        return False
    return text.rsplit("@", 1)[1] in MICROSOFT_PLUS_ALIAS_DOMAINS


def generate_plus_aliases(
    email: str,
    count: int,
    *,
    existing: set[str] | None = None,
) -> list[str]:
    count = max(int(count or 0), 0)
    if count <= 0:
        return []
    text = _safe_text(email)
    if "@" not in text or not is_plus_alias_address(text):
        raise ValueError(f"plus addressing is not enabled for: {email}")
    local, domain = text.split("@", 1)
    base_local = local.split("+", 1)[0].strip()
    if not base_local:
        raise ValueError(f"invalid email local part: {email}")

    used = {item.lower() for item in (existing or set())}
    aliases: list[str] = []
    attempts = 0
    max_attempts = max(count * 30, 100)
    while len(aliases) < count and attempts < max_attempts:
        attempts += 1
        suffix_len = 5 + secrets.randbelow(3)
        suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(suffix_len))
        alias = f"{base_local}+{suffix}@{domain}"
        key = alias.lower()
        if key in used:
            continue
        used.add(key)
        aliases.append(alias)
    if len(aliases) < count:
        raise RuntimeError(f"unable to generate {count} unique aliases for {email}")
    return aliases


def generate_gmail_plus_aliases(
    email: str,
    count: int,
    *,
    existing: set[str] | None = None,
) -> list[str]:
    """Generate unique Gmail plus-address aliases for a user-owned mailbox.

    This mirrors the desktop tool's practical behavior: local+random@domain.
    The base local part is normalized by dropping an existing +suffix so a
    pasted alias can still fan out from the same underlying mailbox.
    """

    if not is_gmail_address(email):
        raise ValueError(f"not a Gmail address: {email}")
    return generate_plus_aliases(email, count, existing=existing)


def generate_microsoft_pool_aliases(
    email: str,
    count: int,
    *,
    existing: set[str] | None = None,
) -> list[str]:
    """Generate unique Hotmail/Outlook plus aliases for pool fan-out.

    Historically this used deterministic ``+reg1/+reg2`` suffixes. Those are
    heavily reused across runs and often already registered on OpenAI.
    Use random local suffixes (same strategy as Gmail) so each task gets fresh
    child addresses while still reading mail via the parent Graph session.
    """
    text = _safe_text(email)
    if "@" not in text or not is_microsoft_plus_alias_address(text):
        raise ValueError(f"not a Microsoft consumer address: {email}")
    return generate_plus_aliases(text, count, existing=existing)


def generate_microsoft_pool_alias(email: str) -> str:
    """Backward-compatible single-alias helper."""

    return generate_microsoft_pool_aliases(email, 1)[0]


def _plus_alias_pool_line(entry: LocalMicrosoftMailboxEntry, alias: str) -> str:
    if entry.mailbox_url:
        return f"{alias}----{entry.mailbox_url}"
    if entry.graph_ready:
        login_email = entry.login_account or entry.email
        return "----".join([
            alias,
            login_email,
            entry.password,
            entry.client_id,
            entry.refresh_token,
        ])
    if entry.imap_ready:
        values = [
            alias,
            entry.password,
            entry.login_account or entry.email,
            entry.imap_host,
            entry.imap_port,
            entry.imap_account_type,
            entry.imap_security,
            entry.smtp_host,
            entry.smtp_port,
            entry.smtp_security,
            entry.note,
            entry.proxy_mode,
            entry.proxy,
            entry.label,
            entry.recovery_email,
            entry.recovery_password,
            entry.client_id,
            entry.refresh_token,
            entry.totp_secret,
        ]
        return "----".join(values)
    raise ValueError(f"mailbox has no reusable inbox credentials: {entry.email}")


def build_inline_mailbox_url_pool_text(
    text: str,
    *,
    gmail_alias_enabled: bool = False,
    gmail_alias_count: int = 1,
) -> InlineMailboxPoolBuildResult:
    """Build a local_ms_pool text block from inline `email----api_url` rows.

    Gmail and Microsoft consumer mailboxes fan out to distinct plus aliases.
    Microsoft aliases are deterministic so state persists across task runs.
    """

    entries = parse_local_ms_pool_rows(text)
    output: list[str] = []
    seen: set[str] = set()
    alias_total = 0
    alias_count = max(int(gmail_alias_count or 1), 1)

    def append_line(email_value: str, line_value: str) -> None:
        key = _safe_text(email_value).lower()
        if not key or key in seen:
            return
        seen.add(key)
        output.append(line_value)

    for entry in entries:
        if gmail_alias_enabled and is_plus_alias_address(entry.email):
            if is_microsoft_plus_alias_address(entry.email):
                aliases = generate_microsoft_pool_aliases(
                    entry.email,
                    alias_count,
                    existing=seen,
                )
            else:
                aliases = generate_plus_aliases(entry.email, alias_count, existing=seen)
            alias_total += len(aliases)
            for alias in aliases:
                append_line(alias, _plus_alias_pool_line(entry, alias))
            continue
        if entry.source_format == "mailbox_url" and entry.mailbox_url:
            append_line(entry.email, f"{entry.email}----{entry.mailbox_url}")
            continue
        append_line(entry.email, entry.raw or entry.email)

    return InlineMailboxPoolBuildResult(
        pool_text="\n".join(output),
        source_count=len(entries),
        expanded_count=len(output),
        alias_count=alias_total,
    )


class LocalMicrosoftMailboxPool(BaseMailbox):
    """Use existing Outlook/Hotmail/Live accounts from a local text pool."""

    _lock = threading.Lock()

    def __init__(
        self,
        *,
        pool_text: str = "",
        pool_file: str = "",
        state_file: str = "",
        graph_scope: str = "",
        allow_reuse: bool = False,
        proxy: str = None,
        mailbox_url_timeout: int = 8,
        mailbox_url_poll_interval: int = 2,
        provider_name: str = "local_ms_pool",
        plus_alias_enabled: bool = False,
        plus_alias_count: int = 1,
        direct_fallback: bool = True,
        network_attempts: int = 2,
    ):
        self.pool_text = str(pool_text or "")
        self.pool_file = str(pool_file or "").strip()
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.graph_scope = str(graph_scope or DEFAULT_GRAPH_SCOPE).strip()
        self.allow_reuse = bool(allow_reuse)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.mailbox_url_timeout = max(_int_config(mailbox_url_timeout, 8), 1)
        self.mailbox_url_poll_interval = max(_int_config(mailbox_url_poll_interval, 2), 1)
        self.provider_name = str(provider_name or "local_ms_pool").strip() or "local_ms_pool"
        self.plus_alias_enabled = bool(plus_alias_enabled)
        self.plus_alias_count = max(_int_config(plus_alias_count, 1), 1)
        self.direct_fallback = bool(direct_fallback)
        self.network_attempts = min(max(_int_config(network_attempts, 2), 1), 3)
        self._expanded_pool_text: str | None = None
        # A transiently failed row is released from persistent state so a
        # later task can retry it, but it remains retired inside this task to
        # prevent a tight loop selecting the same route immediately.
        self._task_retired: set[str] = set()

    def _proxies_for_entry(self, entry: LocalMicrosoftMailboxEntry):
        entry_proxy = normalize_proxy_url(entry.proxy)
        if entry_proxy:
            return {"http": entry_proxy, "https": entry_proxy}
        return self.proxy

    def _request_with_network_fallback(
        self,
        method: str,
        entry: LocalMicrosoftMailboxEntry,
        url: str,
        **kwargs,
    ):
        request_fn = requests.post if method.strip().lower() == "post" else requests.get
        primary_proxies = self._proxies_for_entry(entry)
        routes: list[tuple[str, dict | None]] = [
            ("proxy" if primary_proxies else "direct", primary_proxies)
        ]
        if primary_proxies and self.direct_fallback:
            routes.append(("direct-fallback", None))

        errors: list[str] = []
        for attempt in range(1, self.network_attempts + 1):
            for route_name, proxies in routes:
                try:
                    response = request_fn(
                        url,
                        proxies=proxies,
                        **kwargs,
                    )
                except requests.exceptions.RequestException as exc:
                    errors.append(
                        f"{route_name}/{attempt}: {exc.__class__.__name__}"
                    )
                    continue
                if int(getattr(response, "status_code", 0) or 0) in _MAILBOX_NETWORK_HTTP_STATUSES:
                    errors.append(
                        f"{route_name}/{attempt}: HTTP {response.status_code}"
                    )
                    continue
                return response
            if attempt < self.network_attempts:
                time.sleep(min(attempt, 2))

        detail = " -> ".join(errors[-6:]) or "transport unavailable"
        raise MicrosoftMailboxNetworkError(
            f"mailbox_network_error: Microsoft 邮箱网络请求失败: {detail}"
        )

    @classmethod
    def from_config(cls, config: dict) -> "LocalMicrosoftMailboxPool":
        return cls(
            pool_text=config.get("local_ms_pool_text", ""),
            pool_file=config.get("local_ms_pool_file", ""),
            state_file=config.get("local_ms_pool_state_file", ""),
            graph_scope=config.get("local_ms_graph_scope", ""),
            allow_reuse=_truthy(config.get("local_ms_pool_allow_reuse")),
            proxy=resolve_local_ms_mailbox_proxy(config, config.get("proxy") or None),
            mailbox_url_timeout=_int_config(config.get("local_ms_mailbox_url_timeout"), 8),
            mailbox_url_poll_interval=_int_config(config.get("local_ms_mailbox_url_poll_interval"), 2),
            provider_name=str(config.get("_provider_key") or config.get("mailbox_provider_key") or "local_ms_pool"),
            plus_alias_enabled=_truthy(
                config.get("mailbox_alias_enabled", config.get("gmail_alias_enabled"))
            ),
            plus_alias_count=_int_config(
                config.get("mailbox_alias_count", config.get("gmail_alias_count")),
                1,
            ),
            direct_fallback=_truthy(
                config.get("local_ms_proxy_direct_fallback", True)
            ),
            network_attempts=_int_config(
                config.get("local_ms_network_attempts"),
                2,
            ),
        )

    def _load_pool_text(self) -> str:
        chunks: list[str] = []
        if self.pool_text.strip():
            chunks.append(self.pool_text)
        if self.pool_file:
            path = Path(self.pool_file).expanduser()
            if not path.exists():
                raise RuntimeError(f"本地微软邮箱池文件不存在: {path}")
            chunks.append(path.read_text(encoding="utf-8-sig"))
        combined = "\n".join(chunks)
        if not combined.strip():
            raise RuntimeError("本地微软邮箱池为空，请粘贴 Hotmail 四列格式或配置文件路径")
        return combined

    def _entries(self) -> list[LocalMicrosoftMailboxEntry]:
        pool_text = self._load_pool_text()
        if self.plus_alias_enabled:
            if self._expanded_pool_text is None:
                expanded = build_inline_mailbox_url_pool_text(
                    pool_text,
                    gmail_alias_enabled=True,
                    gmail_alias_count=self.plus_alias_count,
                )
                self._expanded_pool_text = expanded.pool_text
            pool_text = self._expanded_pool_text
        entries = parse_local_ms_pool_rows(pool_text)
        if not entries:
            gmail_totp_count = _unsupported_gmail_totp_card_count(pool_text)
            if gmail_totp_count:
                raise RuntimeError(
                    "本地微软邮箱池只检测到 Gmail 登录卡密格式，无法自动读取验证码。"
                    "local_ms_pool 需要 Hotmail/Outlook Graph 四列格式，或包含 imap_host 的 IMAP 格式；"
                    f"已跳过 Gmail 卡密 {gmail_totp_count} 行。"
                )
            raise RuntimeError("本地微软邮箱池未解析到有效邮箱")
        return entries

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {"used": {}, "blocked": {}, "failures": {}}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _merge_state_records(
        self,
        key: str,
        records: dict[str, dict[str, Any]],
        *,
        remove_from: tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            state = self._state()
            for bucket, record in records.items():
                values = dict(state.get(bucket) or {})
                current = dict(values.get(key) or {})
                current.update(record)
                values[key] = current
                state[bucket] = values
            for bucket in remove_from:
                values = dict(state.get(bucket) or {})
                values.pop(key, None)
                state[bucket] = values
            self._save_state(state)

    def _source_id(self) -> str:
        material = f"{self.pool_file}\n{self.pool_text}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:16]

    def _reserve(self, entry: LocalMicrosoftMailboxEntry) -> None:
        if self.allow_reuse:
            return
        state = self._state()
        used = dict(state.get("used") or {})
        used[entry.key] = {
            "email": entry.email,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "source_id": self._source_id(),
        }
        state["used"] = used
        self._save_state(state)

    @staticmethod
    def _base_email_key(email: str) -> str:
        text = str(email or "").strip().lower()
        if "@" not in text:
            return text
        local, domain = text.rsplit("@", 1)
        return f"{local.split('+', 1)[0]}@{domain}"

    def _available_entry(self) -> LocalMicrosoftMailboxEntry:
        entries = self._entries()
        state = self._state()
        used = set((state.get("used") or {}).keys())
        blocked = set((state.get("blocked") or {}).keys())
        # Only bare-parent keys retire the whole tree. Blocking one child
        # (e.g. already_registered on a single plus-address) must not hide
        # siblings. Bare keys are those without a '+' local-part.
        blocked_bases = {
            item for item in blocked
            if "@" in item and "+" not in item.split("@", 1)[0]
        }
        for entry in entries:
            base_key = self._base_email_key(entry.login_account or entry.email)
            if (
                entry.key in blocked
                or base_key in blocked_bases
                or entry.key in self._task_retired
            ):
                continue
            if self.allow_reuse or entry.key not in used:
                return entry
        raise RuntimeError(f"本地微软邮箱池已用尽: total={len(entries)}")

    def mark_registration_failure(self, account: MailboxAccount, reason: str = "") -> bool:
        """Permanently remove a remotely rejected identity from the available pool."""
        key = str(getattr(account, "account_id", "") or getattr(account, "email", "")).strip().lower()
        if not key:
            return False
        reason_text = str(reason or "registration_failed")[:500]
        reason_lower = reason_text.lower()
        reason_code = (
            "already_registered"
            if "user_already_exists" in reason_lower or "account already exists" in reason_lower
            else "account_deactivated"
            if "deactivated" in reason_lower or "deleted" in reason_lower
            else "remote_rejected"
        )
        now = datetime.now(timezone.utc).isoformat()
        blocked_record = {
            "email": str(getattr(account, "email", "") or ""),
            "reason": reason_text,
            "reason_code": reason_code,
            "blocked_at": now,
            "source_id": self._source_id(),
        }
        failure_record = {
            "email": str(getattr(account, "email", "") or ""),
            "reason": reason_text,
            "reason_code": reason_code,
            "failed_at": now,
            "source_id": self._source_id(),
        }
        self._merge_state_records(
            key,
            {"blocked": blocked_record, "failures": failure_record},
        )
        return True

    def mark_attempt_failure(self, account: MailboxAccount, reason: str = "") -> bool:
        key = str(getattr(account, "account_id", "") or getattr(account, "email", "")).strip().lower()
        if not key:
            return False
        reason_text = str(reason or "registration_failed")[:500]
        failure_record = {
            "email": str(getattr(account, "email", "") or ""),
            "reason": reason_text,
            "reason_code": "registration_failed",
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "source_id": self._source_id(),
        }
        self._merge_state_records(key, {"failures": failure_record})
        return True

    def _release_reserved_failure(
        self,
        account: MailboxAccount,
        reason: str,
        *,
        reason_code: str,
    ) -> bool:
        key = str(
            getattr(account, "account_id", "")
            or getattr(account, "email", "")
        ).strip().lower()
        if not key:
            return False
        reason_text = str(reason or reason_code)[:500]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._task_retired.add(key)
            state = self._state()
            used = dict(state.get("used") or {})
            used.pop(key, None)
            failures = dict(state.get("failures") or {})
            failures[key] = {
                "email": str(getattr(account, "email", "") or ""),
                "reason": reason_text,
                "reason_code": reason_code,
                "failed_at": now,
                "source_id": self._source_id(),
            }
            state["used"] = used
            state["failures"] = failures
            self._save_state(state)
        return True

    def release_uncommitted_failure(self, account: MailboxAccount, reason: str = "") -> bool:
        """Release a row when OTP delivery never reached a confirmed remote state."""
        return self._release_reserved_failure(
            account,
            reason or "otp_delivery_failed",
            reason_code="uncommitted_attempt",
        )

    def release_transient_failure(self, account: MailboxAccount, reason: str = "") -> bool:
        """Release a row after transport failure without retrying it in this task."""
        return self._release_reserved_failure(
            account,
            reason or "mailbox_network_error",
            reason_code="transient_network",
        )

    def recover_transient_failures(self) -> int:
        """Undo stale reservations caused only by a previous network outage."""
        recovered = 0
        with self._lock:
            state = self._state()
            used = dict(state.get("used") or {})
            blocked = dict(state.get("blocked") or {})
            failures = dict(state.get("failures") or {})
            now = datetime.now(timezone.utc).isoformat()
            for raw_key, raw_failure in list(failures.items()):
                key = str(raw_key or "").strip().lower()
                failure = dict(raw_failure or {})
                used_record = dict(used.get(key) or {})
                reason = str(failure.get("reason") or "").lower()
                is_transient = (
                    str(failure.get("reason_code") or "") == "transient_network"
                    or any(marker in reason for marker in _TRANSIENT_MAILBOX_NETWORK_MARKERS)
                )
                if (
                    not key
                    or not is_transient
                    or key in blocked
                    or not used_record
                    or used_record.get("completed_at")
                    or str(used_record.get("outcome") or "").lower() == "success"
                ):
                    continue
                used.pop(key, None)
                failure["reason_code"] = "transient_network"
                failure["recovered_at"] = now
                failures[key] = failure
                recovered += 1
            if recovered:
                state["used"] = used
                state["failures"] = failures
                self._save_state(state)
        return recovered

    def mark_registration_success(self, account: MailboxAccount) -> list[str]:
        key = str(getattr(account, "account_id", "") or getattr(account, "email", "")).strip().lower()
        if not key:
            return []
        self._merge_state_records(
            key,
            {
                "used": {
                    "email": str(getattr(account, "email", "") or ""),
                    "outcome": "success",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "source_id": self._source_id(),
                }
            },
            remove_from=("failures",),
        )
        return [str(getattr(account, "email", "") or key)]

    def inventory(self, *, limit: int = 1000) -> dict[str, Any]:
        entries = self._entries()
        state = self._state()
        used = {str(key).lower(): dict(value or {}) for key, value in (state.get("used") or {}).items()}
        blocked = {str(key).lower(): dict(value or {}) for key, value in (state.get("blocked") or {}).items()}
        failures = {str(key).lower(): dict(value or {}) for key, value in (state.get("failures") or {}).items()}
        counts = {"available": 0, "used": 0, "blocked": 0}
        items: list[dict[str, Any]] = []
        for entry in entries:
            used_record = used.get(entry.key, {})
            blocked_record = blocked.get(entry.key, {})
            failure_record = failures.get(entry.key, {})
            status = "blocked" if blocked_record else "used" if used_record and not self.allow_reuse else "available"
            counts[status] += 1
            if len(items) >= max(int(limit or 0), 0):
                continue
            base_email = entry.login_account or entry.email
            if "+" in base_email and "@" in base_email:
                local, domain = base_email.split("@", 1)
                base_email = f"{local.split('+', 1)[0]}@{domain}"
            detail = blocked_record or failure_record or used_record
            items.append(
                {
                    "email": entry.email,
                    "base_email": base_email,
                    "status": status,
                    "reason_code": str(detail.get("reason_code") or ""),
                    "reason": str(detail.get("reason") or ""),
                    "reserved_at": str(used_record.get("reserved_at") or ""),
                    "completed_at": str(used_record.get("completed_at") or ""),
                    "failed_at": str(failure_record.get("failed_at") or ""),
                    "blocked_at": str(blocked_record.get("blocked_at") or ""),
                    "source_format": entry.source_format,
                }
            )
        return {
            "total_count": len(entries),
            "available_count": counts["available"],
            "used_count": counts["used"],
            "blocked_count": counts["blocked"],
            "allow_reuse": self.allow_reuse,
            "items": items,
            "truncated": len(items) < len(entries),
        }

    def peek_email(self) -> str:
        return self._available_entry().email

    def get_email(self) -> MailboxAccount:
        with self._lock:
            entry = self._available_entry()
            self._reserve(entry)

        credentials = entry.credentials()
        credentials = {key: value for key, value in credentials.items() if value}
        return MailboxAccount(
            email=entry.email,
            account_id=entry.key,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": self.provider_name,
                    "login_identifier": entry.login_account or entry.email,
                    "display_name": entry.email,
                    "credentials": credentials,
                    "metadata": {
                        "source": entry.source_format,
                        "source_format": entry.source_format,
                        "credential_purpose": "otp_mailbox",
                        "refresh_token_role": "microsoft_mailbox_oauth",
                        "not_platform_refresh_token": True,
                        "has_graph_refresh_token": bool(entry.graph_ready),
                        "has_imap_config": bool(entry.imap_ready),
                        "has_mailbox_url": bool(entry.url_ready),
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": self.provider_name,
                    "resource_type": "mailbox",
                    "resource_identifier": entry.key,
                    "handle": entry.email,
                    "display_name": entry.email,
                    "metadata": {
                        "email": entry.email,
                        "source": entry.source_format,
                        "reserved": not self.allow_reuse,
                    },
                },
            },
        )

    def _entry_for_account(self, account: MailboxAccount) -> LocalMicrosoftMailboxEntry:
        account_email = str(getattr(account, "email", "") or "").strip().lower()
        extra = dict(getattr(account, "extra", {}) or {})
        provider_account = dict(extra.get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        metadata = dict(provider_account.get("metadata") or {})
        if credentials:
            return LocalMicrosoftMailboxEntry(
                email=str(credentials.get("email") or account.email or ""),
                password=str(credentials.get("password") or ""),
                login_account=str(credentials.get("login_account") or account.email or ""),
                imap_host=str(credentials.get("imap_host") or ""),
                imap_port=str(credentials.get("imap_port") or ""),
                imap_account_type=str(credentials.get("imap_account_type") or ""),
                imap_security=str(credentials.get("imap_security") or ""),
                client_id=str(credentials.get("client_id") or ""),
                refresh_token=str(credentials.get("refresh_token") or ""),
                recovery_email=str(credentials.get("recovery_email") or ""),
                recovery_password=str(credentials.get("recovery_password") or ""),
                totp_secret=str(credentials.get("totp_secret") or ""),
                mailbox_url=str(credentials.get("mailbox_url") or ""),
                source_format=str(metadata.get("source") or metadata.get("source_format") or ""),
            )

        for entry in self._entries():
            if entry.key == account_email:
                return entry
        raise RuntimeError(f"本地微软邮箱池未找到账号: {getattr(account, 'email', '')}")

    @staticmethod
    def _decode_mime(value: str) -> str:
        parts = []
        for raw, charset in decode_header(value or ""):
            if isinstance(raw, bytes):
                parts.append(raw.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(str(raw or ""))
        return "".join(parts)

    @staticmethod
    def _message_id(mail: dict) -> str:
        return str(mail.get("id") or mail.get("internetMessageId") or mail.get("receivedDateTime") or "")

    @staticmethod
    def _message_text(mail: dict) -> str:
        body = mail.get("body") or {}
        return " ".join(
            str(value or "")
            for value in (
                mail.get("subject"),
                mail.get("bodyPreview"),
                body.get("content") if isinstance(body, dict) else "",
            )
        )

    @staticmethod
    def _message_recipients(mail: dict) -> set[str]:
        recipients: set[str] = set()
        raw_recipients = mail.get("toRecipients")
        if isinstance(raw_recipients, list):
            for recipient in raw_recipients:
                if not isinstance(recipient, dict):
                    continue
                address = recipient.get("emailAddress")
                if isinstance(address, dict):
                    value = str(address.get("address") or "").strip().lower()
                    if value:
                        recipients.add(value)
        raw_to = str(mail.get("to") or "").strip()
        if raw_to:
            recipients.update(
                address.strip().lower()
                for _, address in getaddresses([raw_to])
                if address.strip()
            )
        return recipients

    @classmethod
    def _message_is_for_account(cls, mail: dict, account: MailboxAccount) -> bool:
        target = str(getattr(account, "email", "") or "").strip().lower()
        if not target or "+" not in target.partition("@")[0]:
            return True
        return target in cls._message_recipients(mail)

    @staticmethod
    def _find_received_at(value: Any) -> str:
        if isinstance(value, list):
            for item in value:
                found = LocalMicrosoftMailboxPool._find_received_at(item)
                if found:
                    return found
            return ""
        if not isinstance(value, dict):
            return ""
        for key in ("received_at", "receivedAt", "date", "created_at", "createdAt", "time", "timestamp"):
            item = str(value.get(key) or "").strip()
            if item:
                return item
        for child in value.values():
            found = LocalMicrosoftMailboxPool._find_received_at(child)
            if found:
                return found
        return ""

    @staticmethod
    def _find_code_in_json(value: Any, pattern: re.Pattern) -> str:
        if isinstance(value, list):
            for item in value:
                code = LocalMicrosoftMailboxPool._find_code_in_json(item, pattern)
                if code:
                    return code
            return ""
        if isinstance(value, dict):
            for key in ("code", "otp", "verification_code", "verificationCode", "passcode"):
                code = LocalMicrosoftMailboxPool._extract_code(str(value.get(key) or ""), pattern)
                if code:
                    return code
            for key in ("body", "text", "html", "content", "message", "subject"):
                code = LocalMicrosoftMailboxPool._find_code_in_json(value.get(key), pattern)
                if code:
                    return code
            for key, child in value.items():
                if str(key).lower() in {"date", "received_at", "receivedat", "created_at", "createdat", "time", "timestamp", "from", "to"}:
                    continue
                code = LocalMicrosoftMailboxPool._find_code_in_json(child, pattern)
                if code:
                    return code
            return ""
        if isinstance(value, str):
            return LocalMicrosoftMailboxPool._extract_code(value, pattern)
        return ""

    @staticmethod
    def _extract_code(raw: str, pattern: re.Pattern) -> str:
        text = LocalMicrosoftMailboxPool._clean_search_text(str(raw or ""))
        match = pattern.search(text)
        return match.group(1) if match and match.groups() else (match.group(0) if match else "")

    def _mailbox_url_snapshot(self, entry: LocalMicrosoftMailboxEntry, pattern: re.Pattern | None = None) -> dict:
        if not entry.url_ready:
            raise RuntimeError(f"邮箱缺少接码 API URL: {entry.email}")
        response = self._request_with_network_fallback(
            "get",
            entry,
            entry.mailbox_url,
            headers={"accept": "application/json,text/plain,*/*"},
            timeout=self.mailbox_url_timeout,
        )
        raw = response.text or ""
        if response.status_code != 200:
            raise RuntimeError(f"接码 API 读取失败: HTTP {response.status_code} {raw[:200]}")
        code_pattern = pattern or re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")
        code = ""
        received_at = ""
        try:
            payload = response.json()
            code = self._find_code_in_json(payload, code_pattern)
            received_at = self._find_received_at(payload)
        except Exception:
            code = self._extract_code(raw, code_pattern)
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        return {
            "id": f"{received_at}:{code}:{digest}" if received_at or code else digest,
            "subject": "mailbox_url",
            "bodyPreview": raw,
            "code": code,
        }

    def _graph_access_token(self, entry: LocalMicrosoftMailboxEntry) -> str:
        if not entry.graph_ready:
            raise RuntimeError(f"微软邮箱缺少 Client Id 或刷新令牌: {entry.email}")
        errors: list[str] = []
        strategies = [
            ("entra-common-delegated", GRAPH_TOKEN_URL, {"scope": self.graph_scope}),
            ("entra-consumers-delegated", GRAPH_CONSUMERS_TOKEN_URL, {"scope": self.graph_scope}),
            ("entra-common-default", GRAPH_TOKEN_URL, {"scope": GRAPH_DEFAULT_SCOPE}),
        ]
        for name, url, extra_data in strategies:
            data = {
                "client_id": entry.client_id,
                "grant_type": "refresh_token",
                "refresh_token": entry.refresh_token,
            }
            data.update({key: value for key, value in extra_data.items() if value})
            try:
                response = self._request_with_network_fallback(
                    "post",
                    entry,
                    url,
                    data=data,
                    timeout=25,
                )
            except MicrosoftMailboxNetworkError:
                raise
            except Exception as exc:
                errors.append(f"{name}: request failed: {str(exc)[:200]}")
                continue
            if response.status_code != 200:
                errors.append(f"{name}: HTTP {response.status_code} {response.text[:200]}")
                continue
            payload = response.json() or {}
            token = str(payload.get("access_token") or "").strip()
            if token:
                return token
            errors.append(f"{name}: missing access_token")
        details = " | ".join(errors) if errors else "no token strategies attempted"
        raise RuntimeError(f"Microsoft refresh_token 换 access_token 失败: {details}")

    def _graph_messages(self, entry: LocalMicrosoftMailboxEntry) -> list[dict]:
        token = self._graph_access_token(entry)
        response = self._request_with_network_fallback(
            "get",
            entry,
            GRAPH_MESSAGES_URL,
            headers={"authorization": f"Bearer {token}", "accept": "application/json"},
            params={
                "$top": "25",
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,bodyPreview,receivedDateTime,from,toRecipients,body",
            },
            timeout=25,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Microsoft Graph 读取邮件失败: HTTP {response.status_code} {response.text[:200]}")
        payload = response.json() or {}
        return list(payload.get("value") or [])

    def _imap_connect(self, entry: LocalMicrosoftMailboxEntry):
        host = entry.imap_host.strip()
        port = int(entry.imap_port or 993)
        security = entry.imap_security.lower()
        if port == 993 or "ssl" in security:
            return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
        conn = imaplib.IMAP4(host, port)
        if "tls" in security:
            conn.starttls(ssl_context=ssl.create_default_context())
        return conn

    def _imap_messages(self, entry: LocalMicrosoftMailboxEntry) -> list[dict]:
        if not entry.imap_ready:
            raise RuntimeError(f"微软邮箱没有可用的 Graph token，也没有 IMAP 收件配置: {entry.email}")
        conn = self._imap_connect(entry)
        messages: list[dict] = []
        try:
            conn.login(entry.login_account or entry.email, entry.password)
            conn.select("INBOX", readonly=True)
            _, msg_nums = conn.search(None, "ALL")
            ids = msg_nums[0].split() if msg_nums and msg_nums[0] else []
            for mid in reversed(ids[-30:]):
                _, data = conn.fetch(mid, "(RFC822)")
                if not data or not data[0]:
                    continue
                msg = email_lib.message_from_bytes(data[0][1])
                subject = self._decode_mime(str(msg.get("Subject", "") or ""))
                parts: list[str] = []
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() not in ("text/plain", "text/html"):
                            continue
                        payload = part.get_payload(decode=True)
                        if payload:
                            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
                messages.append({
                    "id": str(msg.get("Message-ID") or mid.decode("ascii", errors="ignore")),
                    "subject": subject,
                    "bodyPreview": " ".join(parts),
                    "to": self._decode_mime(str(msg.get("To", "") or "")),
                })
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return messages

    def _messages(self, account: MailboxAccount) -> list[dict]:
        entry = self._entry_for_account(account)
        if entry.url_ready:
            return [self._mailbox_url_snapshot(entry)]
        if entry.graph_ready:
            messages = self._graph_messages(entry)
        else:
            messages = self._imap_messages(entry)
        return [mail for mail in messages if self._message_is_for_account(mail, account)]

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {self._message_id(mail) for mail in self._messages(account) if self._message_id(mail)}
        except Exception:
            return set()

    @staticmethod
    def _clean_search_text(text: str) -> str:
        cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
        cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", cleaned)
        return cleaned

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        seen = set(before_ids or [])
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)")
        start = time.time()
        entry = self._entry_for_account(account)
        poll_interval = self.mailbox_url_poll_interval if entry.url_ready else 5
        while time.time() - start < timeout:
            messages = [self._mailbox_url_snapshot(entry, pattern)] if entry.url_ready else self._messages(account)
            for mail in messages:
                mid = self._message_id(mail)
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                text = self._clean_search_text(self._message_text(mail))
                if keyword and keyword.lower() not in text.lower():
                    continue
                code = str(mail.get("code") or "")
                if code:
                    return code
                match = pattern.search(text)
                if match:
                    return match.group(1) if match.groups() else match.group(0)
            time.sleep(poll_interval)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
    ) -> str:
        seen = set(before_ids or [])
        entry = self._entry_for_account(account)
        poll_interval = self.mailbox_url_poll_interval if entry.url_ready else 5
        start = time.time()
        while time.time() - start < timeout:
            messages = [self._mailbox_url_snapshot(entry)] if entry.url_ready else self._messages(account)
            for mail in messages:
                mid = self._message_id(mail)
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                link = _extract_verification_link(self._message_text(mail), keyword)
                if link:
                    return link
            time.sleep(poll_interval)
        raise TimeoutError(f"waiting for verification link timed out ({timeout}s)")
