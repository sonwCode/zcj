import json

import pytest
import requests

from core.base_mailbox import MailboxAccount
from core.local_ms_mailbox import (
    LocalMicrosoftMailboxEntry,
    LocalMicrosoftMailboxPool,
    build_inline_mailbox_url_pool_text,
    generate_gmail_plus_aliases,
    generate_microsoft_pool_aliases,
    generate_plus_aliases,
    parse_local_ms_pool_rows,
    split_unused_local_ms_pool_rows,
)


def test_local_ms_pool_entry_proxy_overrides_provider_proxy(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"access_token":"token"}'

        def json(self):
            return {"access_token": "token"}

    def fake_post(url, data, proxies=None, timeout=None):
        calls.append(proxies)
        return FakeResponse()

    monkeypatch.setattr("core.local_ms_mailbox.requests.post", fake_post)
    pool = LocalMicrosoftMailboxPool(proxy="http://pool-user:pool-pass@pool.example:8000")
    entry = LocalMicrosoftMailboxEntry(
        email="user@example.com",
        client_id="client-id",
        refresh_token="refresh-token",
        proxy="entry.example:9000@entry-user:entry-pass",
    )

    assert pool._graph_access_token(entry) == "token"
    assert calls == [{
        "http": "http://entry-user:entry-pass@entry.example:9000",
        "https": "http://entry-user:entry-pass@entry.example:9000",
    }]


def test_local_ms_pool_proxy_failure_falls_back_to_direct(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"access_token":"token"}'

        def json(self):
            return {"access_token": "token"}

    def fake_post(url, data, proxies=None, timeout=None):
        calls.append(proxies)
        if proxies:
            raise requests.exceptions.ProxyError("tunnel failed")
        return FakeResponse()

    monkeypatch.setattr("core.local_ms_mailbox.requests.post", fake_post)
    pool = LocalMicrosoftMailboxPool(proxy="http://mailbox-proxy.example:8000")
    entry = LocalMicrosoftMailboxEntry(
        email="user@example.com",
        client_id="client-id",
        refresh_token="refresh-token",
    )

    assert pool._graph_access_token(entry) == "token"
    assert calls == [
        {
            "http": "http://mailbox-proxy.example:8000",
            "https": "http://mailbox-proxy.example:8000",
        },
        None,
    ]


def test_local_ms_pool_factory_does_not_reuse_registration_proxy_by_default():
    from core.base_mailbox import _create_local_ms_pool

    pool = _create_local_ms_pool(
        {"local_ms_pool_text": "user@example.com----pass----client----refresh"},
        "http://registration-proxy.example:9000",
    )

    assert pool.proxy is None


def test_local_ms_pool_factory_can_explicitly_reuse_registration_proxy():
    from core.base_mailbox import _create_local_ms_pool

    pool = _create_local_ms_pool(
        {
            "local_ms_pool_text": "user@example.com----pass----client----refresh",
            "local_ms_use_registration_proxy": True,
        },
        "http://registration-proxy.example:9000",
    )

    assert pool.proxy == {
        "http": "http://registration-proxy.example:9000",
        "https": "http://registration-proxy.example:9000",
    }


def test_split_unused_local_ms_pool_rows_filters_used_and_duplicates(tmp_path):
    state_file = tmp_path / "pool-state.json"
    state_file.write_text(
        json.dumps({
            "used": {"used@example.com": {"email": "used@example.com"}},
            "blocked": {"blocked@example.com": {"email": "blocked@example.com"}},
        }),
        encoding="utf-8",
    )
    result = split_unused_local_ms_pool_rows(
        "\n".join([
            "used@example.com----pass----client----refresh",
            "blocked@example.com----pass----client----refresh",
            "fresh@example.com----pass2----client2----refresh2",
            "FRESH@example.com----pass3----client3----refresh3",
            "invalid-row",
        ]),
        state_file=str(state_file),
    )

    assert result.unused_count == 1
    assert result.used_count == 1
    assert result.blocked_count == 1
    assert result.duplicate_count == 1
    assert result.invalid_count == 1
    assert result.pool_text.startswith("fresh@example.com----")


