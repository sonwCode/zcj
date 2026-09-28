r"""One-time-code extraction from verification mail.

The P1-1 fix, ported from turb-gpt-free-register. The original was a bare
``re.search(r"(?<!#)(?<!\d)(\d{6})(?!\d)")`` that returned the first six-digit run it
found; verification mail is full of other six-digit numbers (dates, order ids, amounts,
tracking ids), so it reliably returned the wrong value - and because a wrong code and
an expired code look identical downstream, the bug hid as "the code expired".

Two failure modes matter and they are not symmetric. A false negative costs one more
mailbox poll. A false positive submits a bogus code to the verification endpoint, burns
the attempt, and is indistinguishable from a real expiry - so the tests below care much
more about never inventing a code than about always finding one.
"""
from __future__ import annotations

import re

import pytest

from core.registration import otp


CODE = "424242"


def _candidate_values(text, **kwargs):
    return [c.value for c in otp.rank_candidates(text, **kwargs)]


# -- never invent a code ------------------------------------------------------


def test_an_address_with_six_digits_is_not_a_code():
    """The regression that shipped: the code was read out of the email address."""
    mail = "Your account user777888@example.com was created."
    assert otp.extract_otp(mail) == ""
    assert "777888" not in _candidate_values(mail)


def test_an_address_does_not_outrank_a_real_code():
    mail = f"Code {CODE} sent to user777888@example.com"
    assert otp.extract_otp(mail, sender="noreply@openai.com") == CODE


def test_a_tracking_url_does_not_outrank_a_real_code():
    mail = f'<a href="https://x.test/track/887766">Verify</a><p>Your code is {CODE}</p>'
    assert otp.extract_otp(mail, sender="noreply@openai.com") == CODE


def test_a_year_is_never_returned_as_a_code():
    mail = "Copyright 2024 Example Inc. No other numbers here at all."
    assert otp.extract_otp(mail) == ""


def test_the_year_penalty_is_unreachable_at_the_default_length():
    """It can only fire on a 4-digit candidate, and the default length is 6.

    Worth pinning explicitly: no caller in the tree passes length=, so the
    "looks-like-year" branch is defensive code for a length nothing currently uses.
    Deleting it would silently change behaviour for the first caller that does.
    """
    assert otp._YEAR.match("2024") is not None
    assert otp._YEAR.match("202401") is None
    # A date-styled 6-digit run is a normal candidate; only a bare year is penalised.
    assert otp._candidate_pattern(otp.DEFAULT_LENGTH).fullmatch("202401") is not None
    assert "looks-like-year" not in otp.rank_candidates("verification code 202401")[0].reason


def test_the_year_penalty_demotes_a_bare_year_when_four_digit_codes_are_asked_for():
    """Drives the branch directly, since nothing else reaches it."""
    ranked = {c.value: c for c in otp.rank_candidates("verification code 2024 and code 7788", length=4)}
    assert "looks-like-year" in ranked["2024"].reason
    assert ranked["2024"].score < ranked["7788"].score
    assert otp.extract_otp("verification code 2024 and code 7788", length=4) == "7788"


def test_scrubbing_removes_the_address_but_keeps_the_rest():
    scrubbed = otp.scrub_emails(f"write to a.b+tag@example.co.uk about {CODE}")
    assert "example.co.uk" not in scrubbed
    assert CODE in scrubbed


def test_scrub_emails_survives_junk():
    assert otp.scrub_emails("") == ""
    assert otp.scrub_emails(None) == ""
    assert otp.scrub_emails("no address here") == "no address here"


# -- find the right code ------------------------------------------------------


def test_the_code_next_to_the_phrase_wins():
    mail = f"Your order 111222 shipped. Your verification code is {CODE}."
    assert otp.extract_otp(mail, sender="noreply@openai.com") == CODE


