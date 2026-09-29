"""Catch-all IMAP mailbox provider for domains you control.

The provider creates a fresh address below a configured domain for every
registration, then reads verification mail from the single catch-all inbox via
IMAP.  It deliberately matches the original recipient in the message headers
so concurrently running registrations cannot consume one another's codes.

Before enabling this provider, configure the mail server to deliver every
unrecognised address for the domain to the configured IMAP inbox (or use an
equivalent catch-all alias).
"""

from __future__ import annotations

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

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link


DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".domain_imap_mailbox_state.json"
DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"
_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    re.IGNORECASE,
)
_ADDRESS_PATTERN = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+", re.IGNORECASE)


def _normalise_domain(value: object) -> str:
    domain = str(value or "").strip().strip("@").rstrip(".").lower()
    if not _DOMAIN_PATTERN.fullmatch(domain):
        raise ValueError("域名邮箱的域名无效，例如：example.test")
    return domain


def _positive_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    try:
        return min(max(int(value or default), minimum), maximum)
    except (TypeError, ValueError):
        return default


def _safe_local_prefix(value: object) -> str:
    prefix = re.sub(r"[^a-z0-9._-]", "", str(value or "reg").strip().lower())
    return (prefix.strip("._-") or "reg")[:24]


@dataclass(frozen=True)
class DomainImapConfig:
    domain: str
    host: str
    port: int
    security: str
    username: str
    password: str
    folder: str
    local_prefix: str

    @classmethod
    def from_values(
        cls,
        *,
        domain: object,
        host: object,
        port: object,
        security: object,
        username: object,
        password: object,
        folder: object,
        local_prefix: object,
    ) -> "DomainImapConfig":
        resolved_host = str(host or "").strip()
        resolved_username = str(username or "").strip()
        resolved_password = str(password or "")
        if not resolved_host:
            raise ValueError("请填写 IMAP 服务器地址")
        if not resolved_username:
            raise ValueError("请填写 IMAP 登录账号")
        if not resolved_password:
            raise ValueError("请填写 IMAP 密码")
        resolved_security = str(security or "ssl").strip().lower()
        if resolved_security not in {"ssl", "starttls", "plain"}:
            raise ValueError("IMAP 加密方式必须为 SSL、STARTTLS 或明文")
        return cls(
            domain=_normalise_domain(domain),
            host=resolved_host,
            port=_positive_int(port, 993, minimum=1, maximum=65535),
            security=resolved_security,
            username=resolved_username,
            password=resolved_password,
            folder=str(folder or "INBOX").strip() or "INBOX",
            local_prefix=_safe_local_prefix(local_prefix),
        )