def test_parse_local_ms_pool_rows_accepts_gujumpgate_hotmail_format():
    rows = parse_local_ms_pool_rows(
        "\n".join(
            [
                "account----password----ID----Token",
                "user@example.com----mail-pass----client-id-123----refresh-token-456",
            ]
        )
    )

    assert len(rows) == 1
    entry = rows[0]
    assert entry.email == "user@example.com"
    assert entry.password == "mail-pass"
    assert entry.login_account == "user@example.com"
    assert entry.client_id == "client-id-123"
    assert entry.refresh_token == "refresh-token-456"
    assert entry.source_format == "gujumpgate_hotmail"
    assert entry.graph_ready is True
    assert entry.imap_ready is False


def test_parse_local_ms_pool_rows_skips_gmail_totp_card_format():
    rows = parse_local_ms_pool_rows(
        "fixture.account@gmail.com----fixture-login----fixture.recovery@example.com"
        "----jbswy3dpehpk3pxp----2022----Example|VERIFY"
    )

    assert rows == []


def test_parse_local_ms_pool_rows_accepts_mailbox_url_format():
    rows = parse_local_ms_pool_rows(
        "user@gmail.com----https://mail-api.example.com/api/code/fetch?token=TEST_TOKEN&uid=TEST_UID"
    )

    assert len(rows) == 1
    entry = rows[0]
    assert entry.email == "user@gmail.com"
    assert entry.mailbox_url == "https://mail-api.example.com/api/code/fetch?token=TEST_TOKEN&uid=TEST_UID"
    assert entry.source_format == "mailbox_url"
    assert entry.url_ready is True
    assert entry.graph_ready is False
    assert entry.imap_ready is False


def test_generate_gmail_plus_aliases_make_unique_aliases():
    aliases = generate_gmail_plus_aliases("base@gmail.com", 5)

    assert len(aliases) == 5
    assert len(set(aliases)) == 5
    assert all(item.startswith("base+") and item.endswith("@gmail.com") for item in aliases)


def test_build_inline_mailbox_url_pool_text_expands_gmail_aliases():
    result = build_inline_mailbox_url_pool_text(
        "base@gmail.com----https://gapi.example/code",
        gmail_alias_enabled=True,
        gmail_alias_count=3,
    )
    rows = result.pool_text.splitlines()

    assert result.source_count == 1
    assert result.expanded_count == 3
    assert result.alias_count == 3
    assert len(rows) == 3
    assert all(row.endswith("----https://gapi.example/code") for row in rows)
    assert all(row.split("----", 1)[0].startswith("base+") for row in rows)


def test_build_inline_mailbox_pool_expands_hotmail_and_keeps_graph_credentials():
    result = build_inline_mailbox_url_pool_text(
        "base@hotmail.com----mail-pass----client-id----refresh-token",
        gmail_alias_enabled=True,
        gmail_alias_count=2,
    )
    entries = parse_local_ms_pool_rows(result.pool_text)

    assert result.source_count == 1
    assert result.expanded_count == 2
    assert result.alias_count == 2
    assert len(entries) == 2
    assert len({entry.email for entry in entries}) == 2
    assert all(entry.email.startswith("base+") for entry in entries)
    assert all(entry.email.endswith("@hotmail.com") for entry in entries)
    assert all("+reg" not in entry.email for entry in entries)
    assert all(entry.login_account == "base@hotmail.com" for entry in entries)
    assert all(entry.client_id == "client-id" for entry in entries)
    assert all(entry.refresh_token == "refresh-token" for entry in entries)