def test_a_code_that_lives_only_in_the_subject_is_still_found():
    """Candidates are scanned out of the subject as well as the body.

    The subject is where a verification sender writes the code deliberately, so it
    cannot be treated as a mere tie-breaker: providers that send a digit-free body
    ("Thanks for signing up") would otherwise never yield the code at all.
    """
    mail = "Thanks for signing up. Use the code we sent to your inbox to continue."
    assert otp.extract_otp(mail, subject=f"Your code is {CODE}") == CODE
    assert CODE in _candidate_values(mail, subject=f"Your code is {CODE}")


def test_a_subject_code_outranks_an_unrelated_number_in_the_body():
    """The body number is usually an order id; returning it burns the attempt.

    Before the subject was scanned this returned the body's six-digit run, which is
    worse than returning nothing: a wrong code and an expired code are identical
    downstream, so the caller retried a code that could never work.
    """
    mail = "Order 111222 confirmed. Nothing else to see."
    assert otp.extract_otp(mail, subject=f"Your code is {CODE}") == CODE


@pytest.mark.parametrize("subject", [
    "Your code is {code}",
    "Your verification code: {code}",
    "認証コード {code}",
    "인증 코드 {code}",
    "验证码 {code}",
    "Ihr Bestätigungscode lautet {code}",
])
def test_a_subject_only_code_is_found_in_every_documented_language(subject):
    """The mailbox provider, not ZCJ, decides the mail language."""
    assert otp.extract_otp("Thanks for signing up.", subject=subject.format(code=CODE)) == CODE


def test_a_code_present_in_both_subject_and_body_is_listed_only_once():
    """The subject credit is applied to the body candidate instead of duplicating it."""
    mail = f"Your code is {CODE}"
    values = _candidate_values(mail, subject=f"Your code is {CODE}")
    assert values == [CODE]


def test_a_year_in_the_subject_is_still_not_a_code():
    assert otp.extract_otp("Thanks for signing up.", subject="Your code is 2024") == ""


def test_an_empty_subject_changes_nothing():
    mail = f"Your verification code is {CODE}"
    assert otp.extract_otp(mail, subject="") == CODE


def test_a_subject_without_any_digits_is_not_a_code():
    assert otp.extract_otp("Thanks for signing up.", subject="Welcome to OpenAI") == ""


@pytest.mark.parametrize("phrase", [
    "verification code", "one-time code", "security code", "your code",
    "confirmation code", "验证码", "認証コード", "인증번호",
    "bestätigungscode", "código de verificación", "код подтверждения",
])
def test_every_documented_phrase_helps(phrase):
    mail = f"{phrase}: {CODE}"
    assert otp.extract_otp(mail) == CODE


@pytest.mark.parametrize("sender", [
    "noreply@openai.com", "no-reply@x.test", "verify@x.test",
    "security@x.test", "otp@x.test", "noreply@auth0.com",
])
def test_a_trusted_sender_helps(sender):
    mail = f"Here is the number you asked for: {CODE}"
    assert otp.extract_otp(mail, sender=sender) == CODE


def test_the_code_closest_to_a_phrase_wins_over_the_earlier_one():
    """Proximity beats position: 222222 sits right after "confirmation code"."""
    mail = "verification code 111111 confirmation code 222222"
    assert otp.extract_otp(mail) == "222222"


def test_identical_scores_break_towards_the_earlier_candidate():
    """Only when nothing else separates them does position decide."""
    mail = "code 111111 and code 222222"
    values = _candidate_values(mail)
    assert values[0] == "111111"


def test_a_six_digit_run_inside_a_longer_number_is_not_a_code():
    mail = "verification code reference 1234567890 here"
    assert otp.extract_otp(mail) == ""


def test_a_hash_prefixed_number_is_not_a_code():
    mail = "verification code #123456 posted"
    assert otp.extract_otp(mail) == ""


def test_no_candidates_at_all_returns_empty():
    assert otp.extract_otp("nothing numeric here") == ""
    assert otp.extract_otp("") == ""
    assert otp.extract_otp(None) == ""


# -- lengths ------------------------------------------------------------------


