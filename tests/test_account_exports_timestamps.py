r"""Export payloads must survive a hostile JWT ``exp``/``iat`` claim.

``_chatgpt_export_payload`` turns the access token's ``exp``/``iat`` claims into
``expires_at`` / ``last_refresh``. The claims come from stored tokens, so they are
untrusted, and the old inline guard - ``isinstance(v, int) and v > 0`` - accepted any
positive integer, including a millisecond epoch or a corrupt sentinel, which then
raised inside ``datetime.fromtimestamp`` (``ValueError`` / ``OverflowError`` /
``OSError``).

The reason it matters is the call shape, not the math: ``export_chatgpt_cockpit``
builds its payload with ``[_make_cockpit_token(item) for item in items]``, and
``_chatgpt_export_payload`` has no surrounding ``try``. One bad account therefore
aborted an entire bulk export - select fifty accounts, get nothing back.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest

from application.account_exports import (
    AccountExportSelection,
    AccountExportsService,
    _datetime_from_timestamp,
    _make_cockpit_token,
)
from domain.accounts import AccountRecord


def _jwt(payload: dict) -> str:
    """A token whose middle segment decodes to ``payload``."""
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


def _record(account_id: int, exp=None, iat=None) -> AccountRecord:
    claims: dict = {}
    if exp is not None:
        claims["exp"] = exp
    if iat is not None:
        claims["iat"] = iat
    return AccountRecord(
        id=account_id,
        platform="chatgpt",
        email=f"user{account_id}@example.com",
        password="pw",
        user_id=f"user-{account_id}",
        credentials=[
            {"scope": "platform", "key": "access_token", "value": _jwt(claims)},
            {"scope": "platform", "key": "refresh_token", "value": "rt"},
            {"scope": "platform", "key": "session_token", "value": "st"},
        ],
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


# The values that blow up on the way in or out.
#
# The ints overshoot ``datetime.fromtimestamp`` (ValueError / OSError). The floats
# are caught one step earlier: ``int(float("inf"))`` raises OverflowError inside the
# helper's own ``int()`` guard, and ``int(float("nan"))`` raises ValueError. Both are
# reachable, so both are pinned - a guard nothing can reach is not a guard.
PATHOLOGICAL = [
    1767225600000,        # millisecond epoch -> year 57971 in seconds
    999999999999999,      # oversized sentinel
    10 ** 30,
    253402300799000000,   # OSError out of the C library
    float("inf"),         # int() -> OverflowError
    float("-inf"),
    float("nan"),         # int() -> ValueError
]


@pytest.mark.parametrize("value", PATHOLOGICAL)
def test_a_pathological_claim_becomes_none(value):
    assert _datetime_from_timestamp(value) is None


@pytest.mark.parametrize("value", PATHOLOGICAL)
def test_a_pathological_claim_never_escapes_an_export(value):
    """The property that matters: building the export must not raise."""
    payload = _make_cockpit_token(_record(1, exp=value, iat=value))

    assert isinstance(payload, dict)
    assert payload["expired"] == ""


def test_zero_negative_and_bool_are_not_timestamps():
    for value in (0, -1, 0.0, True, False):
        assert _datetime_from_timestamp(value) is None


def test_real_claims_still_render():
    """Hardening must not cost the normal case."""
    moment = _datetime_from_timestamp(1767225600)

    assert moment == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_a_numeric_string_claim_is_accepted():
    assert _datetime_from_timestamp("1767225600") == _datetime_from_timestamp(1767225600)


def test_the_export_falls_back_to_updated_at_when_iat_is_unusable():
    """``last_refresh`` has an explicit fallback; a bad iat must reach it."""
    payload = _make_cockpit_token(_record(1, iat=999999999999999))

    assert payload["last_refresh"] == "2026-01-02T00:00:00Z"


def test_one_bad_account_does_not_abort_a_bulk_export():
    """The concrete blast radius, driven through the real service.

    Three accounts are selected and the middle one carries a millisecond ``exp``.
    Before the fix the whole call raised, so the user lost all three; now every
    account is exported and only the bad one loses its ``expires_at``.
    """

    class _Repo:
        def select_for_export(self, selection):  # noqa: ANN001 - duck-typed
            return [
                _record(1, exp=1767225600),
                _record(2, exp=1767225600000),   # the bad one
                _record(3, exp=1767225600),
            ]

    service = AccountExportsService(_Repo())
    artifact = service.export_chatgpt_cockpit(AccountExportSelection(platform="chatgpt"))

    tokens = json.loads(artifact.content)

    assert len(tokens) == 3
    assert [t["email"] for t in tokens] == [
        "user1@example.com", "user2@example.com", "user3@example.com",
    ]
    assert tokens[0]["expired"] == "2026-01-01T00:00:00Z"
    assert tokens[1]["expired"] == ""            # degraded, not fatal
    assert tokens[2]["expired"] == "2026-01-01T00:00:00Z"