@pytest.mark.parametrize("domain", ["outlook.com", "hotmail.com", "live.com", "msn.com"])
def test_generate_plus_aliases_supports_microsoft_consumer_domains(domain):
    aliases = generate_plus_aliases(f"base@{domain}", 3)

    assert len(set(aliases)) == 3
    assert all(alias.startswith("base+") and alias.endswith(f"@{domain}") for alias in aliases)


@pytest.mark.parametrize("domain", ["outlook.com", "hotmail.com", "live.com", "msn.com"])
def test_generate_microsoft_pool_aliases_are_random_unique(domain):
    aliases = generate_microsoft_pool_aliases(f"base@{domain}", 3)

    assert len(set(aliases)) == 3
    assert all(alias.startswith("base+") and alias.endswith(f"@{domain}") for alias in aliases)
    assert all("+reg" not in alias for alias in aliases)
    # Different calls should not keep collapsing to the old deterministic regN sequence.
    again = generate_microsoft_pool_aliases(f"base@{domain}", 3, existing=set(aliases))
    assert len(set(aliases + again)) == 6


def test_local_ms_pool_expands_hotmail_aliases(tmp_path):
    pool = LocalMicrosoftMailboxPool.from_config({
        "local_ms_pool_text": "base@hotmail.com----mail-pass----client-id----refresh-token",
        "local_ms_pool_state_file": str(tmp_path / "state.json"),
        "gmail_alias_enabled": True,
        "gmail_alias_count": 2,
    })

    accounts = [pool.get_email(), pool.get_email()]

    assert len({account.email for account in accounts}) == 2
    assert all(account.email.startswith("base+") and account.email.endswith("@hotmail.com") for account in accounts)
    assert all("+reg" not in account.email for account in accounts)
    for account in accounts:
        credentials = account.extra["provider_account"]["credentials"]
        assert credentials["login_account"] == "base@hotmail.com"
        assert credentials["client_id"] == "client-id"
        assert credentials["refresh_token"] == "refresh-token"


def test_local_ms_pool_factory_forwards_task_alias_options(tmp_path):
    from core.base_mailbox import _create_local_ms_pool

    pool = _create_local_ms_pool(
        {
            "local_ms_pool_text": "base@hotmail.com----mail-pass----client-id----refresh-token",
            "local_ms_pool_state_file": str(tmp_path / "state.json"),
            "gmail_alias_enabled": True,
            "gmail_alias_count": 3,
        },
        None,
    )

    assert pool.plus_alias_enabled is True
    assert pool.plus_alias_count == 3
    accounts = [pool.get_email() for _ in range(3)]
    assert len({account.email for account in accounts}) == 3
    assert all(account.email.startswith("base+") for account in accounts)
    assert all("+reg" not in account.email for account in accounts)
    assert all(
        account.extra["provider_account"]["credentials"]["login_account"] == "base@hotmail.com"
        for account in accounts
    )


def test_hotmail_alias_setting_can_use_new_aliases_after_base_was_used(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({"used": {"base@hotmail.com": {"email": "base@hotmail.com"}}}),
        encoding="utf-8",
    )
    expanded = build_inline_mailbox_url_pool_text(
        "base@hotmail.com----mail-pass----client-id----refresh-token",
        gmail_alias_enabled=True,
        gmail_alias_count=2,
    )
    unused = split_unused_local_ms_pool_rows(expanded.pool_text, state_file=str(state_file))

    assert unused.unused_count == 2
    assert unused.used_count == 0
    assert all("base+" in line for line in unused.pool_text.splitlines())