def test_a_custom_length_is_honoured():
    assert otp.extract_otp("verification code 12345", length=5) == "12345"
    assert otp.extract_otp("verification code 12345", length=6) == ""


def test_eight_digit_codes_work():
    assert otp.extract_otp("verification code 12345678", length=8) == "12345678"


def test_a_zero_length_falls_back_to_six():
    assert otp.extract_otp("verification code 424242", length=0) == "424242"


# -- legacy explicit pattern --------------------------------------------------


def test_an_explicit_pattern_preserves_the_legacy_behaviour_exactly():
    """Platform overrides still hand in their own regex; they must not be re-scored."""
    mail = f"ignore 111222 and take {CODE}"
    assert otp.extract_otp(mail, code_pattern=re.compile(r"take (\d{6})")) == CODE


def test_an_explicit_pattern_accepts_a_string_too():
    assert otp.extract_otp(f"v={CODE}", code_pattern=r"v=(\d{6})") == CODE


def test_an_explicit_pattern_without_a_group_returns_the_whole_match():
    assert otp.extract_otp("abc42def", code_pattern=r"42") == "42"


def test_an_explicit_pattern_that_does_not_match_returns_empty():
    assert otp.extract_otp("no digits", code_pattern=r"(\d{6})") == ""


def test_the_explicit_pattern_sees_the_raw_html_not_the_stripped_text():
    """Overrides are written against the provider payload, so it is left untouched."""
    assert otp.extract_otp(f"<b>{CODE}</b>", code_pattern=r"<b>(\d{6})</b>") == CODE


# -- html stripping -----------------------------------------------------------


def test_strip_html_removes_tags_and_scripts():
    stripped = otp.strip_html("<div>code <b>424242</b></div><script>var x=1</script>")
    assert "424242" in stripped
    assert "<" not in stripped and "var x" not in stripped


def test_strip_html_decodes_entities_when_the_mail_is_html():
    assert otp.strip_html("<p>a&amp;b</p>") == "a&b"


def test_plain_text_is_not_entity_decoded():
    """Unescaping only runs in the HTML branch; plain text is passed through."""
    assert otp.strip_html("a&amp;b") == "a&amp;b"


def test_strip_html_collapses_whitespace():
    assert otp.strip_html("a\n\n   b") == "a b"


def test_strip_html_leaves_plain_text_alone():
    assert otp.strip_html("nothing to strip") == "nothing to strip"


# -- diagnostics --------------------------------------------------------------


def test_explain_reports_the_same_choice_as_extract():
    """Diagnostics that disagree with the decision are worse than none."""
    mail = f"Your order 111222 shipped. Your verification code is {CODE}."
    report = otp.explain_otp(mail, sender="noreply@openai.com")
    assert report["chosen"] == otp.extract_otp(mail, sender="noreply@openai.com")
    assert report["trusted_sender"] is True
    assert report["candidates"][0]["value"] == report["chosen"]


def test_explain_carries_the_reason_the_code_won():
    report = otp.explain_otp(f"verification code {CODE}", sender="noreply@openai.com")
    top = report["candidates"][0]
    assert "near:" in top["reason"] and "sender" in top["reason"]


def test_explain_respects_its_limit():
    mail = "verification code " + " ".join(f"{n:06d}" for n in range(1, 9))
    assert len(otp.explain_otp(mail, limit=2)["candidates"]) == 2
    # limit=0 is falsy, so it means "unset" and falls back to the default of 5.
    assert len(otp.explain_otp(mail, limit=0)["candidates"]) == 5


def test_explain_on_an_empty_mail_reports_nothing():
    report = otp.explain_otp("")
    assert report["chosen"] == "" and report["candidates"] == []


def test_candidates_serialise_for_the_api():
    top = otp.rank_candidates(f"verification code {CODE}")[0].to_dict()
    assert set(top) == {"value", "index", "score", "reason"}
    assert isinstance(top["score"], float)


def test_a_bare_candidate_says_so():
    candidates = otp.rank_candidates("111222")
    assert candidates[0].reason == "bare"
