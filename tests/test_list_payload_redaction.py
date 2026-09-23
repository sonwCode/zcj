"""Regression tests: the high-frequency account list must never carry secrets.

The list endpoint feeds a table that is polled continuously, so any credential
that leaks here is exposed far more widely than the detail endpoint.
"""
from __future__ import annotations

import datetime
import json

from application.accounts import AccountsService
from domain.accounts import AccountRecord

MAILBOX_TOKEN = "MAILBOX-ACCESS-TOKEN-LEAKED"
ACCOUNT_PASSWORD = "SuperSecretPw"
PRIMARY_TOKEN = "sk-realtoken-abc123"
REFRESH_TOKEN = "rt-secret-value"
PROVIDER_PASSWORD = "prov-secret"
OVERVIEW_TOKEN = "overview-access-token"


def _record() -> AccountRecord:
    now = datetime.datetime.now(datetime.timezone.utc)
    return AccountRecord(
        id=1,
        platform="chatgpt",
        email="victim@example.com",
        password=ACCOUNT_PASSWORD,
        user_id="u1",
        primary_token=PRIMARY_TOKEN,
        trial_end_time=0,
        cashier_url="",
        lifecycle_status="registered",
        validity_status="valid",
        plan_state="free",
        plan_name="Free",
        display_status="registered",
        overview={"access_token": OVERVIEW_TOKEN},
        display_summary={},
        credentials=[{"key": "refresh_token", "value": REFRESH_TOKEN, "metadata": {}}],
        provider_accounts=[{"credentials": {"password": PROVIDER_PASSWORD}}],
        provider_resources=[
            {
                "id": 9,
                "provider_type": "mailbox",
                "provider_name": "tempmail_lol",
                "resource_type": "mailbox",
                "resource_identifier": MAILBOX_TOKEN,
                "handle": "victim@example.com",
                "display_name": "victim@example.com",
                "metadata": {"email": "victim@example.com", "token": MAILBOX_TOKEN},
            }
        ],
        created_at=now,
        updated_at=now,
    )


def test_list_payload_hides_every_secret() -> None:
    payload = json.dumps(
        AccountsService._serialize(_record(), include_secrets=False), default=str
    )
    for label, secret in {
        "provider_resources metadata token": MAILBOX_TOKEN,
        "provider_resources resource_identifier": MAILBOX_TOKEN,
        "account password": ACCOUNT_PASSWORD,
        "primary token": PRIMARY_TOKEN,
        "credential value": REFRESH_TOKEN,
        "provider account password": PROVIDER_PASSWORD,
        "overview access_token": OVERVIEW_TOKEN,
    }.items():
        assert secret not in payload, f"list payload leaked {label}"


def test_list_payload_keeps_mailbox_badge_fields() -> None:
    """The UI derives its verification-mailbox badge from these fields."""
    item = AccountsService._serialize(_record(), include_secrets=False)["provider_resources"][0]
    assert item["provider_name"] == "tempmail_lol"
    assert item["resource_type"] == "mailbox"
    assert item["handle"] == "victim@example.com"
    assert item["display_name"] == "victim@example.com"
    assert item["resource_identifier_present"] is True


def test_detail_payload_still_returns_secrets() -> None:
    """Redaction must only apply to the list view."""
    payload = json.dumps(
        AccountsService._serialize(_record(), include_secrets=True), default=str
    )
    assert MAILBOX_TOKEN in payload
    assert ACCOUNT_PASSWORD in payload
