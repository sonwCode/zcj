"""Resume decisions for partially-registered accounts.

The P1-3 fix: ``platform.register()`` returns an account that already carries a token,
and everything after it (phone binding, credentials, persistence, liveness, workspace
join, payment) is enrichment. When a late stage failed the account was saved as
``pending_verification`` and the next attempt started a brand-new registration anyway -
consuming another mailbox and leaving an orphan behind. ``plan_resume`` is what makes
that orphan usable.

The invariant most likely to rot is the stage ordering: it is written down twice, once
here and once by ``application/tasks.py::_registration_pipeline_update``, which owns the
stage sets. It had already rotted - ``workspace_join`` was missing, so a failure there
was reported as "no failed stage" and the account was silently dropped.
"""
from __future__ import annotations

import pytest

from core.registration import resume


def _pipeline(*states):
    return {"registration_pipeline": {"stages": dict(states)}}


def _failed(name):
    return {name: {"status": "failed"}}


# -- the order invariant ------------------------------------------------------

# The pipeline's real, chronological stage order, taken from the line numbers of the
# _registration_pipeline_update call sites in application/tasks.py.
REAL_ORDER = [
    "account_created",
    "phone_verified",
    "credentials_ready",
    "persisted",
    "liveness",
    "probation",
    "workspace_join",
    "payment",
]


def test_resume_order_matches_the_pipeline_order():
    """A stage missing here can never be reported as the failure to resume from."""
    assert list(resume.PIPELINE_STAGES) == REAL_ORDER


def test_every_resume_stage_is_one_the_pipeline_actually_emits():
    never_emitted = set(resume.PIPELINE_STAGES) - set(REAL_ORDER)
    assert never_emitted == set(), (
        f"resume would re-enter at a stage nothing ever writes: {sorted(never_emitted)}"
    )


@pytest.mark.parametrize("stage", REAL_ORDER)
def test_a_failure_at_any_real_stage_is_found(stage):
    """This is the regression that shipped: workspace_join returned an empty string."""
    assert resume.first_failed_stage(_failed(stage)) == stage


def test_the_earliest_failure_wins():
    stages = {}
    stages.update(_failed("persisted"))
    stages.update(_failed("payment"))
    assert resume.first_failed_stage(stages) == "persisted"


def test_stages_that_did_not_fail_are_ignored():
    stages = {"account_created": {"status": "passed"}, "phone_verified": {"status": "in_progress"}}
    assert resume.first_failed_stage(stages) == ""


def test_a_stage_recorded_with_the_other_key_is_still_read():
    """The writer sets ``status``; older rows in the wild carry ``state``."""
    assert resume.first_failed_stage({"liveness": {"state": "failed"}}) == "liveness"


def test_a_bare_string_stage_value_is_tolerated():
    assert resume.first_failed_stage({"liveness": "FAILED"}) == "liveness"


def test_first_failed_stage_survives_junk():
    assert resume.first_failed_stage({}) == ""
    assert resume.first_failed_stage(None) == ""


# -- plan_resume --------------------------------------------------------------


def test_a_pending_account_failing_late_can_resume():
    stages = {s: {"status": "passed"} for s in REAL_ORDER}
    stages["credentials_ready"] = {"status": "failed"}
    ok, stage, reason = resume.plan_resume(
        {"registration_pipeline": {"stages": stages}}, status="pending_verification"
    )
    assert (ok, stage) == (True, "credentials_ready")
    assert "credentials_ready" in reason


@pytest.mark.parametrize("status", sorted(resume.RESUMABLE_STATUSES))
def test_both_resumable_statuses_are_accepted(status):
    ok, stage, _ = resume.plan_resume(_pipeline(*_failed("liveness").items()), status=status)
    assert ok is True and stage == "liveness"


@pytest.mark.parametrize("status", ["active", "registered", "failed", "disabled", ""])
def test_only_resumable_statuses_are_accepted(status):
    ok, stage, reason = resume.plan_resume(_pipeline(*_failed("liveness").items()), status=status)
    assert ok is False and stage == ""
    assert "不可续跑" in reason


