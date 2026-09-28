"""The persistence boundary: the single predicate that guards a database write.

Historically the executor wrote an account row as soon as the platform call returned, so
"registered" was ambiguous - a run that reached the account-creation screen but never
obtained a usable credential could still be persisted. This module narrows that to one
observable condition, ``stable-http-200-at-v1``: a non-empty identity, a success status
from the platform layer, and an access token long enough to have come from a real 2xx
credential exchange.

The token gate is the whole point of the boundary, so most of what follows pins it: it is
a length test, it reads any of the documented key spellings, and it can be satisfied
through an ``extra`` mapping. The status gate is deliberately permissive - an absent or
blank status passes, because the token is the stronger evidence - and that asymmetry is
pinned too, so nobody "tightens" it into rejecting every result that omits a status.
"""
from __future__ import annotations

import pytest

from core.registration.persistence import (
    BOUNDARY_VERSION,
    MIN_ACCESS_TOKEN_LENGTH,
    PersistenceDecision,
    boundary_summary,
    evaluate_persistence,
)


LONG_TOKEN = "t" * MIN_ACCESS_TOKEN_LENGTH
SHORT_TOKEN = "t" * (MIN_ACCESS_TOKEN_LENGTH - 1)
IDENTITY = {"email": "user@example.com"}


def result(**kwargs):
    return {**IDENTITY, "token": LONG_TOKEN, **kwargs}