def test_hotmail_alias_expansion_uses_requested_tags_per_distinct_base_mailbox():
    result = build_inline_mailbox_url_pool_text(
        "\n".join([
            "first@hotmail.com----pass-1----client-1----refresh-1",
            "second@outlook.com----pass-2----client-2----refresh-2",
            "third@live.com----pass-3----client-3----refresh-3",
        ]),
        gmail_alias_enabled=True,
        gmail_alias_count=10,
    )
    entries = parse_local_ms_pool_rows(result.pool_text)

    assert result.expanded_count == 30
    assert result.alias_count == 30
    first_batch = entries[:10]
    assert len({entry.email for entry in first_batch}) == 10
    assert all(entry.email.startswith("first+") and entry.email.endswith("@hotmail.com") for entry in first_batch)
    assert all("+reg" not in entry.email for entry in first_batch)
    assert all(entry.login_account == "first@hotmail.com" for entry in first_batch)
    assert all("+" in entry.email.split("@", 1)[0] for entry in entries)

    # Random aliases: a second expansion for the same base should still be unique
    # and not collapse back to the old deterministic +regN sequence.
    repeated = build_inline_mailbox_url_pool_text(
        "first@hotmail.com----pass-1----client-1----refresh-1",
        gmail_alias_enabled=True,
        gmail_alias_count=10,
    )
    repeated_entries = parse_local_ms_pool_rows(repeated.pool_text)
    assert len(repeated_entries) == 10
    assert all(entry.email.startswith("first+") for entry in repeated_entries)
    assert all("+reg" not in entry.email for entry in repeated_entries)


def test_child_mailbox_otp_filter_matches_only_assigned_graph_recipient():
    account = MailboxAccount(email="parent+reg2@outlook.com")

    assert LocalMicrosoftMailboxPool._message_is_for_account(
        {"toRecipients": [{"emailAddress": {"address": "parent+reg2@outlook.com"}}]},
        account,
    )
    assert not LocalMicrosoftMailboxPool._message_is_for_account(
        {"toRecipients": [{"emailAddress": {"address": "parent+reg1@outlook.com"}}]},
        account,
    )


def test_child_mailbox_otp_filter_matches_imap_to_header():
    account = MailboxAccount(email="parent+reg2@hotmail.com")

    assert LocalMicrosoftMailboxPool._message_is_for_account(
        {"to": "Parent <parent+reg2@hotmail.com>"},
        account,
    )
    assert not LocalMicrosoftMailboxPool._message_is_for_account(
        {"to": "Parent <parent+reg1@hotmail.com>"},
        account,
    )


