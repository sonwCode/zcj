"""The human-in-the-loop OTP broker (P1-2).

When a mailbox never delivers the code, registration used to sit in a 600s dead wait and
then fail - burning the proxy lease and the mailbox while an operator could have typed
the code in seconds. This broker lets the dashboard inject it.

Two properties carry the weight. First, a code must never be handed to the wrong run:
the broker is addressed by request id, then task id, then email, and only ever resolves
to a request that is still ``waiting``. Second, the wait must stay cooperative - it
blocks in short slices so a cancelled task is noticed promptly rather than after the full
window.
"""
from __future__ import annotations

import threading
import time

import pytest

from core.manual_otp import (
    MAX_TRACKED_REQUESTS,
    POLL_SLICE_SECONDS,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_SUBMITTED,
    STATUS_WAITING,
    ManualOtpBroker,
)


@pytest.fixture()
def broker():
    return ManualOtpBroker()


# -- opening a request --------------------------------------------------------


def test_an_opened_request_starts_waiting(broker):
    request = broker.open(task_id="t1", email="a@b.c", platform="chatgpt", keyword="code")
    assert request.status == STATUS_WAITING
    assert request.code == ""
    assert request.request_id
    assert request.task_id == "t1" and request.email == "a@b.c"
    assert request.keyword == "code"


def test_each_request_gets_its_own_id(broker):
    ids = {broker.open(task_id="t").request_id for _ in range(5)}
    assert len(ids) == 5


def test_the_window_defaults_and_is_never_zero(broker):
    default = broker.open()
    assert default.expires_at - default.created_at >= 300
    """A zero window would close the channel before an operator could ever answer."""
    assert broker.open(timeout=0).expires_at > broker.open(timeout=0).created_at


def test_remaining_seconds_never_goes_negative(broker):
    request = broker.open()
    assert request.remaining_seconds > 0
    request.expires_at = time.time() - 10
    assert request.remaining_seconds == 0.0


def test_a_request_serialises_for_the_dashboard(broker):
    request = broker.open(task_id="t", email="e", platform="p", keyword="k", note="n")
    payload = request.to_dict()
    assert set(payload) == {
        "request_id", "task_id", "email", "platform", "keyword", "status",
        "created_at", "expires_at", "remaining_seconds", "submitted_at", "note",
    }
    assert payload["status"] == STATUS_WAITING
    assert isinstance(payload["remaining_seconds"], float)
    assert "code" not in payload, "the code must not ride along on the waiting list"


# -- submitting a code --------------------------------------------------------


def test_a_submitted_code_is_returned_by_id(broker):
    request = broker.open(task_id="t")
    submitted = broker.submit("123456", request_id=request.request_id)
    assert submitted is not None and submitted.request_id == request.request_id
    assert submitted.status == STATUS_SUBMITTED
    assert submitted.code == "123456"
    assert submitted.submitted_at > 0


def test_the_code_is_trimmed(broker):
    request = broker.open(task_id="t")
    assert broker.submit("  123456  ", request_id=request.request_id).code == "123456"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_code_is_refused(broker, blank):
    request = broker.open(task_id="t")
    assert broker.submit(blank, request_id=request.request_id) is None
    assert broker.get(request.request_id).status == STATUS_WAITING


def test_a_code_cannot_be_submitted_twice(broker):
    """The second answer must not overwrite the first, or the operator could hijack it."""
    request = broker.open(task_id="t")
    broker.submit("111111", request_id=request.request_id)
    assert broker.submit("222222", request_id=request.request_id) is None
    assert broker.get(request.request_id).code == "111111"


def test_submitting_to_an_unknown_request_returns_nothing(broker):
    assert broker.submit("123456", request_id="nope") is None
    assert broker.submit("123456") is None


# -- addressing: by id, task, then email --------------------------------------


def test_a_task_id_resolves_to_its_own_request(broker):
    mine = broker.open(task_id="mine")
    broker.open(task_id="other")
    assert broker.submit("123456", task_id="mine").request_id == mine.request_id


