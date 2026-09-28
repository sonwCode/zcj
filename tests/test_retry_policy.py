"""Retry decisions, and the invariant that keeps two tables from drifting.

The design doc's P1-4: attribution computed a ``retryable`` flag that nothing consumed,
so a blocked proxy and a dead mailbox were retried the same way - burning a fresh
mailbox when the exit IP was the problem. ``retry_policy`` is the consumer: it turns
each code into a concrete action (rotate proxy? rotate mailbox? resume from which
stage?).

Because the fact "is this worth retrying" now lives in two modules, the most
important test here is the cross-check that they agree. They did not: ``phone_risk``
and ``credential_incomplete`` were reported as not retryable to the dashboard while
the engine retried both.
"""
from __future__ import annotations

import pytest

from core.registration import attribution as attr
from core.registration import retry_policy as policy


ALL_CODES = sorted(set(attr._LABELS) | set(policy._POLICIES))


# -- the drift guard ---------------------------------------------------------

@pytest.mark.parametrize("code", ALL_CODES)
def test_attribution_and_retry_policy_agree_on_retryability(code):
    """Two tables encode one fact; the dashboard reads one and the engine the other.

    Compared table-to-table, not through classify_failure(): a category name is not
    necessarily one of its own trigger keywords, so feeding a code in as a message
    would exercise the classifier instead of this invariant.
    """
    from_attribution = code in attr._RETRYABLE
    from_policy = policy._POLICIES.get(code, (False,))[0]
    assert from_attribution == from_policy, (
        f"{code}: attribution says retryable={from_attribution} but retry_policy says "
        f"{from_policy} - the failure breakdown will contradict what the engine does"
    )


def test_every_attribution_category_has_a_retry_policy_row():
    """A category with no row silently inherits the unknown policy (never retry)."""
    missing = sorted(set(attr._LABELS) - set(policy._POLICIES))
    assert missing == [], f"no retry policy defined for {missing}"


def test_the_two_label_tables_do_not_contradict_each_other():
    for code in ALL_CODES:
        assert policy.decide(code, attempt=1, max_attempts=99).label == attr._LABELS.get(code, code)


# -- the money invariant: rotate the right resource ---------------------------

@pytest.mark.parametrize(
    "code", ["proxy_blocked", "rate_limited", "captcha_fail", "credential_incomplete"]
)
def test_proxy_problems_rotate_the_proxy_and_keep_the_mailbox(code):
    action = policy.decide(code, attempt=1, max_attempts=99)
    assert action.retry is True
    assert action.rotate_proxy is True
    assert action.rotate_mailbox is False, "a blocked exit is not fixed by a new mailbox"


@pytest.mark.parametrize("code", ["otp_timeout", "mailbox_error"])
def test_mailbox_problems_rotate_the_mailbox_and_keep_the_proxy(code):
    action = policy.decide(code, attempt=1, max_attempts=99)
    assert action.retry is True
    assert action.rotate_mailbox is True
    assert action.rotate_proxy is False, "a dead mailbox is not fixed by a new exit IP"


@pytest.mark.parametrize("code", ["network_error", "upstream_error"])
def test_transient_infrastructure_failures_rotate_nothing(code):
    """These usually clear on their own; rotating would spend money for nothing."""
    action = policy.decide(code, attempt=1, max_attempts=99)
    assert action.retry is True
    assert action.rotate_proxy is False and action.rotate_mailbox is False
    assert action.backoff_seconds > 0, "but they must wait"


# -- backoff -----------------------------------------------------------------

def test_backoff_doubles_with_each_attempt():
    first = policy.decide("upstream_error", attempt=1, max_attempts=99).backoff_seconds
    second = policy.decide("upstream_error", attempt=2, max_attempts=99).backoff_seconds
    third = policy.decide("upstream_error", attempt=3, max_attempts=99).backoff_seconds
    assert second == first * 2 and third == second * 2


def test_backoff_is_capped():
    huge = policy.decide("upstream_error", attempt=40, max_attempts=99).backoff_seconds
    assert huge == policy.MAX_BACKOFF_SECONDS


def test_a_zero_base_backoff_does_not_grow():
    """proxy_blocked rotates immediately - waiting would not unblock an IP."""
    for attempt in (1, 2, 5):
        assert policy.decide("proxy_blocked", attempt=attempt, max_attempts=99).backoff_seconds == 0.0


# -- attempt budget ----------------------------------------------------------

def test_no_retry_once_the_budget_is_spent():
    action = policy.decide("network_error", attempt=3, max_attempts=3)
    assert action.retry is False
    assert action.backoff_seconds == 0.0
    assert "上限" in action.reason


def test_a_zero_max_attempts_means_unlimited():
    """retry_policy_table() relies on this to render the action for each code."""
    assert policy.decide("network_error", attempt=99, max_attempts=0).retry is True


def test_an_empty_code_becomes_unknown():
    for empty in ("", None, "   "):
        action = policy.decide(empty, attempt=1, max_attempts=99)
        assert action.code == attr.CATEGORY_UNKNOWN
        assert action.retry is False


