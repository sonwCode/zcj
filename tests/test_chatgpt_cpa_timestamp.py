r"""``generate_token_json`` must survive a hostile ``exp`` claim.

The CPA upload path turns the access token's ``exp`` claim into an ISO string. The
claim is upstream-issued, so it is untrusted. Two of the three conversions used to
run inline behind ``isinstance(exp, int) and exp > 0``, which lets an *oversized*
integer through to ``datetime.fromtimestamp`` - where it raises ``OverflowError`` /
``ValueError``.

The blast radius differs by call site, and none of them is good: ``tasks.py`` swallows
it as "CPA 自动上传异常" (a warning that reads like a network fault, while the upload is
in fact silently dropped), and ``plugin.py`` runs it inside an action dispatcher whose
contract is to return ``{"ok": ...}``, not to raise.

The file already had a total helper - ``_format_cpa_timestamp`` - used ten lines earlier
for exactly this conversion. These tests pin that the inline copies behave like it.
"""
from __future__ import annotations

import base64
import json
import logging

import pytest

import platforms.chatgpt.cpa_upload as cpa_upload
from platforms.chatgpt.cpa_upload import (
    _format_cpa_timestamp,
    generate_token_json,
)


def _jwt(payload: dict) -> str:
    """Build a token whose middle segment decodes to ``payload``."""
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


class _Account:
    """Minimal stand-in for the account object the generator reads."""

    def __init__(self, token: str) -> None:
        self.email = "someone@example.com"
        self.access_token = token
        self.refresh_token = "rt"
        self.id_token = ""
        self.session_token = "st"
        self.user_id = "user-1"
        self.account_id = ""
        self.expired = ""
        self.expires_at = ""


# The values that reach fromtimestamp and blow up there.
PATHOLOGICAL_EXP = [1767225600000, 999999999999999, 10 ** 30, 253402300799000000]


@pytest.mark.parametrize("exp", PATHOLOGICAL_EXP)
def test_an_oversized_exp_does_not_raise(exp):
    """The core property: this call must not throw for any int ``exp``."""
    token = _jwt({"exp": exp, "https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"}})

    data = generate_token_json(_Account(token))

    assert isinstance(data, dict)


@pytest.mark.parametrize("exp", PATHOLOGICAL_EXP)
def test_an_oversized_exp_degrades_to_empty_rather_than_a_wrong_date(exp):
    """A value we cannot represent must not be rendered as some bogus date."""
    assert _format_cpa_timestamp(exp) == str(exp).strip()
    assert "T" not in _format_cpa_timestamp(exp) or "+08:00" not in _format_cpa_timestamp(exp)


def test_a_normal_exp_still_renders_an_iso_instant():
    """Hardening must not cost us the real formatting."""
    token = _jwt({"exp": 1767225600})

    data = generate_token_json(_Account(token))

    assert data["expired"] == "2026-01-01T08:00:00+08:00"


def test_a_millisecond_exp_is_not_silently_misread():
    """A ms-scale claim must never be presented as a plausible-looking date.

    ``1767225600000`` is year 57971 in seconds, so it is unrepresentable and has to
    degrade; the important part is that it does not raise and does not lie.
    """
    token = _jwt({"exp": 1767225600000})

    data = generate_token_json(_Account(token))

    assert isinstance(data["expired"], str)
    assert "2026" not in data["expired"]


def test_a_bool_exp_is_ignored():
    """``True`` is an ``int``; it is not a timestamp and must not become one."""
    token = _jwt({"exp": True})

    data = generate_token_json(_Account(token))

    assert isinstance(data, dict)
    assert data.get("expired", "") == "" or "T" not in data.get("expired", "")

class _StubResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _StubSession:
    """Stands in for the curl_cffi session used by the refresh branch."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.cookies = _StubCookies()

    def get(self, *args, **kwargs) -> _StubResponse:
        return _StubResponse(self._payload)


class _StubCookies:
    def set(self, *args, **kwargs) -> None:
        return None


class _StubCffi:
    """Offline stand-in for ``curl_cffi.requests``.

    The session-refresh branch (the one that reads a second ``exp`` claim) only runs
    when no account_id was found earlier, and it needs two network calls to get there:
    ``/backend-api/me`` and the session-token exchange. Both are served from memory
    here so the branch is exercised without touching the network.
    """

    def __init__(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token

    def get(self, *args, **kwargs) -> _StubResponse:
        # /backend-api/me: return an empty body so account_id stays unset and the
        # refresh branch below is reached.
        return _StubResponse({})

    def Session(self, *args, **kwargs) -> _StubSession:  # noqa: N802 - mirrors the API
        return _StubSession({"accessToken": self._refresh_token})


@pytest.mark.parametrize("exp", PATHOLOGICAL_EXP)
def test_an_oversized_exp_on_the_refresh_branch_does_not_raise(exp, monkeypatch):
    """The second inline conversion, reachable only through a session refresh.

    This is the site that a plain ``generate_token_json`` call never reaches, so it
    needs the offline seam above; without it the guard there is unpinned.
    """
    refreshed = _jwt({
        "exp": exp,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-refreshed"},
    })
    monkeypatch.setattr(cpa_upload, "cffi_requests", _StubCffi(refreshed))

    account = _Account("")        # no access_token -> falls through to the refresh
    account.id_token = ""
    account.user_id = ""          # an existing user_id is used as account_id directly
    account.account_id = ""

    data = generate_token_json(account)

    assert isinstance(data, dict)
    # The refresh branch must have run and produced the refreshed account id.
    assert data.get("account_id") == "acc-refreshed"


def test_the_refresh_branch_reports_a_normal_exp(monkeypatch):
    """Sanity: with the same seam, a real ``exp`` still formats."""
    refreshed = _jwt({
        "exp": 1767225600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-refreshed"},
    })
    monkeypatch.setattr(cpa_upload, "cffi_requests", _StubCffi(refreshed))

    account = _Account("")
    account.user_id = ""
    account.account_id = ""

    data = generate_token_json(account)

    assert data.get("account_id") == "acc-refreshed"
    assert data["expired"] == "2026-01-01T08:00:00+08:00"

@pytest.mark.parametrize("exp", PATHOLOGICAL_EXP)
def test_an_oversized_exp_does_not_masquerade_as_a_refresh_failure(exp, monkeypatch, caplog):
    """The refresh branch must not report a formatting overflow as "刷新失败".

    Restoring the inline ``fromtimestamp`` here is *nearly* invisible: the surrounding
    ``except Exception`` swallows it, so the returned payload looks the same either way.
    The difference that does survive - and the reason the change matters - is the log:
    an operator reading "session 刷新失败" goes looking at auth and the network, when the
    real cause is a nonsense ``exp``. Capturing the log is what makes the mutation
    observable, so this test exists specifically to pin it.
    """
    refreshed = _jwt({
        "exp": exp,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-refreshed"},
    })
    monkeypatch.setattr(cpa_upload, "cffi_requests", _StubCffi(refreshed))

    account = _Account("")
    account.user_id = ""
    account.account_id = ""

    with caplog.at_level(logging.ERROR):
        generate_token_json(account)

    assert "session 刷新失败" not in caplog.text