def test_an_email_resolves_when_no_task_matches(broker):
    mine = broker.open(email="me@x.test")
    assert broker.submit("123456", email="me@x.test").request_id == mine.request_id


def test_an_explicit_request_id_beats_the_task_and_email(broker):
    first = broker.open(task_id="t", email="e")
    second = broker.open(task_id="t", email="e")
    assert broker.submit("123456", request_id=first.request_id, task_id="t", email="e").request_id == first.request_id
    assert broker.get(second.request_id).status == STATUS_WAITING


def test_the_most_recent_match_wins(broker):
    """A retried run leaves an older request behind; the live one must be the target."""
    old = broker.open(task_id="t")
    old.created_at -= 100
    new = broker.open(task_id="t")
    assert broker.submit("123456", task_id="t").request_id == new.request_id


def test_a_closed_request_is_never_resolved(broker):
    """Otherwise a finished run would swallow a code meant for the current one."""
    done = broker.open(task_id="t")
    broker.close(done.request_id, STATUS_CLOSED)
    assert broker.submit("123456", task_id="t") is None


def test_a_closed_request_never_shadows_a_waiting_one(broker):
    """A finished run must not steal the code the current run is waiting for.

    The closed request is deliberately the *newer* of the two: if resolution ranks
    candidates before discarding the closed ones, the newer closed entry wins and the
    operator's code is refused even though a live request for the same task exists.
    """
    waiting = broker.open(task_id="t")
    closed = broker.open(task_id="t")
    broker.close(closed.request_id, STATUS_CLOSED)
    resolved = broker.submit("123456", task_id="t")
    assert resolved is not None, "the waiting request was shadowed by a closed one"
    assert resolved.request_id == waiting.request_id
    assert broker.get(closed.request_id).status == STATUS_CLOSED


def test_a_closed_match_does_not_block_an_email_fallback(broker):
    """The same shadowing through the email key, which is the last resort."""
    waiting = broker.open(email="me@x.test")
    closed = broker.open(email="me@x.test")
    broker.close(closed.request_id, STATUS_CLOSED)
    resolved = broker.submit("123456", email="me@x.test")
    assert resolved is not None and resolved.request_id == waiting.request_id


def test_resolution_ignores_requests_with_empty_keys(broker):
    broker.open()
    assert broker.submit("123456", task_id="") is None
    assert broker.submit("123456", email="") is None


# -- closing and expiry -------------------------------------------------------


def test_closing_marks_the_request_and_reports_success(broker):
    request = broker.open(task_id="t")
    assert broker.close(request.request_id) is True
    assert request.status == STATUS_CLOSED


def test_closing_an_unknown_request_reports_failure(broker):
    assert broker.close("nope") is False


def test_closing_does_not_overwrite_an_already_submitted_code(broker):
    request = broker.open(task_id="t")
    broker.submit("123456", request_id=request.request_id)
    broker.close(request.request_id, STATUS_EXPIRED)
    assert request.status == STATUS_SUBMITTED and request.code == "123456"


def test_sweeping_expires_only_past_due_waiting_requests(broker):
    due = broker.open(task_id="due")
    live = broker.open(task_id="live")
    submitted = broker.open(task_id="done")
    broker.submit("123456", request_id=submitted.request_id)
    due.expires_at = time.time() - 1
    assert broker.sweep_expired() == 1
    assert due.status == STATUS_EXPIRED
    assert live.status == STATUS_WAITING
    assert submitted.status == STATUS_SUBMITTED


def test_sweeping_twice_does_not_double_count(broker):
    request = broker.open()
    request.expires_at = time.time() - 1
    assert broker.sweep_expired() == 1
    assert broker.sweep_expired() == 0


# -- the waiting list ---------------------------------------------------------


