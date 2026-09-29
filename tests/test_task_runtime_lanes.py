from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import application.tasks as tasks_module
from sqlmodel import Session

from application.tasks import (
    TASK_TYPE_ACCOUNT_CHECK,
    TASK_TYPE_CODEX_OAUTH,
    TASK_TYPE_GOPAY_PAY_CHATGPT,
    TASK_TYPE_PHONE_BIND,
    TASK_TYPE_PLATFORM_ACTION,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TASK_TYPE_REGISTER,
    claim_next_runnable_task,
    create_task,
    execute_task,
    recover_expired_task_leases,
    renew_task_lease,
    mark_incomplete_tasks_interrupted,
    task_lane,
)
from core.db import TaskModel, engine


def test_account_check_can_start_while_chatgpt_registration_uses_platform_slot():
    create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK,
        platform="chatgpt",
        payload={"account_id": 1001},
    )

    registration = claim_next_runnable_task(
        running_platform_counts={},
        busy_account_keys=set(),
        max_parallel_per_platform=1,
        running_lane_counts={"account_check": 0, "register": 0},
        lane_capacities={"account_check": 1, "register": 1},
    )
    assert registration is not None
    assert registration["lane"] == "register"

    check = claim_next_runnable_task(
        running_platform_counts={"chatgpt": 1},
        busy_account_keys=set(),
        max_parallel_per_platform=1,
        running_lane_counts={"account_check": 0, "register": 1},
        lane_capacities={"account_check": 1, "register": 1},
    )
    assert check is not None
    assert check["type"] == TASK_TYPE_ACCOUNT_CHECK
    assert check["lane"] == "account_check"


def test_account_check_lane_has_its_own_capacity():
    create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK,
        platform="chatgpt",
        payload={"account_id": 1001},
    )

    blocked = claim_next_runnable_task(
        running_platform_counts={"chatgpt": 1},
        busy_account_keys=set(),
        max_parallel_per_platform=1,
        running_lane_counts={"account_check": 1},
        lane_capacities={"account_check": 1},
    )
    assert blocked is None


def test_task_types_are_assigned_to_independent_operation_lanes():
    assert task_lane(TASK_TYPE_REGISTER) == "register"
    assert task_lane(TASK_TYPE_ACCOUNT_CHECK) == "account_check"
    assert task_lane(TASK_TYPE_PHONE_BIND) == "account_action"
    assert task_lane(TASK_TYPE_CODEX_OAUTH) == "account_action"
    assert task_lane(TASK_TYPE_GOPAY_PAY_CHATGPT) == "payment"
    assert task_lane("future_task_type") == "main"


def test_restart_preserves_pending_tasks_but_interrupts_active_tasks():
    pending = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    active = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    with Session(engine) as session:
        active_model = session.get(TaskModel, active["id"])
        assert active_model is not None
        active_model.status = TASK_STATUS_RUNNING
        session.add(active_model)
        session.commit()

    mark_incomplete_tasks_interrupted()

    with Session(engine) as session:
        pending_model = session.get(TaskModel, pending["id"])
        active_model = session.get(TaskModel, active["id"])
        assert pending_model is not None and active_model is not None
        assert pending_model.status == TASK_STATUS_PENDING
        assert active_model.status == TASK_STATUS_INTERRUPTED
        assert active_model.error == "任务在服务重启后被中断"


def test_concurrent_claimers_can_only_claim_one_pending_row(monkeypatch):
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    # The database dispatch lock serializes occupancy calculation and claim
    # across both threads and processes; let two real callers race through it.

    def claim():
        return claim_next_runnable_task(
            max_parallel_per_platform=1,
            running_platform_counts={},
            busy_account_keys=set(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1
    with Session(engine) as session:
        claimed = session.get(TaskModel, task["id"])
        assert claimed is not None
        assert claimed.status == "claimed"



def test_persisted_platform_occupancy_blocks_second_registration_claim():
    first = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    second = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )

    claimed = claim_next_runnable_task(worker_id="worker-a")
    assert claimed is not None
    assert claimed["id"] == first["id"]

    blocked = claim_next_runnable_task(worker_id="worker-b")
    assert blocked is None

    with Session(engine) as session:
        pending = session.get(TaskModel, second["id"])
        assert pending is not None
        assert pending.status == TASK_STATUS_PENDING


def test_persisted_account_occupancy_blocks_same_account_across_workers():
    first = create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 42, "action_id": "check"},
    )
    second = create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 42, "action_id": "check"},
    )

    claimed = claim_next_runnable_task(worker_id="worker-a")
    assert claimed is not None
    assert claimed["id"] == first["id"]

    blocked = claim_next_runnable_task(worker_id="worker-b")
    assert blocked is None

    with Session(engine) as session:
        pending = session.get(TaskModel, second["id"])
        assert pending is not None
        assert pending.status == TASK_STATUS_PENDING


def test_persisted_lane_occupancy_blocks_second_check_task():
    first = create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 101},
    )
    second = create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK,
        platform="chatgpt",
        payload={"platform": "chatgpt", "account_id": 102},
    )

    claimed = claim_next_runnable_task(
        worker_id="worker-a",
        lane_capacities={"account_check": 1},
    )
    assert claimed is not None
    assert claimed["id"] == first["id"]

    blocked = claim_next_runnable_task(
        worker_id="worker-b",
        lane_capacities={"account_check": 1},
    )
    assert blocked is None

    with Session(engine) as session:
        pending = session.get(TaskModel, second["id"])
        assert pending is not None
        assert pending.status == TASK_STATUS_PENDING


def test_unexpected_handler_exception_finishes_active_task(monkeypatch):
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )

    def boom(_payload, _logger):
        raise RuntimeError("synthetic handler crash")

    monkeypatch.setattr(tasks_module, "_execute_register_task", boom)
    execute_task(task["id"])

    with Session(engine) as session:
        model = session.get(TaskModel, task["id"])
        assert model is not None
        assert model.status == TASK_STATUS_FAILED
        assert model.error == "任务执行异常: synthetic handler crash"
        assert model.finished_at is not None


def test_claim_records_owner_and_only_owner_can_renew():
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )

    claim = claim_next_runnable_task(worker_id="worker-a", lease_seconds=60)
    assert claim is not None
    assert claim["id"] == task["id"]

    with Session(engine) as session:
        model = session.get(TaskModel, task["id"])
        assert model is not None
        assert model.worker_id == "worker-a"
        assert model.lease_expires_at is not None
        assert model.lease_expires_at > time.time()

    assert renew_task_lease(task["id"], "worker-b") is False
    assert renew_task_lease(task["id"], "worker-a") is True


def test_expired_worker_lease_is_recovered_without_touching_pending():
    task = create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload={"platform": "chatgpt", "count": 1},
    )
    claim = claim_next_runnable_task(worker_id="worker-a", lease_seconds=60)
    assert claim is not None

    with Session(engine) as session:
        model = session.get(TaskModel, task["id"])
        assert model is not None
        model.lease_expires_at = time.time() - 1
        session.add(model)
        session.commit()

    assert task["id"] in recover_expired_task_leases()
    with Session(engine) as session:
        model = session.get(TaskModel, task["id"])
        assert model is not None
        assert model.status == TASK_STATUS_INTERRUPTED
        assert model.worker_id == ""
        assert model.lease_expires_at is None
