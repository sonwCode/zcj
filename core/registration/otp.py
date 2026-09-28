r"""Robust one-time-code extraction from verification mail.

The original implementation was a bare ``re.search(r"(?<!#)(?<!\d)(\d{6})(?!\d)")``
that returned the *first* six-digit run it found. Verification mail routinely
contains other six-digit numbers — dates, order ids, amounts, tracking numbers —
so that reliably returned the wrong value, and the failure looked like an expired
code rather than an extraction bug.

This module ports the approach used by turb-gpt-free-register: strip HTML, keep
every candidate, then score them by proximity to a verification keyword and by
whether the sender looks like a verification sender. It is language aware because
the mailbox provider, not ZCJ, decides the mail language.

When a caller passes an explicit ``code_pattern`` the legacy behaviour is preserved
exactly, so platform-specific overrides keep working.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass

DEFAULT_LENGTH = 6

SENDER_HINTS = (
    "openai",
    "noreply",
    "no-reply",
    "no_reply",
    "verify",
    "verification",
    "one-time",
    "otp",
    "security",
    "auth0",
    "okta",
    "验证码",
    "校验码",
    "認証",
    "確認コード",
    "인증",
    "bestätigung",
    "verifizierung",
    "verificación",
    "código",
    "codice",
    "verifica",
    "vérification",
    "doğrulama",
    "подтвержд",
    "xác minh",
    "verifikasi",
)

CONTEXT_HINTS = (
    "verification code",
    "one-time code",
    "one time code",
    "security code",
    "login code",
    "confirmation code",
    "your code",
    "code:",
    "otp",
    "验证码",
    "校验码",
    "验证代码",
    "確認コード",
    "認証コード",
    "コード",
    "인증 코드",
    "인증번호",
    "bestätigungscode",
    "verifizierungscode",
    "código de verificación",
    "código",
    "code de vérification",
    "codice di verifica",
    "код подтверждения",
    "doğrulama kodu",
    "mã xác minh",
    "kode verifikasi",
)

_HTML_TAGS = re.compile(r"(?s)<[^>]+>")
_SCRIPT_BLOCK = re.compile(r"(?is)<script[^>]*>.*?</script>")
_STYLE_BLOCK = re.compile(r"(?is)<style[^>]*>.*?</style>")
_WHITESPACE = re.compile(r"\s+")
_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_DIRECT_PREFIX = re.compile(r"(?:is|are|为|是|は|는|:)\s*$")


@dataclass(frozen=True)
class OtpCandidate:
    value: str
    index: int
    score: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "index": self.index,
            "score": round(self.score, 2),
            "reason": self.reason,
        }


def strip_html(text: str) -> str:
    """Reduce an HTML mail body to searchable text."""
    raw = str(text or "")
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        raw = _SCRIPT_BLOCK.sub(" ", raw)
        raw = _STYLE_BLOCK.sub(" ", raw)
        raw = _HTML_TAGS.sub(" ", raw)
        raw = _html.unescape(raw)
    return _WHITESPACE.sub(" ", raw).strip()


def _candidate_pattern(length: int) -> re.Pattern:
    return re.compile(r"(?<!#)(?<!\d)(\d{%d})(?!\d)" % max(int(length or DEFAULT_LENGTH), 1))


def _nearest_hint_distance(haystack: str, start: int, end: int) -> tuple[int, str, int]:
    best_distance = -1
    best_hint = ""
    best_start = -1
    for hint in CONTEXT_HINTS:
        probe = haystack.find(hint, max(0, start - 160), end + 160)
        if probe < 0:
            continue
        distance = start - probe if probe <= start else probe - end
        distance = abs(distance)
        if best_distance < 0 or distance < best_distance:
            best_distance = distance
            best_hint = hint
            best_start = probe
    return best_distance, best_hint, best_start


def rank_candidates(
    text: str,
    *,
    subject: str = "",
    sender: str = "",
    length: int = DEFAULT_LENGTH,
) -> list[OtpCandidate]:
    """Score every plausible code, best first."""
    # Addresses are scrubbed before scanning. A mailbox URL or a reply-to often carries
    # six consecutive digits (user123456@example.com), which the candidate pattern
    # happily accepts - so a mail with no real code at all yielded a confident,
    # completely invented one. Scrub here rather than in extract_otp so the diagnostics
    # from explain_otp describe the same text the decision was made on.
    body = scrub_emails(strip_html(text))
    subject_text = scrub_emails(strip_html(subject))
    sender_text = str(sender or "").lower()
    haystack = f"{subject_text} {body}".lower()

    trusted_sender = any(hint in sender_text for hint in SENDER_HINTS)
    has_verification_phrase = any(hint in haystack for hint in CONTEXT_HINTS)

    candidates: list[OtpCandidate] = []

    # The subject is scanned as well as the body. Some providers put the code only
    # in the subject ("Your OpenAI code is 525210") and leave the body with nothing
    # but prose - and when the body does carry a six-digit number it is usually an
    # order id or a timestamp, so returning that instead was worse than returning
    # nothing: the caller burned the attempt on a code that could never work.
    subject_matches = list(_candidate_pattern(length).finditer(subject_text))

    body_values: set[str] = set()
    for match in _candidate_pattern(length).finditer(body):
        value = match.group(1)
        body_values.add(value)
        score = 0.0
        reasons: list[str] = []

        if subject_text and value in subject_text:
            score += 40
            reasons.append("subject")

        distance, hint, hint_start = _nearest_hint_distance(haystack, match.start(), match.end())
        if distance >= 0:
            score += max(0.0, 30.0 - distance / 10.0)
            if 0 <= hint_start < match.start():
                # "your verification code is 123456": the real code follows the phrase.
                score += 6
                reasons.append("after-phrase")
            reasons.append(f"near:{hint}")

        prefix = haystack[max(0, match.start() - 14):match.start()]
        if _DIRECT_PREFIX.search(prefix):
            score += 8
            reasons.append("direct")

        if trusted_sender:
            score += 20
            reasons.append("sender")

        if has_verification_phrase:
            score += 10
            reasons.append("phrase")

        if _YEAR.match(value):
            score -= 60
            reasons.append("looks-like-year")

        # A code is normally the only six-digit run; penalise later duplicates so
        # the earliest plausible candidate wins ties.
        score -= match.start() / 100000.0

        candidates.append(OtpCandidate(value, match.start(), score, ",".join(reasons) or "bare"))

    # A code found only in the subject still competes, on a par with a body code
    # that the subject corroborates. Giving it the same +40 keeps the two paths
    # consistent: what matters is that the subject vouches for the value, not
    # which field it was first seen in.
    for match in subject_matches:
        value = match.group(1)
        # A value already found in the body was scored there, with the subject credit
        # applied. Re-adding it would list the same code twice in explain_otp output.
        if value in body_values:
            continue
        score = 0.0
        reasons: list[str] = ["subject"]
        score += 40

        distance, hint, _hint_start = _nearest_hint_distance(haystack, match.start(), match.end())
        if distance >= 0:
            score += max(0.0, 30.0 - distance / 10.0)
            reasons.append(f"near:{hint}")

        if trusted_sender:
            score += 20
            reasons.append("sender")

        if has_verification_phrase:
            score += 10
            reasons.append("phrase")

        if _YEAR.match(value):
            score -= 60
            reasons.append("looks-like-year")

        # Subject codes sort ahead of a body-only tie: the subject is the field a
        # verification sender writes deliberately, so it outranks incidental digits.
        score += 1.0

        candidates.append(
            OtpCandidate(value, -1, score, ",".join(reasons) or "subject")
        )

    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


def extract_otp(
    text: str,
    *,
    subject: str = "",
    sender: str = "",
    code_pattern=None,
    length: int = DEFAULT_LENGTH,
) -> str:
    """Return the most likely verification code, or an empty string."""
    body = str(text or "")
    if not body:
        return ""

    if code_pattern is not None:
        pattern = code_pattern if hasattr(code_pattern, "search") else re.compile(code_pattern)
        match = pattern.search(body)
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    ranked = rank_candidates(body, subject=subject, sender=sender, length=length)
    return ranked[0].value if ranked else ""


def explain_otp(
    text: str,
    *,
    subject: str = "",
    sender: str = "",
    length: int = DEFAULT_LENGTH,
    limit: int = 5,
) -> dict:
    """Diagnostics for why a particular code was chosen."""
    ranked = rank_candidates(text, subject=subject, sender=sender, length=length)
    return {
        "chosen": ranked[0].value if ranked else "",
        "candidates": [item.to_dict() for item in ranked[: max(int(limit or 5), 1)]],
        "trusted_sender": any(hint in str(sender or "").lower() for hint in SENDER_HINTS),
        "length": length,
    }


def scrub_emails(text: str) -> str:
    """Remove addresses so a mailbox URL cannot match its own domain as a code."""
    return _EMAIL.sub(" ", str(text or ""))