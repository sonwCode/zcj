"""Domain catch-all mailbox backed by an Inbucket SMTP/API deployment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import string
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link
from core.domain_imap_mailbox import _normalise_domain


DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".inbucket_domain_mailbox_state.json"
DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"


def _positive_number(value: object, default: float, *, minimum: float, maximum: float) -> float:
    try:
        return min(max(float(value or default), minimum), maximum)
    except (TypeError, ValueError):
        return default


def _local_prefix(value: object) -> str:
    prefix = re.sub(r"[^a-z0-9._-]", "", str(value or "reg").strip().lower())
    return (prefix.strip("._-") or "reg")[:24]


class InbucketDomainMailbox(BaseMailbox):
    """Generate domain addresses and read their messages through Inbucket's API."""

    _reservation_lock = threading.Lock()

    def __init__(
        self,
        *,
        domain: str = "",
        api_url: str = "http://127.0.0.1:9000/api/v1",
        local_prefix: str = "reg",
        state_file: str = "",
        poll_interval: float | str = 3,
        request_timeout: float | str = 15,
        username: str = "",
        password: str = "",
        session: requests.Session | None = None,
    ):
        self.domain = _normalise_domain(domain)
        resolved_api_url = str(
            os.getenv("CHATGPT_INBUCKET_API_URL") or api_url or ""
        ).strip()
        parsed = urlparse(resolved_api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Inbucket API 地址无效，例如：http://127.0.0.1:9000/api/v1")
        self.api_url = resolved_api_url.rstrip("/")
        self.local_prefix = _local_prefix(local_prefix)
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.poll_interval = _positive_number(poll_interval, 3, minimum=0, maximum=60)
        self.request_timeout = _positive_number(request_timeout, 15, minimum=1, maximum=120)
        # Optional HTTP basic auth, e.g. when the Inbucket API is fronted by
        # nginx ``auth_basic`` can protect the local Inbucket endpoint.
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self._auth = (
            (self.username, self.password)
            if (self.username and self.password)
            else None
        )
        self.session = session or requests.Session()

    @classmethod
    def from_config(cls, config: dict) -> "InbucketDomainMailbox":
        return cls(
            domain=config.get("inbucket_domain", ""),
            api_url=config.get("inbucket_api_url", "http://127.0.0.1:9000/api/v1"),
            local_prefix=config.get("inbucket_local_prefix", "reg"),
            state_file=config.get("inbucket_state_file", ""),
            poll_interval=config.get("inbucket_poll_interval", 3),
            request_timeout=config.get("inbucket_request_timeout", 15),
            username=config.get("inbucket_username", ""),
            password=config.get("inbucket_password", ""),
        )

    def _state(self) -> dict:
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {"used": {}}
        except Exception:
            return {"used": {}}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _key(address: str) -> str:
        return str(address or "").strip().lower()

    def _new_address(self, used: set[str]) -> str:
        for _ in range(20):
            token = "".join(secrets.choice(string.ascii_lowercase) for _ in range(12))
            address = f"{self.local_prefix}-{token}@{self.domain}"
            if self._key(address) not in used:
                return address
        raise RuntimeError("无法生成未使用的 Inbucket 域名邮箱地址")

    def _reserve(self, address: str) -> None:
        state = self._state()
        used = dict(state.get("used") or {})
        used[self._key(address)] = {
            "email": address,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "source_id": hashlib.sha256(f"{self.domain}|{self.api_url}".encode()).hexdigest()[:16],
        }
        state["used"] = used
        self._save_state(state)

    def peek_email(self) -> str:
        return self._new_address(set((self._state().get("used") or {}).keys()))

    def get_email(self) -> MailboxAccount:
        with self._reservation_lock:
            address = self._new_address(set((self._state().get("used") or {}).keys()))
            self._reserve(address)
        return MailboxAccount(
            email=address,
            account_id=self._key(address),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "domain_inbucket",
                    "login_identifier": address,
                    "display_name": address,
                    "credentials": {"domain": self.domain, "inbucket_api_url": self.api_url},
                    "metadata": {"source": "inbucket_api"},
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "domain_inbucket",
                    "resource_type": "mailbox",
                    "resource_identifier": self._key(address),
                    "handle": address,
                    "display_name": address,
                    "metadata": {"email": address, "domain": self.domain, "delivery": "inbucket_smtp"},
                },
            },
        )

    def _mailbox_url(self, address: str, message_id: str = "") -> str:
        path = quote(self._key(address), safe="")
        if message_id:
            path += "/" + quote(str(message_id), safe="")
        return f"{self.api_url}/mailbox/{path}"

    def _request_json(self, url: str) -> object:
        response = self.session.get(
            url,
            headers={"Accept": "application/json"},
            timeout=self.request_timeout,
            auth=self._auth,
        )
        response.raise_for_status()
        return response.json()

    def test_connection(self) -> None:
        probe = f"healthcheck@{self.domain}"
        payload = self._request_json(self._mailbox_url(probe))
        if not isinstance(payload, list):
            raise RuntimeError("Inbucket API 未返回邮箱列表")

    def _messages(self, account: MailboxAccount) -> list[dict]:
        index = self._request_json(self._mailbox_url(account.email))
        if not isinstance(index, list):
            raise RuntimeError("Inbucket API 未返回邮件列表")
        messages: list[dict] = []
        for item in reversed(index[-50:]):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            detail = self._request_json(self._mailbox_url(account.email, str(item["id"])))
            if isinstance(detail, dict):
                messages.append(detail)
        return messages

    @staticmethod
    def _message_id(mail: dict) -> str:
        return str(mail.get("id") or "")

    @staticmethod
    def _message_text(mail: dict) -> str:
        body = mail.get("body") if isinstance(mail.get("body"), dict) else {}
        return "\n".join((
            str(mail.get("subject") or ""),
            str(body.get("text") or ""),
            str(body.get("html") or ""),
        ))

    @staticmethod
    def _clean_search_text(text: str) -> str:
        cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
        cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        return re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", " ", cleaned)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {self._message_id(mail) for mail in self._messages(account) if self._message_id(mail)}
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
            for mail in self._messages(account):
                message_id = self._message_id(mail)
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
        raise TimeoutError(f"等待 Inbucket 域名邮箱验证码超时 ({timeout}s)")

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
            for mail in self._messages(account):
                message_id = self._message_id(mail)
                if message_id in seen:
                    continue
                if message_id:
                    seen.add(message_id)
                link = _extract_verification_link(self._message_text(mail), keyword)
                if link:
                    return link
            time.sleep(self.poll_interval)
        raise TimeoutError(f"等待 Inbucket 域名邮箱验证链接超时 ({timeout}s)")
