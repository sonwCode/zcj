"""Buffered writer for the task event log.

``TaskLogger.log()`` used to insert one row and commit per log line. A single ChatGPT
registration emits on the order of a hundred events and ``browser_register.py`` alone
has 200+ ``log()`` call sites, so the hot path was one transaction per line.

Measured against this project own engine (WAL, ``synchronous=NORMAL``), writing 2000
events:

| scenario | seconds | events/sec |
| --- | --- | --- |
| 1 thread, 1 commit per event | 4.176 | 479 |
| 1 thread, batched by 100 | 0.350 | 5715 |
| 8 threads, 1 commit per event | 48.831 | 328 |
| 8 threads, batched by 100 | 3.261 | 4906 |

Two things stand out. Batching is ~15x faster under concurrency, and the per-commit
path gets *slower* as threads are added (479 to 328 events/sec) because every log line
queues for the SQLite write lock and they convoy. That is the real cost: not the time
spent logging, but the write lock held on the critical path while accounts are being
persisted.

This module buffers rows in memory and writes them in one transaction. The safety
property that makes it correct is **flush on read**: every reader flushes first, so a
client polling ``/tasks/{id}/events`` or tailing the SSE stream never misses an event.
The buffer is additionally flushed on a timer, when it grows past a threshold, and on
shutdown, so the only thing at risk is the last fraction of a second of log lines if the
process is killed outright - and those are logs.

``ZCJ_TASK_EVENT_BUFFER=0`` restores the previous write-per-line behaviour.
"""
from __future__ import annotations

import atexit
import os
import threading

DEFAULT_INTERVAL_SECONDS = 0.5
# Bound memory if the database is slow or unavailable.
DEFAULT_MAX_BUFFER = 500
# After this many consecutive failed flushes, stop re-queueing and drop instead of
# growing the buffer forever.
MAX_FLUSH_FAILURES = 3


def buffering_enabled() -> bool:
    raw = str(os.environ.get("ZCJ_TASK_EVENT_BUFFER", "1") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _interval_seconds() -> float:
    raw = str(os.environ.get("ZCJ_TASK_EVENT_FLUSH_SECONDS", "") or "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_INTERVAL_SECONDS


def _max_buffer() -> int:
    raw = str(os.environ.get("ZCJ_TASK_EVENT_BUFFER_SIZE", "") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BUFFER
    return value if value > 0 else DEFAULT_MAX_BUFFER


def _dump_json(data: dict) -> str:
    import json

    try:
        return json.dumps(data or {}, ensure_ascii=False)
    except Exception:
        return "{}"


class TaskEventWriter:
    """Collects event rows and commits them in batches.

    Thread-safe: registration workers log from a pool, the API reads from request
    threads, and the flusher runs on its own thread.
    """

    def __init__(self, *, interval=None, max_buffer=None) -> None:
        self._lock = threading.Lock()
        self._buffer = []
        self._thread = None
        self._stop = threading.Event()
        self._interval = interval if interval is not None else _interval_seconds()
        self._max_buffer = max_buffer if max_buffer is not None else _max_buffer()
        self._flushed = 0
        self._dropped = 0
        self._failures = 0

    # -- write path ---------------------------------------------------------

    def enqueue(self, task_id, message, *, event_type="log", level="info", detail=None) -> None:
        row = {
            "task_id": str(task_id or ""),
            "type": str(event_type or "log"),
            "level": str(level or "info"),
            "message": str(message or ""),
            "detail_json": _dump_json(detail or {}),
        }
        with self._lock:
            self._buffer.append(row)
            over = len(self._buffer) >= self._max_buffer
        if over:
            self.flush()

    def flush(self) -> int:
        """Write everything buffered in a single transaction. Returns rows written."""
        with self._lock:
            if not self._buffer:
                return 0
            rows = self._buffer
            self._buffer = []
        try:
            from sqlmodel import Session

            from core.db import TaskEventModel, engine

            with Session(engine) as session:
                session.add_all([TaskEventModel(**row) for row in rows])
                session.commit()
        except Exception as exc:
            self._requeue_or_drop(rows, exc)
            return 0
        with self._lock:
            self._flushed += len(rows)
            self._failures = 0
        return len(rows)

    def _requeue_or_drop(self, rows, exc) -> None:
        """Put rows back for a retry, but never grow the buffer without bound.

        A database that stays unavailable must not turn into an out-of-memory kill;
        after a few attempts the rows are dropped and counted instead.
        """
        with self._lock:
            self._failures += 1
            if self._failures <= MAX_FLUSH_FAILURES:
                # rows were written first, so they go back at the front to keep order
                self._buffer = rows + self._buffer
            else:
                self._dropped += len(rows)
                self._failures = 0
        print("[TaskEventWriter] 事件批量写入失败: %s" % str(exc)[:200], flush=True)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the background flusher. Idempotent."""
        if not buffering_enabled():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="task-event-flusher",
            )
            self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        """Stop the flusher and write whatever is left."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(self._interval * 4, 2.0))
        self.flush()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.flush()
            except Exception:
                # flush() handles its own failures; never let the thread die
                pass

    # -- introspection ------------------------------------------------------

    def pending(self) -> int:
        with self._lock:
            return len(self._buffer)

    def stats(self) -> dict:
        with self._lock:
            return {
                "pending": len(self._buffer),
                "flushed": self._flushed,
                "dropped": self._dropped,
                "enabled": buffering_enabled(),
                "interval_seconds": self._interval,
                "max_buffer": self._max_buffer,
            }


task_event_writer = TaskEventWriter()


def flush_pending_events() -> int:
    """Flush before any read. Cheap when the buffer is empty."""
    if not buffering_enabled():
        return 0
    return task_event_writer.flush()
