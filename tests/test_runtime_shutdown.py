from __future__ import annotations

import time

import services.task_runtime as runtime_module
from core.lifecycle import LifecycleManager
from services.task_runtime import TaskRuntime


def test_task_runtime_stop_joins_dispatcher(monkeypatch):
    monkeypatch.setattr(runtime_module, "mark_incomplete_tasks_interrupted", lambda: None)
    monkeypatch.setattr(runtime_module, "claim_next_runnable_task", lambda **kwargs: None)

    runtime = TaskRuntime(poll_interval=0.01)
    runtime.start()
    deadline = time.monotonic() + 1
    while not runtime.get_status()["dispatcher_alive"] and time.monotonic() < deadline:
        time.sleep(0.01)

    runtime.stop(timeout=1)
    status = runtime.get_status()
    assert status["running"] is False
    assert status["dispatcher_alive"] is False
    assert status["worker_count"] == 0


def test_lifecycle_stop_interrupts_startup_wait():
    manager = LifecycleManager()
    manager.start()
    manager.stop(timeout=1)
    assert manager._thread is not None
    assert manager._thread.is_alive() is False