class _Object:
    """A non-dict result, since the boundary reads attributes as well as keys."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Status:
    """Mimics an enum-ish status object with a ``value`` member."""

    def __init__(self, value):
        self.value = value


# -- the happy path -----------------------------------------------------------


def test_a_complete_result_is_persistable():
    decision = evaluate_persistence(result())
    assert decision.ok is True
    assert decision.code == "ok"
    assert decision.detail == ""


def test_the_decision_reports_the_boundary_it_was_made_under():
    assert evaluate_persistence(result()).boundary == BOUNDARY_VERSION


def test_the_boundary_version_names_the_observable_condition():
    """Operators grep for this string, so it must stay self-describing."""
    assert BOUNDARY_VERSION == "stable-http-200-at-v1"


# -- the empty and identity gates ---------------------------------------------


def test_nothing_at_all_is_refused():
    decision = evaluate_persistence(None)
    assert (decision.ok, decision.code) == (False, "empty_result")


@pytest.mark.parametrize("identity_key", ["email", "user_id", "userId", "account_id"])
def test_any_documented_identity_key_satisfies_the_gate(identity_key):
    payload = {"token": LONG_TOKEN, "status": "success", identity_key: "someone"}
    assert evaluate_persistence(payload).ok is True


def test_an_empty_identity_is_refused():
    decision = evaluate_persistence({"email": "   ", "token": LONG_TOKEN})
    assert (decision.ok, decision.code) == (False, "missing_identity")


def test_the_identity_gate_is_checked_before_the_token_gate():
    """An operator needs to hear "no identity", not "no token", when both are absent."""
    decision = evaluate_persistence({"status": "success"})
    assert decision.code == "missing_identity"


@pytest.mark.parametrize("falsy", [0, False, ""])
def test_a_falsy_identity_is_not_an_identity(falsy):
    """The gate says "non-empty", not "present".

    A numeric user_id of 0 - or a boolean - is not a usable account handle, so it must
    be refused rather than stringified into "0" and waved through.
    """
    payload = {"user_id": falsy, "token": LONG_TOKEN, "status": "success"}
    assert evaluate_persistence(payload).code == "missing_identity"


def test_a_token_alone_is_not_an_identity():
    """A credential without an account tells us nothing about who it belongs to."""
    assert evaluate_persistence({"token": LONG_TOKEN}).code == "missing_identity"


# -- the status gate ----------------------------------------------------------


@pytest.mark.parametrize(
    "status", ["success", "registered", "active", "ok", "valid", "created", "SUCCESS", " Ok "]
)
def test_every_documented_success_status_passes(status):
    assert evaluate_persistence(result(status=status)).ok is True


@pytest.mark.parametrize("status", ["failed", "error", "banned", "pending", "unknown"])
def test_a_non_success_status_is_refused(status):
    decision = evaluate_persistence(result(status=status))
    assert (decision.ok, decision.code) == (False, "status_not_success")


def test_an_absent_status_is_treated_as_success():
    """Deliberate: the access token is the stronger evidence of a credential exchange."""
    payload = {"email": "user@example.com", "token": LONG_TOKEN}
    assert evaluate_persistence(payload).ok is True


def test_a_blank_status_is_treated_as_success():
    assert evaluate_persistence(result(status="   ")).ok is True


def test_an_enum_style_status_is_unwrapped():
    assert evaluate_persistence(result(status=_Status("success"))).ok is True
    assert evaluate_persistence(result(status=_Status("failed"))).code == "status_not_success"


def test_an_object_status_is_read_through_its_value():
    """The platform layer hands back a result object, not a dict."""
    payload = _Object(email="user@example.com", token=LONG_TOKEN, status=_Status("registered"))
    assert evaluate_persistence(payload).ok is True


def test_the_status_gate_is_checked_before_the_token_gate():
    decision = evaluate_persistence({"email": "user@example.com", "status": "failed"})
    assert decision.code == "status_not_success"


# -- the token gate -----------------------------------------------------------


def test_a_missing_token_is_refused():
    decision = evaluate_persistence({"email": "user@example.com", "status": "success"})
    assert (decision.ok, decision.code) == (False, "missing_access_token")


def test_a_token_one_character_short_is_refused():
    """The boundary is a length, so the exact threshold matters."""
    decision = evaluate_persistence(result(token=SHORT_TOKEN))
    assert (decision.ok, decision.code) == (False, "missing_access_token")


def test_a_token_at_the_threshold_is_accepted():
    assert evaluate_persistence(result(token=LONG_TOKEN)).ok is True


def test_the_minimum_length_describes_a_real_bearer_token():
    assert MIN_ACCESS_TOKEN_LENGTH == 20


@pytest.mark.parametrize(
    "token_key", ["token", "access_token", "accessToken", "at", "bearer_token"]
)
def test_every_documented_token_key_is_read(token_key):
    payload = {"email": "user@example.com", "status": "success", token_key: LONG_TOKEN}
    assert evaluate_persistence(payload).ok is True


def test_the_first_usable_token_key_wins():
    """A short ``token`` must not be rescued by a long alias, or the order would be arbitrary."""
    payload = {
        "email": "user@example.com",
        "status": "success",
        "token": SHORT_TOKEN,
        "access_token": LONG_TOKEN,
    }
    assert evaluate_persistence(payload).code == "missing_access_token"


def test_a_blank_token_is_not_a_token():
    assert evaluate_persistence(result(token="   ")).code == "missing_access_token"


def test_a_token_is_found_through_the_extra_mapping():
    """Some executors nest vendor fields under ``extra``; the boundary must still see them."""
    payload = {"email": "user@example.com", "status": "success", "extra": {"access_token": LONG_TOKEN}}
    assert evaluate_persistence(payload).ok is True


def test_a_toplevel_token_beats_one_nested_in_extra():
    payload = {
        "email": "user@example.com",
        "status": "success",
        "token": SHORT_TOKEN,
        "extra": {"token": LONG_TOKEN},
    }
    assert evaluate_persistence(payload).code == "missing_access_token"


def test_extra_is_read_from_an_object_as_well():
    payload = _Object(
        email="user@example.com", status="success", extra={"access_token": LONG_TOKEN}
    )
    assert evaluate_persistence(payload).ok is True


def test_an_identity_is_found_through_extra_too():
    payload = {"status": "success", "token": LONG_TOKEN, "extra": {"email": "user@example.com"}}
    assert evaluate_persistence(payload).ok is True


def test_a_non_mapping_extra_is_ignored():
    """A malformed extra must not crash the boundary - it should simply not help."""
    payload = {"email": "user@example.com", "status": "success", "extra": "not-a-mapping"}
    assert evaluate_persistence(payload).code == "missing_access_token"


# -- the secret gate ----------------------------------------------------------


def test_a_secret_is_not_required_by_default():
    """The token already proves a credential exchange, so this is opt-in."""
    assert evaluate_persistence(result()).ok is True


def test_require_secret_refuses_a_passwordless_result():
    decision = evaluate_persistence(result(), require_secret=True)
    assert (decision.ok, decision.code) == (False, "missing_secret")


@pytest.mark.parametrize(
    "secret_key", ["password", "secret", "refresh_token", "refreshToken"]
)
def test_require_secret_accepts_any_documented_secret_key(secret_key):
    assert evaluate_persistence(result(**{secret_key: "hunter2"}), require_secret=True).ok is True


def test_the_secret_gate_runs_after_the_token_gate():
    """A missing token is the more fundamental failure and must be reported first."""
    payload = {"email": "user@example.com", "status": "success"}
    assert evaluate_persistence(payload, require_secret=True).code == "missing_access_token"


# -- the decision object ------------------------------------------------------


def test_a_decision_is_frozen():
    """A decision is a record of what happened, so it must not be edited afterwards."""
    decision = evaluate_persistence(result())
    with pytest.raises(Exception):
        decision.ok = False  # type: ignore[misc]


def test_the_log_fields_report_the_boundary_and_the_verdict():
    ok_fields = evaluate_persistence(result()).log_fields()
    assert ok_fields == {"persistence_boundary": BOUNDARY_VERSION, "persistence_code": "ok"}
    refused = evaluate_persistence(None).log_fields()
    assert refused["persistence_code"] == "empty_result"


def test_a_refusal_carries_an_operator_readable_detail():
    for payload in (None, {"status": "success"}, {"email": "x", "status": "failed"}):
        decision = evaluate_persistence(payload)
        assert decision.ok is False
        assert decision.detail.strip()


# -- the summary --------------------------------------------------------------


def test_the_summary_describes_the_live_boundary():
    summary = boundary_summary()
    assert summary["boundary"] == BOUNDARY_VERSION
    assert summary["min_access_token_length"] == MIN_ACCESS_TOKEN_LENGTH
    for group in ("identity_keys", "token_keys", "secret_keys"):
        assert isinstance(summary[group], list) and summary[group]


def test_every_advertised_key_actually_works():
    """The summary is documentation; a stale entry would mislead whoever reads it."""
    summary = boundary_summary()
    for key in summary["identity_keys"]:
        assert evaluate_persistence({"token": LONG_TOKEN, "status": "success", key: "id"}).ok is True
    for key in summary["token_keys"]:
        assert evaluate_persistence({"email": "user@example.com", "status": "success", key: LONG_TOKEN}).ok is True
    for key in summary["secret_keys"]:
        assert evaluate_persistence(
            {"email": "user@example.com", "status": "success", "token": LONG_TOKEN, key: "s"},
            require_secret=True,
        ).ok is True