def test_the_waiting_list_holds_only_waiting_requests_oldest_first(broker):
    second = broker.open(task_id="second")
    first = broker.open(task_id="first")
    first.created_at, second.created_at = second.created_at, first.created_at
    done = broker.open(task_id="done")
    broker.submit("123456", request_id=done.request_id)
    listing = broker.list_waiting()
    assert [item["task_id"] for item in listing] == ["first", "second"]


def test_the_waiting_list_sweeps_expired_entries_first(broker):
    stale = broker.open(task_id="stale")
    stale.expires_at = time.time() - 1
    assert broker.list_waiting() == []
    assert stale.status == STATUS_EXPIRED


def test_the_registry_is_bounded():
    """An unbounded registry would grow for the lifetime of the process."""
    broker = ManualOtpBroker()
    for index in range(MAX_TRACKED_REQUESTS + 40):
        request = broker.open(task_id=f"t{index}")
        broker.submit("123456", request_id=request.request_id)
    assert len(broker._requests) <= MAX_TRACKED_REQUESTS


def test_eviction_never_discards_a_waiting_request():
    """Dropping a waiting request would silently strand the run it belongs to."""
    broker = ManualOtpBroker()
    pinned = broker.open(task_id="pinned")
    for index in range(MAX_TRACKED_REQUESTS + 40):
        request = broker.open(task_id=f"t{index}")
        broker.submit("123456", request_id=request.request_id)
    assert broker.get(pinned.request_id) is not None
    assert pinned.status == STATUS_WAITING


# -- waiting for the code -----------------------------------------------------


def test_wait_returns_the_code_once_submitted(broker):
    request = broker.open(task_id="t")
    threading.Timer(0.05, lambda: broker.submit("123456", request_id=request.request_id)).start()
    assert broker.wait(request.request_id, timeout=5) == "123456"


def test_wait_gives_up_when_the_window_closes(broker):
    request = broker.open(task_id="t")
    started = time.time()
    assert broker.wait(request.request_id, timeout=1) == ""
    assert time.time() - started < 5
    assert request.status != STATUS_WAITING


def test_wait_returns_immediately_for_an_unknown_request(broker):
    started = time.time()
    assert broker.wait("nope", timeout=30) == ""
    assert time.time() - started < 1


def test_wait_stops_promptly_when_the_task_is_cancelled(broker):
    """Cooperative cancellation is the reason the wait blocks in slices."""
    request = broker.open(task_id="t")
    started = time.time()
    code = broker.wait(request.request_id, timeout=60, cancel_check=lambda: True)
    elapsed = time.time() - started
    assert code == ""
    assert elapsed < POLL_SLICE_SECONDS * 4, f"took {elapsed:.2f}s to notice cancellation"
    assert request.status == STATUS_CLOSED


def test_a_submitted_code_wins_over_cancellation(broker):
    """Draining a code the operator already typed is better than discarding it."""
    request = broker.open(task_id="t")
    broker.submit("123456", request_id=request.request_id)
    assert broker.wait(request.request_id, timeout=5, cancel_check=lambda: True) == "123456"


def test_wait_rejects_a_non_callable_cancel_check(broker):
    request = broker.open(task_id="t")
    broker.submit("123456", request_id=request.request_id)
    assert broker.wait(request.request_id, timeout=5, cancel_check="not-callable") == "123456"


# -- concurrency --------------------------------------------------------------


def test_only_one_of_many_concurrent_submissions_wins(broker):
    request = broker.open(task_id="t")
    results: list = []
    lock = threading.Lock()

    def submit(value):
        outcome = broker.submit(value, request_id=request.request_id)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=submit, args=(f"{n:06d}",)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert broker.get(request.request_id).status == STATUS_SUBMITTED


def test_a_code_delivered_during_the_wait_is_never_lost(broker):
    request = broker.open(task_id="t")
    threading.Timer(0.2, lambda: broker.submit("424242", request_id=request.request_id)).start()
    assert broker.wait(request.request_id, timeout=10) == "424242"
