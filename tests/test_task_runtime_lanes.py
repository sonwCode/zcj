from __future__ import annotations

from application.tasks import (
    TASK_TYPE_ACCOUNT_CHECK,
    TASK_TYPE_CODEX_OAUTH,
    TASK_TYPE_GOPAY_PAY_CHATGPT,
    TASK_TYPE_PHONE_BIND,
    TASK_TYPE_REGISTER,
    claim_next_runnable_task,
    create_task,
    task_lane,
)


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