def test_an_unrecognised_code_keeps_its_name_and_inherits_the_unknown_policy():
    """Renaming it would hide the real code from the operator, so it is kept - unretried."""
    action = policy.decide("not_a_real_code", attempt=1, max_attempts=99)
    assert action.code == "not_a_real_code"
    assert action.retry is False
    assert action.reason == policy._POLICIES[attr.CATEGORY_UNKNOWN][4]


def test_an_attempt_below_one_is_treated_as_the_first():
    assert (policy.decide("upstream_error", attempt=0, max_attempts=99).backoff_seconds
            == policy.decide("upstream_error", attempt=1, max_attempts=99).backoff_seconds)


# -- resume stages must be real pipeline stages ------------------------------

# The stage names the pipeline actually reports (see core/registration/models.py and
# the registration_pipeline.stages payload). A resume_stage that names a stage the
# pipeline never emits would park an account forever.
KNOWN_STAGES = {
    "account_created",
    "phone_verified",
    "credentials_ready",
    "persisted",
    "liveness",
    "payment",
    "probation",
}


def test_every_resume_stage_is_a_real_pipeline_stage():
    for code, stage in policy._RESUME_STAGE.items():
        assert stage in KNOWN_STAGES, f"{code} would resume at unknown stage {stage!r}"


def test_resume_stages_are_only_set_for_codes_that_retry():
    for code, stage in policy._RESUME_STAGE.items():
        assert policy._POLICIES.get(code, (False,))[0] is True, (
            f"{code} declares a resume stage ({stage}) but never retries"
        )


def test_a_fresh_start_has_no_resume_stage():
    """Resuming from a stage that was never reached is worse than starting over."""
    action = policy.decide("proxy_blocked", attempt=1, max_attempts=99)
    assert action.resume_stage == ""


# -- policy table ------------------------------------------------------------


def test_the_policy_table_renders_every_code_exactly_once():
    table = policy.retry_policy_table()
    codes = [row["code"] for row in table]
    assert sorted(codes) == sorted(policy._POLICIES)
    assert len(codes) == len(set(codes))


def test_the_policy_table_never_asks_for_a_retry_loop():
    """It renders with max_attempts=0, so nothing in it may be exhausted already."""
    for row in policy.retry_policy_table():
        expected = policy._POLICIES[row["code"]][0]
        assert row["retry"] is expected


def test_to_dict_rounds_backoff_for_display():
    row = policy.decide("upstream_error", attempt=2, max_attempts=99).to_dict()
    assert row["backoff_seconds"] == 60.0
    assert set(row) == {
        "retry", "code", "label", "backoff_seconds", "rotate_proxy",
        "rotate_mailbox", "resume_stage", "reason", "stage",
    }


# -- classification ----------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (407, "proxy_blocked"),
    (403, "proxy_blocked"),
    (429, "rate_limited"),
    (500, "upstream_error"),
    (503, "upstream_error"),
])
def test_status_codes_are_classified_before_the_message(status, expected):
    """A status is unambiguous; keyword-matching a message is not."""
    assert attr.classify_failure("something ambiguous", status_code=status).code == expected


@pytest.mark.parametrize("message,expected", [
    ("429 Too Many Requests", "rate_limited"),
    ("too many requests", "rate_limited"),
    ("captcha challenge failed", "captcha_fail"),
    ("proxy authentication required", "proxy_blocked"),
    ("connect timeout after 30s", "network_error"),
    ("HTTP 502 bad gateway", "upstream_error"),
])
def test_representative_messages_classify_as_expected(message, expected):
    assert attr.classify_failure(message).code == expected


def test_an_unrecognised_message_is_unknown_and_not_retried():
    result = attr.classify_failure("something we have never seen before")
    assert result.code == "unknown"
    assert result.retryable is False
    assert policy.decide(result.code, attempt=1, max_attempts=99).retry is False


def test_decide_for_failure_wires_classification_into_the_policy():
    action = policy.decide_for_failure("429 Too Many Requests", stage="liveness")
    assert action.code == "rate_limited"
    assert action.stage == "liveness"
    assert action.rotate_proxy is True


def test_summarize_attributions_buckets_by_code_and_keeps_samples():
    rows = [
        {"error": "429 Too Many Requests", "count": 5},
        {"error": "too many requests again", "count": 2},
        {"error": "captcha failed", "count": 9},
    ]
    buckets = attr.summarize_attributions(rows)
    assert [b["code"] for b in buckets] == ["captcha_fail", "rate_limited"]
    top = next(b for b in buckets if b["code"] == "rate_limited")
    assert top["count"] == 7
    assert len(top["samples"]) == 2


def test_summarize_attributions_returns_most_frequent_first_and_tolerates_junk():
    assert attr.summarize_attributions([]) == []
    assert attr.summarize_attributions(None) == []
    single = attr.summarize_attributions([{"error": "", "count": 3}])
    assert single[0]["code"] == "unknown" and single[0]["count"] == 3
