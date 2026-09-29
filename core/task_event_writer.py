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
import json
import os
from pathlib import Path
import threading

DEFAULT_INTERVAL_SECONDS = 0.5
# Bound memory if the database is slow or unavailable.
DEFAULT_MAX_BUFFER = 500
# Flush retries stay bounded, but rows are retained in a durable local spool
# before they are dropped so a transient database outage cannot erase events.
MAX_FLUSH_FAILURES = 3


def _default_spool_path() -> Path:
    configured = str(os.environ.get("ZCJ_TASK_EVENT_SPOOL", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent / "data" / "task_events.spool.jsonl"


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

    def __init__(self, *, interval=None, max_buffer=None, spool_path=None) -> None:
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._buffer = []
        self._thread = None
        self._stop = threading.Event()
        self._interval = interval if interval is not None else _interval_seconds()
        self._max_buffer = max_buffer if max_buffer is not None else _max_buffer()
        self._spool_path = Path(spool_path) if spool_path else _default_spool_path()
        self._flushed = 0
        self._spooled = 0
        self._dropped = 0
        self._failures = 0

    def _read_spool_rows(self) -> list[dict]:
        """Read durable rows without moving them into memory permanently."""
        try:
            handle = self._spool_path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return []
        rows = []
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(row, dict) and row.get("task_id") is not None:
                    rows.append(row)
        return rows

    def _clear_spool(self) -> None:
        try:
            self._spool_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _persist_spool(self, rows) -> None:
        existing = self._read_spool_rows()
        all_rows = existing + list(rows)
        if not all_rows:
            return
        self._spool_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._spool_path.with_name(self._spool_path.name + ".tmp")
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for row in all_rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self._spool_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

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
        """Commit memory and durable rows in order, with one writer at a time."""
        with self._flush_lock:
            with self._lock:
                rows = self._buffer
                self._buffer = []
            try:
                spool_rows = self._read_spool_rows()
            except OSError as exc:
                if rows:
                    self._requeue_or_drop(rows, exc)
                else:
                    print("[TaskEventWriter] 事件 spool 读取失败: %s" % str(exc)[:200], flush=True)
                return 0
            all_rows = spool_rows + rows
            if not all_rows:
                return 0
            try:
                from sqlmodel import Session

                from core.db import TaskEventModel, engine

                with Session(engine) as session:
                    session.add_all([TaskEventModel(**row) for row in all_rows])
                    session.commit()
            except Exception as exc:
                # Rows already present on disk stay there; only new memory rows
                # need an in-memory retry or a new durable append.
                if rows:
                    self._requeue_or_drop(rows, exc)
                else:
                    print("[TaskEventWriter] 事件 spool 批量写入失败: %s" % str(exc)[:200], flush=True)
                return 0
            with self._lock:
                self._flushed += len(all_rows)
                self._failures = 0
            self._clear_spool()
            return len(all_rows)

    def _requeue_or_drop(self, rows, exc) -> None:
        """Retry briefly, then persist rows locally instead of discarding them."""
        persist = False
        with self._lock:
            self._failures += 1
            if self._failures <= MAX_FLUSH_FAILURES:
                # rows were removed before the transaction; put them back in order.
                self._buffer = rows + self._buffer
            else:
                self._failures = 0
                persist = True
        if persist:
            try:
                self._persist_spool(rows)
                with self._lock:
                    self._spooled += len(rows)
            except Exception as spool_exc:
                with self._lock:
                    self._dropped += len(rows)
                print("[TaskEventWriter] 事件 spool 写入失败: %s" % str(spool_exc)[:200], flush=True)
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
                "spooled": self._spooled,
                "spool_path": str(self._spool_path),
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
