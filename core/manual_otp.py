"""Human-in-the-loop fallback for one-time codes.

When a mailbox never delivers the code, registration sat in a 600s dead wait and
then failed — burning the proxy lease and the mailbox while an operator could have
typed the code in seconds. This broker lets the dashboard inject the code.

The wait is cooperative: it blocks in short slices so a task cancellation is still
observed promptly, and it is keyed by request id, task id and email so the caller can
address it however it happens to identify the run.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

DEFAULT_MANUAL_TIMEOUT = 300
POLL_SLICE_SECONDS = 0.5
MAX_TRACKED_REQUESTS = 200

STATUS_WAITING = "waiting"
STATUS_SUBMITTED = "submitted"
STATUS_EXPIRED = "expired"
STATUS_CLOSED = "closed"


@dataclass
class ManualOtpRequest:
    request_id: str
    task_id: str
    email: str
    platform: str
    keyword: str
    created_at: float
    expires_at: float
    status: str = STATUS_WAITING
    code: str = ""
    submitted_at: float = 0.0
    note: str = ""

    @property
    def remaining_seconds(self) -> float:
        return max(self.expires_at - time.time(), 0.0)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "email": self.email,
            "platform": self.platform,
            "keyword": self.keyword,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "remaining_seconds": round(self.remaining_seconds, 1),
            "submitted_at": self.submitted_at,
            "note": self.note,
        }


class ManualOtpBroker:
    """Thread-safe registry of runs waiting for an operator-supplied code."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, ManualOtpRequest] = {}

    def open(
        self,
        *,
        task_id: str = "",
        email: str = "",
        platform: str = "",
        keyword: str = "",
        timeout: int | None = None,
        note: str = "",
    ) -> ManualOtpRequest:
        now = time.time()
        window = int(timeout or DEFAULT_MANUAL_TIMEOUT)
        request = ManualOtpRequest(
            request_id=uuid.uuid4().hex[:16],
            task_id=str(task_id or ""),
            email=str(email or ""),
            platform=str(platform or ""),
            keyword=str(keyword or ""),
            created_at=now,
            expires_at=now + max(window, 1),
            note=str(note or ""),
        )
        with self._lock:
            self._requests[request.request_id] = request
            self._evict_locked()
        return request

    def _evict_locked(self) -> None:
        if len(self._requests) <= MAX_TRACKED_REQUESTS:
            return
        finished = [
            item for item in self._requests.values()
            if item.status != STATUS_WAITING
        ]
        finished.sort(key=lambda item: item.created_at)
        for item in finished:
            if len(self._requests) <= MAX_TRACKED_REQUESTS:
                break
            self._requests.pop(item.request_id, None)

    def get(self, request_id: str) -> ManualOtpRequest | None:
        with self._lock:
            return self._requests.get(str(request_id or ""))

    def _resolve_locked(self, *, request_id: str = "", task_id: str = "", email: str = ""):
        if request_id:
            return self._requests.get(str(request_id))
        candidates = [item for item in self._requests.values() if item.status == STATUS_WAITING]
        if task_id:
            matches = [item for item in candidates if item.task_id and item.task_id == str(task_id)]
            if matches:
                return max(matches, key=lambda item: item.created_at)
        if email:
            matches = [item for item in candidates if item.email and item.email == str(email)]
            if matches:
                return max(matches, key=lambda item: item.created_at)
        return None

    def submit(
        self,
        code: str,
        *,
        request_id: str = "",
        task_id: str = "",
        email: str = "",
    ) -> ManualOtpRequest | None:
        value = str(code or "").strip()
        if not value:
            return None
        with self._lock:
            request = self._resolve_locked(request_id=request_id, task_id=task_id, email=email)
            if request is None or request.status != STATUS_WAITING:
                return None
            request.code = value
            request.status = STATUS_SUBMITTED
            request.submitted_at = time.time()
            return request

    def close(self, request_id: str, status: str = STATUS_CLOSED) -> bool:
        with self._lock:
            request = self._requests.get(str(request_id or ""))
            if request is None:
                return False
            if request.status == STATUS_WAITING:
                request.status = status
            return True

    def list_waiting(self) -> list[dict]:
        self.sweep_expired()
        with self._lock:
            waiting = [
                item.to_dict() for item in self._requests.values()
                if item.status == STATUS_WAITING
            ]
        waiting.sort(key=lambda item: item["created_at"])
        return waiting

    def sweep_expired(self) -> int:
        now = time.time()
        expired = 0
        with self._lock:
            for item in self._requests.values():
                if item.status == STATUS_WAITING and item.expires_at <= now:
                    item.status = STATUS_EXPIRED
                    expired += 1
        return expired

    def wait(
        self,
        request_id: str,
        *,
        timeout: int | None = None,
        cancel_check=None,
    ) -> str:
        """Block until the code is submitted, the window closes, or the task is cancelled."""
        deadline = time.time() + max(int(timeout or DEFAULT_MANUAL_TIMEOUT), 1)
        cancelled = cancel_check if callable(cancel_check) else (lambda: False)
        while time.time() < deadline:
            request = self.get(request_id)
            if request is None:
                return ""
            if request.status == STATUS_SUBMITTED:
                return request.code
            if request.status != STATUS_WAITING:
                return ""
            if cancelled():
                self.close(request_id)
                return ""
            time.sleep(min(POLL_SLICE_SECONDS, max(deadline - time.time(), 0.05)))
        self.close(request_id, STATUS_EXPIRED)
        return ""


manual_otp_broker = ManualOtpBroker()