def test_local_ms_pool_mailbox_url_wait_for_code(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 200
        text = '{"code":"123456","time":"2026-07-04T00:00:00Z"}'

        def json(self):
            return {"code": "123456", "time": "2026-07-04T00:00:00Z"}

    calls = []

    def fake_get(url, headers=None, proxies=None, timeout=None):
        calls.append({"url": url, "timeout": timeout})
        assert url == "https://mail-api.example.com/api/code/fetch?token=TEST_TOKEN&uid=TEST_UID"
        return FakeResponse()

    monkeypatch.setattr("core.local_ms_mailbox.requests.get", fake_get)
    pool = LocalMicrosoftMailboxPool(
        pool_text="user@gmail.com----https://mail-api.example.com/api/code/fetch?token=TEST_TOKEN&uid=TEST_UID",
        state_file=str(tmp_path / "state.json"),
        allow_reuse=True,
    )
    account = pool.get_email()

    assert account.email == "user@gmail.com"
    assert account.extra["provider_account"]["metadata"]["source_format"] == "mailbox_url"
    assert account.extra["provider_account"]["metadata"]["has_mailbox_url"] is True
    assert pool.wait_for_code(account, timeout=5) == "123456"
    assert calls[0]["timeout"] == 8


def test_local_ms_pool_mailbox_url_fast_polling_config_is_tolerant():
    pool = LocalMicrosoftMailboxPool.from_config(
        {
            "local_ms_pool_text": "user@gmail.com----https://example.test/code",
            "local_ms_mailbox_url_timeout": "bad",
            "local_ms_mailbox_url_poll_interval": "1",
        }
    )

    assert pool.mailbox_url_timeout == 8
    assert pool.mailbox_url_poll_interval == 1


def test_local_ms_pool_reports_gmail_totp_card_format_as_unsupported(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text=(
            "fixture.account@gmail.com----fixture-login----fixture.recovery@example.com"
            "----jbswy3dpehpk3pxp----2022----Example|VERIFY"
        ),
        state_file=str(tmp_path / "state.json"),
    )

    try:
        pool.get_email()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected unsupported Gmail card format error")

    assert "Gmail 登录卡密格式" in message
    assert "已跳过 Gmail 卡密 1 行" in message


def test_local_ms_pool_records_gujumpgate_source_metadata(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text="user@example.com----mail-pass----client-id-123----refresh-token-456",
        state_file=str(tmp_path / "state.json"),
    )

    account = pool.get_email()
    provider_account = account.extra["provider_account"]
    provider_resource = account.extra["provider_resource"]

    assert provider_account["credentials"]["client_id"] == "client-id-123"
    assert provider_account["credentials"]["refresh_token"] == "refresh-token-456"
    assert provider_account["metadata"]["source"] == "gujumpgate_hotmail"
    assert provider_account["metadata"]["credential_purpose"] == "otp_mailbox"
    assert provider_account["metadata"]["refresh_token_role"] == "microsoft_mailbox_oauth"
    assert provider_account["metadata"]["not_platform_refresh_token"] is True
    assert provider_resource["metadata"]["source"] == "gujumpgate_hotmail"


def test_local_ms_pool_never_reuses_blocked_identity(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text=(
            "blocked@hotmail.com----https://example.test/blocked\n"
            "next@hotmail.com----https://example.test/next"
        ),
        state_file=str(tmp_path / "state.json"),
        allow_reuse=True,
    )
    blocked = pool.get_email()
    assert blocked.email == "blocked@hotmail.com"

    assert pool.mark_registration_failure(blocked, "account_deactivated") is True

    assert pool.get_email().email == "next@hotmail.com"


def test_hotmail_failure_skips_only_current_child_address(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text="base@hotmail.com----mail-pass----client-id----refresh-token",
        state_file=str(tmp_path / "state.json"),
        plus_alias_enabled=True,
        plus_alias_count=3,
    )

    first = pool.get_email()
    assert first.email.startswith("base+")
    assert first.email.endswith("@hotmail.com")
    assert pool.mark_registration_failure(first, "user_already_exists") is True

    second = pool.get_email()
    assert second.email.startswith("base+")
    assert second.email != first.email
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert first.email.lower() in state["blocked"]
    assert state["blocked"][first.email.lower()]["reason_code"] == "already_registered"
    assert "base@hotmail.com" not in state["blocked"]

    inventory = pool.inventory()
    assert inventory["total_count"] == 3
    assert inventory["available_count"] == 1
    assert inventory["used_count"] == 1
    assert inventory["blocked_count"] == 1
    blocked_item = next(item for item in inventory["items"] if item["status"] == "blocked")
    assert blocked_item["email"] == first.email.lower()
    assert blocked_item["reason_code"] == "already_registered"


def test_local_ms_pool_inventory_records_failure_and_success(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text=(
            "first@hotmail.com----https://example.test/first\n"
            "second@hotmail.com----https://example.test/second"
        ),
        state_file=str(tmp_path / "state.json"),
    )
    first = pool.get_email()
    assert pool.mark_attempt_failure(first, "Graph refresh token invalid_grant") is True
    second = pool.get_email()
    assert pool.mark_registration_success(second) == ["second@hotmail.com"]

    inventory = pool.inventory()
    first_item = next(item for item in inventory["items"] if item["email"] == "first@hotmail.com")
    second_item = next(item for item in inventory["items"] if item["email"] == "second@hotmail.com")
    assert first_item["status"] == "used"
    assert "invalid_grant" in first_item["reason"]
    assert second_item["status"] == "used"
    assert second_item["completed_at"]


def test_transient_network_failure_is_released_but_retired_for_current_task(tmp_path):
    state_file = tmp_path / "state.json"
    pool_text = (
        "first@hotmail.com----https://example.test/first\n"
        "second@hotmail.com----https://example.test/second"
    )
    pool = LocalMicrosoftMailboxPool(
        pool_text=pool_text,
        state_file=str(state_file),
    )

    first = pool.get_email()
    assert first.email == "first@hotmail.com"
    assert pool.release_transient_failure(
        first,
        "mailbox_network_error: ProxyError",
    ) is True
    assert pool.get_email().email == "second@hotmail.com"

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "first@hotmail.com" not in state["used"]
    assert state["failures"]["first@hotmail.com"]["reason_code"] == "transient_network"

    next_task_pool = LocalMicrosoftMailboxPool(
        pool_text=pool_text,
        state_file=str(state_file),
    )
    assert next_task_pool.get_email().email == "first@hotmail.com"


def test_unconfirmed_otp_delivery_releases_persistent_mailbox_reservation(tmp_path):
    state_file = tmp_path / "state.json"
    pool_text = (
        "first@hotmail.com----https://example.test/first\n"
        "second@hotmail.com----https://example.test/second"
    )
    pool = LocalMicrosoftMailboxPool(
        pool_text=pool_text,
        state_file=str(state_file),
    )

    first = pool.get_email()
    assert pool.release_uncommitted_failure(
        first,
        "otp_delivery_failed: HTTP 409",
    ) is True
    assert pool.get_email().email == "second@hotmail.com"

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "first@hotmail.com" not in state["used"]
    assert state["failures"]["first@hotmail.com"]["reason_code"] == "uncommitted_attempt"

    next_task_pool = LocalMicrosoftMailboxPool(
        pool_text=pool_text,
        state_file=str(state_file),
    )
    assert next_task_pool.get_email().email == "first@hotmail.com"


def test_recover_transient_failures_releases_stale_network_reservation(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "used": {
                    "first@hotmail.com": {
                        "email": "first@hotmail.com",
                        "reserved_at": "2026-08-24T00:00:00Z",
                    },
                    "successful@hotmail.com": {
                        "email": "successful@hotmail.com",
                        "outcome": "success",
                        "completed_at": "2026-08-24T00:01:00Z",
                    },
                },
                "blocked": {},
                "failures": {
                    "first@hotmail.com": {
                        "reason": "Microsoft refresh_token failed: ProxyError: tunnel connection failed",
                        "reason_code": "registration_failed",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    pool = LocalMicrosoftMailboxPool(
        pool_text=(
            "first@hotmail.com----https://example.test/first\n"
            "successful@hotmail.com----https://example.test/success"
        ),
        state_file=str(state_file),
    )

    assert pool.recover_transient_failures() == 1
    assert pool.get_email().email == "first@hotmail.com"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["used"]["successful@hotmail.com"]["outcome"] == "success"


def test_graph_access_token_tries_fallback_endpoint(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_post(url, data, proxies=None, timeout=None):
        calls.append((url, data))
        if len(calls) == 1:
            return FakeResponse(400, text='{"error":"invalid_request"}')
        return FakeResponse(200, {"access_token": "access-token-ok"})

    monkeypatch.setattr("core.local_ms_mailbox.requests.post", fake_post)
    pool = LocalMicrosoftMailboxPool()
    account = MailboxAccount(
        email="user@example.com",
        account_id="user@example.com",
        extra={
            "provider_account": {
                "credentials": {
                    "email": "user@example.com",
                    "client_id": "client-id-123",
                    "refresh_token": "refresh-token-456",
                }
            }
        },
    )
    entry = pool._entry_for_account(account)

    assert pool._graph_access_token(entry) == "access-token-ok"
    assert len(calls) == 2
    assert calls[0][0] == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert calls[1][0] == "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