class DomainImapCatchallMailbox(BaseMailbox):
    """Allocate unique catch-all addresses and poll one IMAP inbox for OTPs."""

    _reservation_lock = threading.Lock()

    def __init__(
        self,
        *,
        domain: str = "",
        host: str = "",
        port: int | str = 993,
        security: str = "ssl",
        username: str = "",
        password: str = "",
        folder: str = "INBOX",
        local_prefix: str = "reg",
        state_file: str = "",
        poll_interval: float | str = 3,
        search_limit: int | str = 40,
        imap_factory=None,
    ):
        self.config = DomainImapConfig.from_values(
            domain=domain,
            host=host,
            port=port,
            security=security,
            username=username,
            password=password,
            folder=folder,
            local_prefix=local_prefix,
        )
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.poll_interval = max(0.0, float(3 if poll_interval in (None, "") else poll_interval))
        self.search_limit = _positive_int(search_limit, 40, minimum=1, maximum=200)
        self._imap_factory = imap_factory

    @classmethod
    def from_config(cls, config: dict) -> "DomainImapCatchallMailbox":
        return cls(
            domain=config.get("domain_imap_domain", ""),
            host=config.get("domain_imap_host", ""),
            port=config.get("domain_imap_port", 993),
            security=config.get("domain_imap_security", "ssl"),
            username=config.get("domain_imap_username", ""),
            password=config.get("domain_imap_password", ""),
            folder=config.get("domain_imap_folder", "INBOX"),
            local_prefix=config.get("domain_imap_local_prefix", "reg"),
            state_file=config.get("domain_imap_state_file", ""),
            poll_interval=config.get("domain_imap_poll_interval", 3),
            search_limit=config.get("domain_imap_search_limit", 40),
        )

    def _state(self) -> dict:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {"used": {}}
        except Exception:
            return {"used": {}}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _address_key(self, address: str) -> str:
        return address.strip().lower()

    def _new_address(self, used: set[str]) -> str:
        # Use letters only so an OTP parser never mistakes a numeric mailbox
        # local-part for a verification code in an HTML message.
        for _ in range(20):
            token = "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
            address = f"{self.config.local_prefix}-{token}@{self.config.domain}"
            if self._address_key(address) not in used:
                return address
        raise RuntimeError("无法生成未使用的域名邮箱地址，请检查占用状态文件")

    def _reserve_address(self, address: str) -> None:
        state = self._state()
        used = dict(state.get("used") or {})
        used[self._address_key(address)] = {
            "email": address,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "domain": self.config.domain,
            "source_id": hashlib.sha256(
                f"{self.config.domain}|{self.config.username}".encode("utf-8")
            ).hexdigest()[:16],
        }
        state["used"] = used
        self._save_state(state)

    def peek_email(self) -> str:
        used = set((self._state().get("used") or {}).keys())
        return self._new_address(used)

    def get_email(self) -> MailboxAccount:
        with self._reservation_lock:
            used = set((self._state().get("used") or {}).keys())
            address = self._new_address(used)
            self._reserve_address(address)
        return MailboxAccount(
            email=address,
            account_id=self._address_key(address),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "domain_imap_catchall",
                    "login_identifier": self.config.username,
                    "display_name": address,
                    "credentials": {
                        "domain": self.config.domain,
                        "imap_host": self.config.host,
                        "imap_port": self.config.port,
                        "imap_security": self.config.security,
                        "imap_username": self.config.username,
                        "imap_folder": self.config.folder,
                    },
                    "metadata": {"source": "domain_imap_catchall"},
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "domain_imap_catchall",
                    "resource_type": "mailbox",
                    "resource_identifier": self._address_key(address),
                    "handle": address,
                    "display_name": address,
                    "metadata": {
                        "email": address,
                        "domain": self.config.domain,
                        "delivery": "catch_all_imap",
                    },
                },
            },
        )

    def _connect(self):
        if self._imap_factory is not None:
            return self._imap_factory(self.config)
        if self.config.security == "ssl":
            return imaplib.IMAP4_SSL(
                self.config.host,
                self.config.port,
                ssl_context=ssl.create_default_context(),
            )
        conn = imaplib.IMAP4(self.config.host, self.config.port)
        if self.config.security == "starttls":
            conn.starttls(ssl_context=ssl.create_default_context())
        return conn

    def test_connection(self) -> None:
        conn = self._connect()
        try:
            result = conn.login(self.config.username, self.config.password)
            if isinstance(result, tuple) and str(result[0]).upper() != "OK":
                raise RuntimeError("IMAP 登录失败")
            result = conn.select(self.config.folder, readonly=True)
            if isinstance(result, tuple) and str(result[0]).upper() != "OK":
                raise RuntimeError(f"无法打开 IMAP 文件夹: {self.config.folder}")
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    @staticmethod
    def _decode_mime(value: str) -> str:
        parts: list[str] = []
        for chunk, charset in decode_header(value or ""):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(str(chunk))
        return "".join(parts)

    @classmethod
    def _body_text(cls, message) -> str:
        parts: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() not in {"text/plain", "text/html"}:
                    continue
                if str(part.get("Content-Disposition") or "").lower().startswith("attachment"):
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        else:
            payload = message.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(message.get_content_charset() or "utf-8", errors="replace"))
        return "\n".join(parts)

    def _messages(self) -> list[dict]:
        conn = self._connect()
        messages: list[dict] = []
        try:
            result = conn.login(self.config.username, self.config.password)
            if isinstance(result, tuple) and str(result[0]).upper() != "OK":
                raise RuntimeError("IMAP 登录失败")
            result = conn.select(self.config.folder, readonly=True)
            if isinstance(result, tuple) and str(result[0]).upper() != "OK":
                raise RuntimeError(f"无法打开 IMAP 文件夹: {self.config.folder}")
            result, raw_ids = conn.uid("search", None, "ALL")
            if str(result).upper() != "OK":
                raise RuntimeError("IMAP 搜索邮件失败")
            identifiers = raw_ids[0].split() if raw_ids and raw_ids[0] else []
            for uid in reversed(identifiers[-self.search_limit:]):
                status, data = conn.uid("fetch", uid, "(RFC822)")
                if str(status).upper() != "OK" or not data or not data[0]:
                    continue
                raw = next((item[1] for item in data if isinstance(item, tuple) and len(item) > 1), None)
                if not isinstance(raw, bytes):
                    continue
                message = email_lib.message_from_bytes(raw)
                headers = {
                    name: "\n".join(str(value) for value in (message.get_all(name, []) or []))
                    for name in (
                        "To", "Cc", "Delivered-To", "X-Original-To", "X-Envelope-To",
                        "Envelope-To", "Apparently-To", "Resent-To",
                    )
                }
                messages.append({
                    "id": str(message.get("Message-ID") or uid.decode("ascii", errors="ignore")),
                    "subject": self._decode_mime(str(message.get("Subject") or "")),
                    "body": self._body_text(message),
                    "headers": headers,
                })
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return messages

    @staticmethod
    def _recipient_addresses(mail: dict) -> set[str]:
        values = [str(value or "") for value in dict(mail.get("headers") or {}).values()]
        recipients = {
            address.strip().lower()
            for _, address in getaddresses(values)
            if address.strip()
        }
        for value in values:
            recipients.update(match.group(0).lower() for match in _ADDRESS_PATTERN.finditer(value))
        return recipients

    @classmethod
    def _message_is_for_account(cls, mail: dict, account: MailboxAccount) -> bool:
        target = str(account.email or "").strip().lower()
        return bool(target and target in cls._recipient_addresses(mail))

    @staticmethod
    def _message_text(mail: dict) -> str:
        return "\n".join((str(mail.get("subject") or ""), str(mail.get("body") or "")))

    @staticmethod
    def _clean_search_text(text: str) -> str:
        cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
        cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        return _ADDRESS_PATTERN.sub(" ", cleaned)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(mail.get("id") or "")
                for mail in self._messages()
                if self._message_is_for_account(mail, account) and mail.get("id")
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        seen = set(before_ids or [])
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            for mail in self._messages():
                if not self._message_is_for_account(mail, account):
                    continue
                message_id = str(mail.get("id") or "")
                if message_id in seen:
                    continue
                if message_id:
                    seen.add(message_id)
                text = self._clean_search_text(self._message_text(mail))
                if keyword and keyword.lower() not in text.lower():
                    continue
                match = pattern.search(text)
                if match:
                    return match.group(1) if match.groups() else match.group(0)
            time.sleep(self.poll_interval)
        raise TimeoutError(f"等待域名邮箱验证码超时 ({timeout}s)")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        seen = set(before_ids or [])
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            for mail in self._messages():
                if not self._message_is_for_account(mail, account):
                    continue
                message_id = str(mail.get("id") or "")
                if message_id in seen:
                    continue
                if message_id:
                    seen.add(message_id)
                link = _extract_verification_link(self._message_text(mail), keyword)
                if link:
                    return link
            time.sleep(self.poll_interval)
        raise TimeoutError(f"等待域名邮箱验证链接超时 ({timeout}s)")