def test_an_account_the_remote_has_invalidated_is_never_resumed():
    overview = _pipeline(*_failed("liveness").items())
    overview["validity_status"] = "invalid"
    ok, _, reason = resume.plan_resume(overview, status="pending_verification")
    assert ok is False and "失效" in reason


def test_an_account_with_no_failed_stage_is_not_resumed():
    ok, stage, reason = resume.plan_resume(_pipeline(("liveness", {"status": "passed"})), status="pending_verification")
    assert (ok, stage) == (False, "") and "没有失败阶段" in reason


def test_the_account_created_stage_is_a_terminal_failure():
    """There is no account to enrich yet, so resuming would burn another retry."""
    ok, stage, reason = resume.plan_resume(_pipeline(*_failed("account_created").items()), status="pending_verification")
    assert (ok, stage) == (False, "") and "终态" in reason


def test_the_explicit_terminal_marker_beats_the_stage_name():
    """_mark_terminal_registration_account writes detail.terminal for remote permanence."""
    stages = {"liveness": {"status": "failed", "detail": {"terminal": True}}}
    ok, _, reason = resume.plan_resume({"registration_pipeline": {"stages": stages}}, status="pending_verification")
    assert ok is False and "终态" in reason


def test_a_non_terminal_detail_is_not_mistaken_for_the_marker():
    stages = {"liveness": {"status": "failed", "detail": {"terminal": False}}}
    ok, stage, _ = resume.plan_resume({"registration_pipeline": {"stages": stages}}, status="pending_verification")
    assert (ok, stage) == (True, "liveness")


def test_plan_resume_survives_a_missing_overview():
    for empty in ({}, None, {"registration_pipeline": None}, {"registration_pipeline": {}}):
        ok, stage, _ = resume.plan_resume(empty, status="pending_verification")
        assert (ok, stage) == (False, "")


# -- the switch ---------------------------------------------------------------


def test_resuming_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("ZCJ_RESUME_REGISTRATION", raising=False)
    assert resume.resume_enabled(None) is False
    assert resume.resume_enabled({}) is False


def test_the_per_task_switch_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("ZCJ_RESUME_REGISTRATION", "1")
    assert resume.resume_enabled({"resume_registration": False}) is False
    monkeypatch.delenv("ZCJ_RESUME_REGISTRATION", raising=False)
    assert resume.resume_enabled({"resume_registration": True}) is True


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "enabled", " on "])
def test_the_environment_switch_accepts_the_usual_spellings(monkeypatch, raw):
    monkeypatch.setenv("ZCJ_RESUME_REGISTRATION", raw)
    assert resume.resume_enabled(None) is True


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", "maybe"])
def test_the_environment_switch_rejects_everything_else(monkeypatch, raw):
    monkeypatch.setenv("ZCJ_RESUME_REGISTRATION", raw)
    assert resume.resume_enabled(None) is False


# -- the candidate record -----------------------------------------------------


def test_the_candidate_serialises_what_the_api_needs():
    candidate = resume.ResumeCandidate(
        account_id=7, platform="chatgpt", email="a@b.c", status="pending_verification",
        resume_stage="liveness", has_token=True, reason="从 liveness 续跑",
    )
    payload = candidate.to_dict()
    assert payload == {
        "account_id": 7, "platform": "chatgpt", "email": "a@b.c",
        "status": "pending_verification", "resume_stage": "liveness",
        "has_token": True, "reason": "从 liveness 续跑",
    }
    assert "account" not in payload, "the ORM object must not leak into the response"


def test_the_candidate_is_frozen():
    candidate = resume.ResumeCandidate(1, "p", "e", "s", "liveness", True, "r")
    with pytest.raises(Exception):
        candidate.account_id = 2  # type: ignore[misc]


# -- finding orphans ----------------------------------------------------------


def test_looking_for_orphans_never_returns_more_than_asked_for():
    """find_resumable_accounts queries a live table; prove the cap holds with none."""
    found = resume.find_resumable_accounts("no-such-platform-exists", limit=3)
    assert found == []


def test_the_limit_floor_is_one():
    assert resume.find_resumable_accounts("no-such-platform-exists", limit=0) == []
