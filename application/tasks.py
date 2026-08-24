"""Task orchestration and persistence helpers."""
from __future__ import annotations

import copy
import json
import queue
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, select, func

from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.base_platform import AccountStatus, RegisterConfig
from core.datetime_utils import format_local_clock, serialize_datetime
from core.db import AccountModel, TaskEventModel, TaskLog, TaskModel, engine, save_account
from core.platform_accounts import build_platform_account
from core.proxy_utils import mask_proxy_url
from core.registration_logging import classify_registration_log
from core.registry import get
from infrastructure.platform_runtime import PlatformRuntime
from application.ctf_plus import CtfPlusAccountsService
from application.phone_binding import PhoneBindingService
from platforms._browser_backend import resolve_runtime_browser_mode

TASK_TYPE_REGISTER = "register"
TASK_TYPE_ACCOUNT_CHECK = "account_check"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"
TASK_TYPE_PLATFORM_ACTION = "platform_action"
TASK_TYPE_PHONE_BIND = "phone_bind"
TASK_TYPE_CODEX_OAUTH = "codex_oauth"
TASK_TYPE_GET_RT = "get_rt"
TASK_TYPE_GET_RT_BYPASS = "get_rt_bypass"
TASK_TYPE_GOPAY_PAY_CHATGPT = "gopay_pay_chatgpt"
TASK_TYPE_GOPAY_REGISTER_ACCOUNT = "gopay_register_account"

# Tasks are scheduled by lane rather than by one global worker bucket.  The
# mapping keeps the scheduler extensible: adding a task type to a lane does
# not require changing the dispatch loop or its accounting rules.
TASK_LANE_MAIN = "main"
TASK_LANE_REGISTER = "register"
TASK_LANE_ACCOUNT_CHECK = "account_check"
TASK_LANE_ACCOUNT_ACTION = "account_action"
TASK_LANE_PLATFORM_ACTION = "platform_action"
TASK_LANE_PAYMENT = "payment"

TASK_LANE_BY_TYPE = {
    TASK_TYPE_REGISTER: TASK_LANE_REGISTER,
    TASK_TYPE_ACCOUNT_CHECK: TASK_LANE_ACCOUNT_CHECK,
    TASK_TYPE_ACCOUNT_CHECK_ALL: TASK_LANE_ACCOUNT_CHECK,
    TASK_TYPE_PLATFORM_ACTION: TASK_LANE_PLATFORM_ACTION,
    TASK_TYPE_PHONE_BIND: TASK_LANE_ACCOUNT_ACTION,
    TASK_TYPE_CODEX_OAUTH: TASK_LANE_ACCOUNT_ACTION,
    TASK_TYPE_GET_RT: TASK_LANE_ACCOUNT_ACTION,
    TASK_TYPE_GET_RT_BYPASS: TASK_LANE_ACCOUNT_ACTION,
    TASK_TYPE_GOPAY_PAY_CHATGPT: TASK_LANE_PAYMENT,
    TASK_TYPE_GOPAY_REGISTER_ACCOUNT: TASK_LANE_PAYMENT,
}

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"
TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"
TASK_STATUS_CANCELLED = "cancelled"

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_CANCELLED,
}
ACTIVE_TASK_STATUSES = {
    TASK_STATUS_CLAIMED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_CANCEL_REQUESTED,
}

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()

_CHATGPT_PROXY_PREFLIGHT_CACHE_TTL_SECONDS = 90.0
_CHATGPT_PROXY_PREFLIGHT_CACHE_MAX_ENTRIES = 256
_chatgpt_proxy_preflight_cache: dict[str, tuple[float, str]] = {}
_chatgpt_proxy_preflight_inflight: dict[str, threading.Event] = {}
_chatgpt_proxy_preflight_cache_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _serialize_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _is_global_sms_pool_exhausted_error(error: object) -> bool:
    return "SMS_POOL_EXHAUSTED" in str(error or "")


def _is_current_sms_phone_exhausted_error(error: object) -> bool:
    return "SMS_PHONE_EXHAUSTED" in str(error or "")


def _register_task_outcome(
    *,
    target_count: int,
    success_count: int,
    submitted_attempts: int,
    attempt_errors: list[str],
) -> dict[str, Any]:
    """Build target-level registration status without hiding retry diagnostics."""
    target = max(int(target_count or 0), 0)
    success = max(int(success_count or 0), 0)
    attempts = max(int(submitted_attempts or 0), 0)
    target_failures = max(target - min(success, target), 0)
    attempt_failures = len(attempt_errors)
    summary = f"完成: 成功 {success} 个, 失败 {target_failures} 个"
    if attempts != target or attempt_failures != target_failures:
        summary += f"（共尝试 {attempts} 次，尝试失败 {attempt_failures} 次）"
    status = TASK_STATUS_SUCCEEDED if target_failures == 0 else TASK_STATUS_FAILED
    final_error = ""
    if status == TASK_STATUS_FAILED:
        final_error = attempt_errors[-1] if attempt_errors else "未达到目标成功数量"
    return {
        "target_count": target,
        "success_count": success,
        "failure_count": target_failures,
        "attempt_count": attempts,
        "attempt_failure_count": attempt_failures,
        "summary": summary,
        "status": status,
        "final_error": final_error,
    }


_REGISTRATION_FAILURE_CATEGORIES = (
    ("email_already_registered", "邮箱已注册", ("user_already_exists", "account already exists")),
    ("mailbox_auth", "邮箱授权失效", ("invalid_grant", "refresh_token", "unauthorized or expired")),
    ("mailbox_otp", "邮箱取码失败", ("获取验证码", "等待验证码", "mailbox_otp")),
    (
        "proxy_network",
        "代理或网络异常",
        (
            "proxy",
            "proxy_network_error",
            "proxy_or_access_blocked",
            "unsupported_region",
            "connect tunnel",
            "tls connect",
            "device id",
            "curl:",
        ),
    ),
    ("account_rejected", "账号被远端拒绝", ("deactivated", "deleted", "suspicious")),
    (
        "phone_verification",
        "手机号验证失败",
        (
            "phone_number_in_use",
            "phone number already in use",
            "phone_number_rejected",
            "fraud_guard",
            "phone_country_pool_rejected",
            "手机号验证",
            "手机号",
            "add-phone",
            "add_phone",
        ),
    ),
    ("codex_credentials", "Codex 凭据获取失败", ("codex", "refresh_token is missing")),
    ("oauth", "OAuth 流程失败", ("oauth", "authorization", "authorize")),
)


def _registration_failure_category(error: object) -> tuple[str, str]:
    lowered = str(error or "unknown error").strip().lower()
    for code, label, markers in _REGISTRATION_FAILURE_CATEGORIES:
        if any(marker in lowered for marker in markers):
            return code, label
    return "other", "其他错误"


def _registration_failure_summary(errors: list[str]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for raw_error in errors:
        error = str(raw_error or "unknown error").strip()
        code, label = _registration_failure_category(error)
        item = grouped.setdefault(
            code,
            {"code": code, "label": label, "count": 0, "sample": error[:500]},
        )
        item["count"] += 1
    return sorted(grouped.values(), key=lambda item: (-int(item["count"]), str(item["code"])))


def _task_lock(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _task_locks[task_id] = lock
        return lock


def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:
    with _task_lock(task_id):
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            if not task:
                return None
            fn(task)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task


def _save_task_log(platform: str, email: str, status: str, error: str = "", detail: dict | None = None) -> None:
    with Session(engine) as session:
        log = TaskLog(
            platform=platform,
            email=email,
            status=status,
            error=error,
            detail_json=_dump_json(detail or {}),
        )
        session.add(log)
        session.commit()


def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"errors": [], "cashier_urls": [], "data": None}
    if result:
        base.update(result)
    return base


def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    if task_type in {TASK_TYPE_ACCOUNT_CHECK, TASK_TYPE_PLATFORM_ACTION}:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    if task_type in {TASK_TYPE_PHONE_BIND, TASK_TYPE_CODEX_OAUTH}:
        ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
        if not ids and int(payload.get("account_id") or 0) > 0:
            ids = [int(payload.get("account_id") or 0)]
        return [f"account:{account_id}" for account_id in ids]
    return []


def task_lane(task_type: str) -> str:
    """Return the scheduler lane for a task type.

    Unknown/future task types intentionally fall back to ``main`` so they keep
    the old scheduling behavior until explicitly assigned to another lane.
    """
    return str(TASK_LANE_BY_TYPE.get(task_type, TASK_LANE_MAIN))


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING, TASK_STATUS_CANCEL_REQUESTED},
        "progress": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        "progress_detail": {
            "current": progress_current,
            "total": progress_total,
            "label": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        },
        "success": int(task.success_count or 0),
        "error_count": int(task.error_count or 0),
        "errors": list(result.get("errors", [])),
        "cashier_urls": list(result.get("cashier_urls", [])),
        "data": result.get("data"),
        "result": result,
        "error": task.error,
        "created_at": _serialize_datetime(task.created_at),
        "started_at": _serialize_datetime(task.started_at),
        "finished_at": _serialize_datetime(task.finished_at),
        "updated_at": _serialize_datetime(task.updated_at),
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
        "detail": event.get_detail(),
        "created_at": _serialize_datetime(event.created_at),
    }


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_PENDING,
        payload_json=_dump_json(payload),
        result_json=_dump_json(_task_result_seed(result_seed)),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    count = max(int(payload.get("count", 1) or 1), 1)
    return create_task(
        task_type=TASK_TYPE_REGISTER,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=count,
    )


def create_account_check_task(account_id: int) -> dict[str, Any]:
    platform = ""
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            platform = model.platform
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK,
        platform=platform,
        payload={"account_id": int(account_id)},
        progress_total=1,
    )


def create_account_check_all_task(platform: str = "", limit: int = 50) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform=platform,
        payload={"platform": platform, "limit": int(limit or 50)},
        progress_total=max(int(limit or 50), 1),
    )


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,
    )


def create_phone_bind_task(payload: dict[str, Any]) -> dict[str, Any]:
    selected = [item for item in payload.get("ids") or [] if int(item or 0) > 0]
    fallback = [item for item in payload.get("fallback_ids") or [] if int(item or 0) > 0]
    total = len(selected) if selected else max(len(fallback), 1)
    return create_task(
        task_type=TASK_TYPE_PHONE_BIND,
        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),
        payload=payload,
        progress_total=total,
    )


def create_codex_oauth_task(payload: dict[str, Any]) -> dict[str, Any]:
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    account_id = int(payload.get("account_id") or 0)
    total = len(ids) if ids else (1 if account_id > 0 else 0)
    return create_task(
        task_type=TASK_TYPE_CODEX_OAUTH,
        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),
        payload=payload,
        progress_total=max(total, 1),
    )


def create_get_rt_task(payload: dict[str, Any]) -> dict[str, Any]:
    """批量获取 refresh_token 任务创建。

    payload 包含 ids（账号 ID 列表）、browser_mode、concurrency。
    """
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    total = len(ids) if ids else 1
    return create_task(
        task_type=TASK_TYPE_GET_RT,
        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),
        payload=payload,
        progress_total=max(total, 1),
    )


def create_get_rt_bypass_task(payload: dict[str, Any]) -> dict[str, Any]:
    """批量获取 refresh_token（绕过手机号）任务创建。"""
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    total = len(ids) if ids else 1
    return create_task(
        task_type=TASK_TYPE_GET_RT_BYPASS,
        platform=str(payload.get("platform", "chatgpt") or "chatgpt"),
        payload=payload,
        progress_total=max(total, 1),
    )


def create_gopay_pay_chatgpt_task(payload: dict[str, Any]) -> dict[str, Any]:
    """GoPay 协议付款 ChatGPT Plus 任务创建。

    payload 至少包含 ``chatgpt_account_ids: [int, ...]`` 或 ``register_count``；
    可选 ``gopay_account_id`` / ``cashier_url_override`` / ``midtrans_url_override``
    / ``country`` / ``currency`` / ``checkout_mode`` / ``bit_profile_id`` /
    ``envelope_url`` / ``concurrency`` / ``grab_timeout`` / ``phone_ttl_seconds``。
    progress_total = 选中账号数；若没选账号则用 register_count。
    """
    ids = [int(item) for item in payload.get("chatgpt_account_ids") or [] if int(item or 0) > 0]
    register_count = max(int(payload.get("register_count") or 0), 0)
    total = max(len(ids) or register_count, 1)
    return create_task(
        task_type=TASK_TYPE_GOPAY_PAY_CHATGPT,
        platform="chatgpt",
        payload=payload,
        progress_total=total,
    )


def create_gopay_register_account_task(payload: dict[str, Any]) -> dict[str, Any]:
    """GoPay 协议注册账号 + 设置 PIN 的单步任务。"""
    return create_task(
        task_type=TASK_TYPE_GOPAY_REGISTER_ACCOUNT,
        platform="gopay",
        payload=payload,
        progress_total=1,
    )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


def list_tasks(*, platform: str = "", status: str = "", page: int = 1, page_size: int = 50) -> dict[str, Any]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    with Session(engine) as session:
        q = select(TaskModel)
        total_q = select(func.count()).select_from(TaskModel)
        if platform:
            q = q.where(TaskModel.platform == platform)
            total_q = total_q.where(TaskModel.platform == platform)
        if status:
            q = q.where(TaskModel.status == status)
            total_q = total_q.where(TaskModel.status == status)
        q = q.order_by(TaskModel.created_at.desc())
        total = int(session.exec(total_q).one() or 0)
        items = session.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "items": [serialize_task(item) for item in items]}


def list_task_events(task_id: str, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    with Session(engine) as session:
        q = (
            select(TaskEventModel)
            .where(TaskEventModel.task_id == task_id)
            .where(TaskEventModel.id > since)
            .order_by(TaskEventModel.id)
            .limit(limit)
        )
        items = session.exec(q).all()
    return [serialize_event(item) for item in items]


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
    return serialize_event(event)


def mark_incomplete_tasks_interrupted() -> None:
    interrupted_ids: list[str] = []
    with Session(engine) as session:
        non_terminal = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)
        tasks = session.exec(
            select(TaskModel).where(TaskModel.status.in_(non_terminal))
        ).all()
        for task in tasks:
            task.status = TASK_STATUS_INTERRUPTED
            task.error = task.error or "任务在服务重启后被中断"
            task.finished_at = _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            interrupted_ids.append(task.id)
        session.commit()
    for task_id in interrupted_ids:
        append_task_event(
            task_id,
            "任务在服务重启后被标记为中断",
            event_type="state",
            level="warning",
        )


def request_cancel(task_id: str) -> Optional[dict[str, Any]]:
    task = _mutate_task(
        task_id,
        lambda model: _request_cancel_mutation(model),
    )
    if not task:
        return None
    append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
    return serialize_task(task)


def _request_cancel_mutation(task: TaskModel) -> None:
    if task.status in TERMINAL_TASK_STATUSES:
        return
    # Cancellation is terminal from the user's point of view.  The worker may
    # still be unwinding a bounded network/browser call, so all worker-side
    # cancellation checks also treat CANCELLED as a stop signal.
    task.status = TASK_STATUS_CANCELLED
    task.finished_at = _utcnow()
    task.error = task.error or "任务已取消"


def claim_next_runnable_task(
    *,
    running_platform_counts: dict[str, int] | None = None,
    busy_account_keys: set[str] | None = None,
    max_parallel_per_platform: int = 1,
    running_lane_counts: dict[str, int] | None = None,
    lane_capacities: dict[str, int] | None = None,
    platform_limited_lanes: set[str] | None = None,
    # Kept as compatibility aliases for callers from the two-lane revision.
    running_check_tasks: int | None = None,
    max_parallel_check_tasks: int | None = None,
) -> Optional[dict[str, Any]]:
    running_platform_counts = dict(running_platform_counts or {})
    busy_account_keys = set(busy_account_keys or set())
    running_lane_counts = dict(running_lane_counts or {})
    lane_capacities = dict(lane_capacities or {})
    if running_check_tasks is not None:
        running_lane_counts[TASK_LANE_ACCOUNT_CHECK] = int(running_check_tasks)
    if max_parallel_check_tasks is not None:
        lane_capacities[TASK_LANE_ACCOUNT_CHECK] = max(int(max_parallel_check_tasks), 1)
    platform_limited_lanes = set(platform_limited_lanes or {TASK_LANE_MAIN, TASK_LANE_REGISTER})
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel)
            .where(TaskModel.status == TASK_STATUS_PENDING)
            .order_by(TaskModel.created_at)
        ).all()
        for task in tasks:
            payload = task.get_payload()
            platform = task.platform or str(payload.get("platform", "") or "")
            lane = task_lane(task.type)
            lane_capacity = lane_capacities.get(lane)
            if lane_capacity is not None and running_lane_counts.get(lane, 0) >= max(int(lane_capacity), 1):
                continue
            account_keys = _task_account_keys(task.type, payload)
            # Platform limits are lane-local.  A check/action/payment task is
            # therefore independent from registration, while two registration
            # tasks for the same platform retain the original guard.
            platform_key = f"{lane}:{platform}" if platform else ""
            platform_count = running_platform_counts.get(platform_key, 0)
            # Accept the old unqualified key for direct callers/tests that
            # still pass the pre-lane accounting dictionary.
            if lane == TASK_LANE_MAIN and platform_count == 0:
                platform_count = running_platform_counts.get(platform, 0)
            if lane in platform_limited_lanes and platform and platform_count >= max_parallel_per_platform:
                continue
            if account_keys and busy_account_keys.intersection(account_keys):
                continue
            task.status = TASK_STATUS_CLAIMED
            task.started_at = task.started_at or _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            return {
                "id": task.id,
                "type": task.type,
                "lane": lane,
                "platform": platform,
                "account_keys": account_keys,
            }
    return None


class TaskLogger:
    def __init__(self, task_id: str, *, task_type: str = ""):
        self.task_id = task_id
        self.task_type = str(task_type or "")
        # 并发任务里每个 worker 通过 ``set_subtask`` 把自己的 subtask_id
        # 绑到 thread-local，之后 ``log()`` 自动把 ``subtask_id`` 注入
        # 事件 detail，前端按这个分组折叠展示。
        self._tlocal = threading.local()

    def set_subtask(self, subtask_id: str, label: str = "") -> None:
        """绑定当前线程的子任务标签。子任务结束后调 ``clear_subtask`` 解绑。

        ``subtask_id`` 是稳定标识（如 ``worker_1``）；``label`` 是给前端
        展示的人类可读标题（如"账号 #1"）。
        """
        self._tlocal.subtask_id = str(subtask_id or "")
        self._tlocal.subtask_label = str(label or "")

    def clear_subtask(self) -> None:
        try:
            del self._tlocal.subtask_id
        except AttributeError:
            pass
        try:
            del self._tlocal.subtask_label
        except AttributeError:
            pass

    def _current_subtask(self) -> tuple[str, str]:
        sid = getattr(self._tlocal, "subtask_id", "") or ""
        label = getattr(self._tlocal, "subtask_label", "") or ""
        return sid, label

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        # 自动给当前线程绑定的 subtask 加 detail，用于前端按 worker 分组折叠
        merged_detail = dict(detail or {})
        if self.task_type == TASK_TYPE_REGISTER:
            for key, value in classify_registration_log(
                message,
                level=level,
                event_type=event_type,
            ).items():
                merged_detail.setdefault(key, value)
        sid, slabel = self._current_subtask()
        if sid and "subtask_id" not in merged_detail:
            merged_detail["subtask_id"] = sid
        if slabel and "subtask_label" not in merged_detail:
            merged_detail["subtask_label"] = slabel
        append_task_event(
            self.task_id,
            message,
            event_type=event_type,
            level=level,
            detail=merged_detail or None,
        )
        prefix = f"[task:{self.task_id}]"
        if sid:
            prefix += f"[{sid}]"
        print(f"{prefix} {message}")

    def mark_running(self) -> None:
        def _update(task: TaskModel) -> None:
            if task.status in TERMINAL_TASK_STATUSES:
                return
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()

        _mutate_task(self.task_id, _update)
        self.log("任务已开始执行", event_type="state")

    def is_cancel_requested(self) -> bool:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return bool(task and task.status in {TASK_STATUS_CANCEL_REQUESTED, TASK_STATUS_CANCELLED})

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        current = max(int(current), 0)

        def _update(task: TaskModel) -> None:
            task.progress_current = current
            if total is not None:
                task.progress_total = max(int(total), 0)

        _mutate_task(self.task_id, _update)

    def record_success(self) -> None:
        def _update(task: TaskModel) -> None:
            task.success_count += 1

        _mutate_task(self.task_id, _update)

    def record_error(self, error: str) -> None:
        def _update(task: TaskModel) -> None:
            task.error_count += 1
            result = task.get_result()
            errors = list(result.get("errors", []))
            errors.append(error)
            result["errors"] = errors
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def record_sub2_sync(
        self,
        *,
        email: str,
        synced: bool,
        status: str,
        error: str = "",
    ) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            summary = dict(result.get("sub2_sync") or {})
            summary["attempted"] = int(summary.get("attempted") or 0) + 1
            normalized_status = str(status or "").strip().lower()
            if normalized_status == "imported_cooling":
                summary["cooling"] = int(summary.get("cooling") or 0) + 1
            if synced:
                counter = "synced"
            elif normalized_status.endswith("_pending") or normalized_status in {
                "credentials_pending",
                "registry_pending",
                "sync_pending",
            }:
                counter = "pending"
            elif normalized_status in {"invalid", "registry_ineligible", "ineligible"}:
                counter = "invalid"
            else:
                counter = "failed"
            summary[counter] = int(summary.get(counter) or 0) + 1
            items = list(summary.get("items") or [])
            items.append(
                {
                    "email": str(email or ""),
                    "status": str(status or ("active" if synced else "sync_failed")),
                    "error": str(error or "")[:500],
                }
            )
            summary["items"] = items[-100:]
            result["sub2_sync"] = summary
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def add_cashier_url(self, url: str) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            urls = list(result.get("cashier_urls", []))
            urls.append(url)
            result["cashier_urls"] = urls
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_result_data(self, data: Any) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        def _update(task: TaskModel) -> None:
            if task.status == TASK_STATUS_CANCELLED and status != TASK_STATUS_CANCELLED:
                return
            task.status = status
            task.finished_at = _utcnow()
            if error:
                task.error = error

        _mutate_task(self.task_id, _update)
        event_level = "error" if status == TASK_STATUS_FAILED else ("warning" if status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {status}",
            level=event_level,
            event_type="state",
            detail={"status": status, "error": error},
        )


def _auto_push_any2api(task_logger: TaskLogger, account) -> None:
    """注册成功后自动推送账号到 Any2API（如果已配置）。"""
    try:
        from core.any2api_sync import push_account_to_any2api
        push_account_to_any2api(account, log_fn=task_logger.log)
    except Exception as exc:
        task_logger.log(f"  [Any2API] 自动推送异常: {exc}", level="warning")


def _auto_push_sub2api(
    task_logger: TaskLogger,
    account,
    *,
    options: dict | None = None,
) -> bool | None:
    """Upload a successful ChatGPT account to Sub2API when configured."""
    try:
        from core.sub2api_sync import push_account_to_sub2api, sub2api_auto_sync_enabled

        opts = dict(options or {})
        enabled = sub2api_auto_sync_enabled()
        if "sub2api_auto_sync" in opts:
            # A registration task can explicitly opt in even when the global
            # backfill switch is off. Previously the task advertised/enforced
            # Sub2 delivery but this early global-only gate silently skipped it.
            enabled = _bool_config(opts.get("sub2api_auto_sync"), enabled)
        if not enabled:
            return None
        # Hard gate: never push web-only sessions without Codex RT.
        if not _account_has_codex_rt(account):
            msg = "skip Sub2 push: missing Codex access_token/refresh_token"
            task_logger.log(f"  [Sub2API] {msg}", level="warning")
            recorder = getattr(task_logger, "record_sub2_sync", None)
            if callable(recorder):
                recorder(
                    email=str(getattr(account, "email", "") or ""),
                    synced=False,
                    status="credentials_pending",
                    error=msg,
                )
            return False
        opts.setdefault("sub2api_auth_mode", "oauth")
        opts.setdefault("sub2api_allow_agent_identity", False)
        synced = push_account_to_sub2api(account, log_fn=task_logger.log, sync_options=opts)
        state = dict((getattr(account, "extra", {}) or {}).get("sub2api_sync") or {})
        recorder = getattr(task_logger, "record_sub2_sync", None)
        if callable(recorder):
            recorder(
                email=str(getattr(account, "email", "") or ""),
                synced=bool(synced),
                status=str(state.get("status") or ("active" if synced else "sync_failed")),
                error=str(state.get("last_error") or ""),
            )
        return bool(synced)
    except Exception as exc:
        task_logger.log(f"  [Sub2API] Auto sync error: {exc}", level="warning")
        recorder = getattr(task_logger, "record_sub2_sync", None)
        if callable(recorder):
            recorder(
                email=str(getattr(account, "email", "") or ""),
                synced=False,
                status="sync_failed",
                error=str(exc),
            )
        return False


def _auto_upload_cpa(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "chatgpt":
        return
    try:
        from core.config_store import config_store

        cpa_url = str(config_store.get("cpa_api_url", "") or "").strip()
        cpa_key = str(config_store.get("cpa_api_key", "") or "").strip()
        if not cpa_url:
            task_logger.log(
                "  [CPA] 自动上传跳过：未配置 API URL，请在“设置 -> ChatGPT -> CPA 面板”填写",
                level="warning",
            )
            return
        if not cpa_key:
            task_logger.log(
                "  [CPA] 自动上传跳过：未配置 API Key，请在“设置 -> ChatGPT -> CPA 面板”填写",
                level="warning",
            )
            return

        from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa
        from platforms.chatgpt.cpa_session import assert_workspace_cpa_json

        task_logger.log(f"  [CPA] 已启用自动上传: {cpa_url.rstrip('/')}")

        extra = account.extra or {}
        workspace_statuses = {}
        if isinstance(extra, dict):
            workspace_statuses.update(dict(extra.get("workspace_statuses") or {}))
            overview = dict(extra.get("account_overview") or {})
            workspace_statuses.update(dict(overview.get("workspace_statuses") or {}))

        workspace_uploads = 0
        workspace_failures = 0
        for workspace_id, status in workspace_statuses.items():
            if not isinstance(status, dict):
                continue
            if str(status.get("status") or "") != "export_ok":
                continue
            json_path = str(status.get("json_path") or "").strip()
            if not json_path:
                continue
            try:
                with open(json_path, "r", encoding="utf-8-sig") as fh:
                    token_data = json.load(fh)
                assert_workspace_cpa_json(token_data, workspace_id=str(workspace_id or ""))
                ok, msg = upload_to_cpa(token_data, api_url=cpa_url, api_key=cpa_key)
                if ok:
                    workspace_uploads += 1
                else:
                    workspace_failures += 1
                task_logger.log(
                    f"  [CPA] workspace {str(workspace_id)[:8]} "
                    f"{'✓ ' + msg if ok else '✗ ' + msg}"
                )
            except Exception as exc:
                workspace_failures += 1
                task_logger.log(
                    f"  [CPA] workspace {str(workspace_id)[:8]} 上传失败: {exc}",
                    level="warning",
                )

        if workspace_uploads or workspace_failures:
            task_logger.log(
                f"  [CPA] workspace JSON 自动上传完成: 成功 {workspace_uploads} 个, 失败 {workspace_failures} 个"
            )
            return

        class _AccountProxy:
            pass

        target = _AccountProxy()
        target.email = account.email
        target.access_token = extra.get("access_token") or account.token
        target.refresh_token = extra.get("refresh_token", "")
        target.id_token = extra.get("id_token", "")
        target.session_token = extra.get("session_token", "")
        target.user_id = account.user_id or ""
        target.account_id = account.user_id or ""
        target.cookies = extra.get("cookies", "")

        token_data = generate_token_json(target)
        ok, msg = upload_to_cpa(token_data, api_url=cpa_url, api_key=cpa_key)
        task_logger.log(f"  [CPA] {'✓ ' + msg if ok else '✗ ' + msg}")
    except Exception as exc:
        task_logger.log(f"  [CPA] 自动上传异常: {exc}", level="warning")


def _chatgpt_workspace_join_failure(account) -> str:
    if getattr(account, "platform", "") != "chatgpt":
        return ""
    extra = getattr(account, "extra", None) or {}
    if not isinstance(extra, dict):
        return ""
    workspace_join = extra.get("workspace_join")
    if not isinstance(workspace_join, dict):
        return ""
    if bool(workspace_join.get("ok")):
        return ""

    accept_result = workspace_join.get("accept_result")
    reason = str(workspace_join.get("error") or "").strip()
    if isinstance(accept_result, dict):
        reason = reason or str(
            accept_result.get("error") or accept_result.get("status") or ""
        ).strip()
    return f"Workspace Join 失败: {reason or 'not completed'}"


def _outlook_mailbox_account_from_platform_account(account) -> Any | None:
    extra = dict(getattr(account, "extra", {}) or {})
    resources = list(extra.get("provider_resources") or [])
    identity = dict(extra.get("identity") or {})
    if isinstance(identity.get("provider_resource"), dict):
        resources.append(identity["provider_resource"])
    for item in resources:
        if not isinstance(item, dict):
            continue
        provider_name = str(item.get("provider_name") or item.get("provider") or "").strip().lower()
        if provider_name not in {"outlook_email", "outlook_email_api"}:
            continue
        handle = str(item.get("handle") or item.get("email") or getattr(account, "email", "") or "").strip()
        resource_id = str(item.get("resource_identifier") or item.get("account_id") or "").strip()
        if not handle:
            continue
        from core.base_mailbox import MailboxAccount

        return MailboxAccount(
            email=handle,
            account_id=resource_id,
            extra={"provider_resource": item},
        )
    return None


def _resolve_outlook_mailbox_for_tagging(shared_mailbox, mailbox_account):
    if shared_mailbox is not None:
        if hasattr(shared_mailbox, "mark_registration_success") or hasattr(shared_mailbox, "mark_plus_success"):
            return shared_mailbox
        resolver = getattr(shared_mailbox, "_resolve_mailbox", None)
        if callable(resolver):
            try:
                resolved = resolver(mailbox_account)
                if hasattr(resolved, "mark_registration_success") or hasattr(resolved, "mark_plus_success"):
                    return resolved
            except Exception:
                pass

    try:
        from core.outlook_email_mailbox import OutlookEmailMailbox
        from infrastructure.provider_settings_repository import ProviderSettingsRepository

        settings = ProviderSettingsRepository().resolve_runtime_settings("mailbox", "outlook_email_api", {})
        if settings.get("outlook_email_api_url") and settings.get("outlook_email_api_key"):
            return OutlookEmailMailbox.from_config(settings)
    except Exception:
        return None
    return None


def _resolve_mailbox_for_method(shared_mailbox, mailbox_account, method_name: str):
    if shared_mailbox is None or mailbox_account is None:
        return None
    if callable(getattr(shared_mailbox, method_name, None)):
        return shared_mailbox
    resolver = getattr(shared_mailbox, "_resolve_mailbox", None)
    if callable(resolver):
        try:
            resolved = resolver(mailbox_account)
        except Exception:
            return None
        if callable(getattr(resolved, method_name, None)):
            return resolved
    return None


def _mark_protocol_mailbox_success(shared_mailbox, platform, logger: TaskLogger) -> None:
    worker = getattr(platform, "_last_protocol_mailbox_worker", None)
    mailbox_account = getattr(worker, "mailbox_account", None)
    mailbox = _resolve_mailbox_for_method(
        shared_mailbox,
        mailbox_account,
        "mark_registration_success",
    )
    if mailbox is None:
        return
    from core.local_ms_mailbox import LocalMicrosoftMailboxPool

    if not isinstance(mailbox, LocalMicrosoftMailboxPool):
        return
    try:
        mailbox.mark_registration_success(mailbox_account)
    except Exception as exc:
        logger.log(f"邮箱池成功状态写入失败: {exc}", level="warning")


def _mark_outlook_mailbox_event(shared_mailbox, account, event: str, logger: TaskLogger) -> None:
    mailbox_account = _outlook_mailbox_account_from_platform_account(account)
    if mailbox_account is None:
        return
    mailbox = _resolve_outlook_mailbox_for_tagging(shared_mailbox, mailbox_account)
    if mailbox is None:
        return
    try:
        if event == "registration_success":
            applied = mailbox.mark_registration_success(mailbox_account)
            label = "注册成功"
        elif event == "plus_success":
            applied = mailbox.mark_plus_success(mailbox_account)
            label = "Plus 开通成功"
        else:
            return
        if applied:
            logger.log(f"outlookEmail {label}后已打标签: {', '.join(applied)}")
    except Exception as exc:
        logger.log(f"outlookEmail 自动打标签失败（忽略）: {exc}", level="warning")


def _build_platform_instance(platform_name: str, payload: dict[str, Any], logger: TaskLogger, resolved_proxy: str | None = None, shared_mailbox=None):
    from core.base_identity import normalize_identity_provider
    from core.base_mailbox import create_mailbox

    executor_type = str(payload.get("executor_type", "protocol") or "protocol")
    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")
    extra = dict(payload.get("extra") or {})
    config = RegisterConfig(
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=resolved_proxy,
        extra=extra,
    )
    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
    mailbox = shared_mailbox
    if mailbox is None and identity_provider == "mailbox":
        if not extra.get("mail_provider"):
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=resolved_proxy,
        )

    platform_cls = get(platform_name)
    platform = platform_cls(config=config, mailbox=mailbox)
    if hasattr(platform, "set_logger"):
        platform.set_logger(logger.log)
    else:
        platform._log_fn = logger.log
    if hasattr(platform, "set_cancel_checker"):
        platform.set_cancel_checker(logger.is_cancel_requested)
    else:
        platform._cancel_check_fn = logger.is_cancel_requested
    return platform


def _run_single_account_check(account_id: int, logger: TaskLogger | None = None) -> tuple[bool | None, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        plugin = get(model.platform)(config=RegisterConfig())
        account = build_platform_account(session, model)

    valid = plugin.check_valid(account)
    check_overview = (
        plugin.get_last_check_overview() or {}
        if hasattr(plugin, "get_last_check_overview")
        else {}
    )
    check_credential_updates = (
        plugin.get_last_check_credential_updates() or {}
        if hasattr(plugin, "get_last_check_credential_updates")
        else {}
    )
    validity_status = str(check_overview.get("validity_status") or ("valid" if valid else "invalid"))
    validity_result: bool | None = True if validity_status == "valid" else False if validity_status == "invalid" else None
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            summary_updates = {
                "checked_at": _utcnow_iso(),
                "valid": validity_result,
                "validity_status": validity_status,
                **check_overview,
            }
            lifecycle_status = None
            if validity_result is True:
                # **bug 修复**：原实现 ``recover_lifecycle_status_for_valid_account``
                # 直接读 ``current_graph`` 老快照——但 plugin 刚拉到的新
                # ``plan_state`` 在 ``summary_updates`` 里、还没写回 graph，
                # 导致 free → 重新刷新仍然被认成 subscribed。这里把
                # ``summary_updates`` merge 到 graph 里再算 lifecycle。
                merged_graph = dict(current_graph)
                merged_overview = dict(merged_graph.get("overview") or {})
                merged_overview.update(summary_updates)
                merged_graph["overview"] = merged_overview
                lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)
            patch_account_graph(
                session,
                model,
                lifecycle_status=lifecycle_status,
                summary_updates=summary_updates,
                credential_updates=check_credential_updates,
            )
            session.add(model)
            session.commit()

    if account.platform == "chatgpt":
        try:
            from core.sub2api_sync import reconcile_sub2_remote_statuses

            remote_result = reconcile_sub2_remote_statuses(
                limit=1,
                account_ids=[account_id],
                log_fn=logger.log if logger else None,
            )
            if remote_result.get("invalid"):
                validity_status = "invalid"
                validity_result = False
                with Session(engine) as session:
                    refreshed_graph = load_account_graphs(session, [account_id]).get(account_id, {})
                    check_overview = dict(refreshed_graph.get("overview") or {})
        except Exception as exc:
            if logger:
                logger.log(f"  [Sub2API] Remote status reconciliation error: {exc}", level="warning")

    if validity_status == "invalid":
        try:
            from core.sub2api_sync import delete_synced_account

            delete_synced_account(
                account_id,
                reason=str(check_overview.get("validity_reason") or "invalid"),
                log_fn=logger.log if logger else None,
            )
        except Exception as exc:
            if logger:
                logger.log(f"  [Sub2API] Auto delete error: {exc}", level="warning")

    result = {
        "account_id": account_id,
        "valid": validity_result,
        "validity_status": validity_status,
        "validity_reason": check_overview.get("validity_reason"),
        "platform": account.platform,
        "email": account.email,
    }
    if logger:
        label = "有效" if validity_result is True else "失效" if validity_result is False else "检测状态未知"
        logger.log(f"{account.email}: {label}")
    return validity_result, result


def execute_task(task_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        task_type = task.type
        payload = task.get_payload()

    logger = TaskLogger(task_id, task_type=task_type)
    logger.mark_running()

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        return

    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_ACCOUNT_CHECK: _execute_account_check_task,
        TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_account_check_all_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
        TASK_TYPE_PHONE_BIND: _execute_phone_bind_task,
        TASK_TYPE_CODEX_OAUTH: _execute_codex_oauth_task,
        TASK_TYPE_GET_RT: _execute_get_rt_task,
        TASK_TYPE_GET_RT_BYPASS: _execute_get_rt_bypass_task,
        TASK_TYPE_GOPAY_PAY_CHATGPT: _execute_gopay_pay_chatgpt_task,
        TASK_TYPE_GOPAY_REGISTER_ACCOUNT: _execute_gopay_register_account_task,
    }
    handler = handlers.get(task_type)
    if not handler:
        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
        return
    handler(payload, logger)


def _resolve_sms_provider_for_task(extra: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    settings_repo = ProviderSettingsRepository()
    definitions_repo = ProviderDefinitionsRepository()
    provider_key = str(
        extra.get("sms_provider")
        or extra.get("phone_provider")
        or settings_repo.get_default_provider_key("sms")
        or ""
    ).strip()
    if not provider_key:
        provider_key = "sms_activate" if extra.get("sms_activate_api_key") else ""
    provider_key = {
        "herosms": "herosms_api",
        "smsbower": "smsbower_api",
        "sms_activate": "sms_activate_api",
    }.get(provider_key, provider_key)
    definition = definitions_repo.get_by_key("sms", provider_key) if provider_key else None
    settings = settings_repo.resolve_runtime_settings("sms", provider_key, extra) if definition else dict(extra)

    # 归一化接码参数：country 和 max_price
    settings["country"] = (
        extra.get("sms_country")
        or extra.get("country")
        or settings.get("sms_country")
        or settings.get("country")
        or "US"
    )
    settings["sms_country"] = settings["country"]
    settings["sms_countries"] = (
        extra.get("sms_countries")
        or extra.get("smsbower_countries")
        or settings.get("sms_countries")
        or settings.get("smsbower_countries")
        or ""
    )
    settings["max_price"] = (
        extra.get("smspool_max_price")
        or extra.get("sms_max_price")
        or extra.get("max_price")
        or settings.get("max_price")
        or ""
    )
    settings["sms_max_price"] = settings["max_price"]
    return provider_key, settings


def _complete_required_chatgpt_phone_verification(
    *,
    platform,
    account,
    extra: dict[str, Any],
    logger: "TaskLogger",
    country_offset: int = 0,
) -> None:
    """Finish add_phone through the configured SMS provider before success."""
    from core.base_sms import HERO_SMS_DEFAULT_SERVICE, create_phone_callbacks

    provider_key, settings = _resolve_sms_provider_for_task(extra)
    if not provider_key:
        raise RuntimeError("手机号验证已启用，但没有配置接码 Provider")

    country = str(settings.get("sms_country") or settings.get("country") or "").strip()
    settings["sms_country_offset"] = max(int(country_offset or 0), 0)
    service = str(
        settings.get("sms_service")
        or settings.get("smsbower_service")
        or settings.get("smsbower_default_service")
        or settings.get("herosms_service")
        or settings.get("herosms_default_service")
        or settings.get("sms_activate_service")
        or settings.get("sms_activate_default_service")
        or HERO_SMS_DEFAULT_SERVICE
    ).strip()
    phone_callback, cleanup = create_phone_callbacks(
        provider_key,
        settings,
        service=service,
        country=country,
        log_fn=logger.log,
    )
    executor_type = str(getattr(getattr(platform, "config", None), "executor_type", "") or "")
    protocol_mode = executor_type == "protocol"
    browser_mode = str(extra.get("phone_verification_browser_mode") or "camoufox_headed").strip()
    logger.log(
        "邮箱 Free 注册已完成，开始同账号手机号验证: "
        f"provider={provider_key} service={service} country={country or 'default'} "
        f"country_pool={settings.get('sms_countries') or '-'} "
        f"engine={'protocol' if protocol_mode else browser_mode}"
    )

    try:
        if protocol_mode:
            action_result = platform.complete_protocol_phone_verification(
                account,
                phone_callback=phone_callback,
                max_phone_attempts=min(
                    max(_int_config(extra.get("sms_phone_max_attempts"), 8), 1),
                    20,
                ),
            )
        else:
            action_result = platform.execute_action(
                "get_rt",
                account,
                {
                    "browser_mode": browser_mode,
                    "sms_provider": provider_key,
                    "phone_callback": phone_callback,
                },
            )
        if not isinstance(action_result, dict) or not action_result.get("ok"):
            error = (
                action_result.get("error")
                if isinstance(action_result, dict)
                else "手机号 OAuth 未返回结果"
            )
            raise RuntimeError(f"手机号验证失败: {error or 'unknown error'}")
        result_data = dict(action_result.get("data") or {})
        if (
            not result_data.get("already_verified")
            and not bool(getattr(phone_callback, "completed", False))
        ):
            raise RuntimeError("手机号验证未完成: OAuth 流程没有确认 add_phone 短信验证成功")

        account_extra = dict(getattr(account, "extra", {}) or {})
        prior_access_token = str(
            account_extra.get("web_access_token")
            or account_extra.get("access_token")
            or getattr(account, "token", "")
            or ""
        ).strip()
        for key in (
            "access_token",
            "refresh_token",
            "id_token",
            "session_token",
            "cookies",
            "oai_device_id",
        ):
            value = str(result_data.get(key) or "").strip()
            if value:
                account_extra[key] = value
        if result_data.get("auth_cookies"):
            account_extra["auth_cookies"] = json.dumps(
                result_data["auth_cookies"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if str(result_data.get("refresh_token") or "").strip():
            if prior_access_token and not str(account_extra.get("web_access_token") or "").strip():
                account_extra["web_access_token"] = prior_access_token
            account_extra["oauth_credential_type"] = "codex_oauth"
            account.token = str(account_extra.get("access_token") or getattr(account, "token", "") or "")
        account_id = str(result_data.get("account_id") or "").strip()
        if account_id:
            account.user_id = account_id

        activation = getattr(phone_callback, "activation", None)
        phone_number = str(
            result_data.get("phone_number")
            or getattr(activation, "phone_number", "")
            or ""
        ).strip()
        activation_country = str(getattr(activation, "country", "") or country).strip()
        overview = dict(account_extra.get("account_overview") or {})
        overview["phone_binding"] = {
            "status": "already_verified" if result_data.get("already_verified") else "bound",
            "provider": provider_key,
            "country": activation_country,
            "country_iso": str(result_data.get("phone_country") or ""),
            "phone": phone_number,
            "proxy_country": str(result_data.get("auth_proxy_country") or ""),
            "route_country_consistent": result_data.get("route_country_consistent"),
            "auth_generation": int(result_data.get("auth_generation") or 0),
            "bound_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        account_extra["account_overview"] = overview
        account_extra["register_mode"] = "email_then_phone"
        if phone_number:
            account_extra["phone_number"] = phone_number
        account.extra = account_extra

        require_rt = _bool_config(extra.get("require_codex_refresh_token"), True)
        has_rt = bool(str(account_extra.get("refresh_token") or "").strip())
        has_at = bool(str(account_extra.get("access_token") or getattr(account, "token", "") or "").strip())
        if require_rt and (not has_rt or not has_at):
            raise RuntimeError(
                "CODEX_RT_MISSING: 手机号已绑定，但账号缺少 Codex access_token/refresh_token，"
                "不能标记成功或上传 Sub2"
            )
        if has_rt:
            logger.log("邮箱 Free 账号手机号验证完成，已拿到 Codex RT，满足成功门槛")
        else:
            logger.log("邮箱 Free 账号手机号验证完成（未强制要求 Codex RT）")
    finally:
        if callable(cleanup):
            cleanup()


def _bool_config(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _account_has_codex_rt(account) -> bool:
    extra = dict(getattr(account, "extra", {}) or {})
    refresh = str(extra.get("refresh_token") or "").strip()
    access = str(extra.get("access_token") or getattr(account, "token", "") or "").strip()
    return bool(refresh and access)


def _upgrade_protocol_codex_credentials(
    *,
    platform_name: str,
    platform,
    account,
    executor_type: str,
    logger: "TaskLogger",
    require_success: bool = False,
) -> bool:
    """Ensure Codex OAuth tokens exist on the account. Returns True when RT is present."""
    if str(platform_name or "").strip().lower() != "chatgpt":
        return True
    if str(executor_type or "").strip().lower() != "protocol":
        return _account_has_codex_rt(account) if require_success else True
    account_extra = dict(getattr(account, "extra", {}) or {})
    if (
        str(account_extra.get("oauth_credential_type") or "").strip() == "codex_oauth"
        and str(account_extra.get("refresh_token") or "").strip()
        and str(account_extra.get("access_token") or getattr(account, "token", "") or "").strip()
    ):
        logger.log("Codex PKCE 凭据已在手机号验证阶段获取")
        return True
    upgrader = getattr(platform, "complete_protocol_codex_credentials", None)
    if not callable(upgrader):
        if require_success and not _account_has_codex_rt(account):
            raise RuntimeError("CODEX_RT_MISSING: protocol platform 无法补取 Codex RT")
        return _account_has_codex_rt(account)

    logger.log("开始将 ChatGPT Web 会话升级为 Codex PKCE 凭据...")
    try:
        result = upgrader(account)
    except Exception as exc:
        result = {"ok": False, "error_code": "codex_upgrade_failed", "error": str(exc)}
    if isinstance(result, dict) and result.get("ok"):
        # ensure tokens landed on account.extra
        tokens = dict(result.get("data") or {})
        extra = dict(getattr(account, "extra", {}) or {})
        for key in ("access_token", "refresh_token", "id_token"):
            value = str(tokens.get(key) or extra.get(key) or "").strip()
            if value:
                extra[key] = value
        if str(extra.get("refresh_token") or "").strip():
            extra["oauth_credential_type"] = "codex_oauth"
            account.token = str(extra.get("access_token") or getattr(account, "token", "") or "")
        account.extra = extra
        logger.log("Codex PKCE 凭据获取成功，已保存 access_token / id_token / refresh_token")
        return _account_has_codex_rt(account)

    error_code = str((result or {}).get("error_code") or "codex_upgrade_failed")
    error = str((result or {}).get("error") or "Codex OAuth did not return credentials")
    account_extra = dict(getattr(account, "extra", {}) or {})
    account_extra["oauth_credential_type"] = "chatgpt_web"
    account_extra["codex_credential_status"] = {
        "status": "phone_required" if error_code == "phone_required" else "pending",
        "error_code": error_code,
        "error": error[:500],
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    account.extra = account_extra
    if require_success:
        raise RuntimeError(
            f"CODEX_RT_MISSING: Codex PKCE 补取失败 ({error_code}): {error[:200]}"
        )
    logger.log(
        f"Codex PKCE 凭据本次未获取，账号保留且暂不进入 Sub2: {error_code}",
        level="warning",
    )
    return False


def _post_registration_liveness_delay_seconds(extra: dict[str, Any]) -> int:
    return min(
        max(_int_config(extra.get("post_registration_liveness_delay_seconds"), 15), 0),
        600,
    )


def _post_registration_chatgpt_liveness_error(
    *,
    platform_name: str,
    platform,
    account,
    extra: dict[str, Any],
    logger: "TaskLogger",
) -> str:
    """Reject SMS-created accounts already disabled by the remote service."""
    if str(platform_name or "").strip().lower() != "chatgpt":
        return ""
    account_extra = dict(getattr(account, "extra", {}) or {})
    register_mode = str(account_extra.get("register_mode") or "").strip().lower()
    sms_flow = register_mode in {"phone", "phone_with_email", "email_then_phone"} or _bool_config(
        extra.get("require_phone_verification"),
        False,
    )
    if not sms_flow:
        return ""

    delay_seconds = _post_registration_liveness_delay_seconds(extra)
    if delay_seconds:
        logger.log(f"注册完成，等待 {delay_seconds} 秒后执行远端存活复检...")
        deadline = time.monotonic() + delay_seconds
        while time.monotonic() < deadline:
            if logger.is_cancel_requested():
                raise RuntimeError("任务已取消")
            time.sleep(min(0.5, max(deadline - time.monotonic(), 0)))

    logger.log("开始验证新账号远端存活状态...")
    try:
        valid = bool(platform.check_valid(account))
    except Exception as exc:
        logger.log(f"新账号存活复检请求未完成，保留未知状态: {exc}", level="warning")
        return ""

    overview = (
        dict(platform.get_last_check_overview() or {})
        if hasattr(platform, "get_last_check_overview")
        else {}
    )
    account_overview = dict(account_extra.get("account_overview") or {})
    account_overview.update(overview)
    account_extra["account_overview"] = account_overview
    account.extra = account_extra
    validity_status = str(overview.get("validity_status") or "").strip().lower()
    if valid or validity_status == "valid":
        logger.log("新账号远端存活复检通过")
        return ""
    if validity_status != "invalid":
        logger.log("新账号远端存活状态暂不可判定，按 unknown 保存", level="warning")
        return ""

    account.status = AccountStatus.INVALID
    reason = str(overview.get("validity_reason") or "远端账号已停用").strip()
    logger.log(f"新账号远端存活复检失败: {reason}", level="error")
    return f"新账号注册后已被远端停用: {reason}"


def _registration_pipeline_update(
    account,
    stage: str,
    status: str,
    *,
    error: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """Persist registration and delivery as two independent state machines."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    account_extra = dict(getattr(account, "extra", {}) or {})
    overview = dict(account_extra.get("account_overview") or {})
    pipeline = dict(overview.get("registration_pipeline") or {})
    stages = dict(pipeline.get("stages") or {})
    stage_state = dict(stages.get(stage) or {})
    stage_state.update({"status": str(status or "unknown"), "updated_at": now})
    if error:
        stage_state["error"] = str(error)[:500]
    else:
        stage_state.pop("error", None)
    if detail:
        stage_state["detail"] = dict(detail)
    stages[stage] = stage_state
    pipeline["stages"] = stages
    pipeline["current_stage"] = stage
    pipeline.setdefault("started_at", now)
    pipeline["updated_at"] = now

    core_registration_stages = {
        "account_created",
        "phone_verified",
        "credentials_ready",
        "liveness",
        "persisted",
    }
    post_registration_stages = {
        "probation",
        "workspace_join",
        "payment",
        "post_registration",
    }
    if stage == "delivery":
        pipeline["delivery_status"] = str(status or "unknown")
        if status == "delivered":
            pipeline["delivered_at"] = now
    elif stage in post_registration_stages:
        pipeline["post_registration_status"] = str(status or "unknown")
    elif status == "failed" and stage in core_registration_stages:
        pipeline["registration_status"] = "failed"
    elif stage == "persisted" and status == "passed":
        pipeline["registration_status"] = "registered"
        pipeline["registered_at"] = now
    else:
        pipeline.setdefault("registration_status", "in_progress")

    overview["registration_pipeline"] = pipeline
    account_extra["account_overview"] = overview
    account.extra = account_extra


def _post_registration_probation_offsets(extra: dict[str, Any]) -> list[int]:
    # Compatibility wrapper: the old implementation accepted several one-off
    # offsets. Monitoring is now continuous, so only one repeat interval is
    # returned. Prefer the explicit interval, then the first legacy offset.
    raw = extra.get("post_registration_probation_interval_seconds")
    if raw in (None, ""):
        raw = extra.get("post_registration_probation_offsets_seconds", [60])
    values: list[Any]
    if isinstance(raw, str):
        values = [part.strip() for part in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    offsets: set[int] = set()
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            offsets.add(min(max(parsed, 15), 3600))
    return [min(offsets)] if offsets else [60]


def _int_config(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _register_concurrency_cap(platform_name: str, extra: dict[str, Any]) -> int:
    if str(platform_name or "").strip().lower() != "chatgpt":
        return 5
    cfg = dict((extra or {}).get("high_concurrency") or {})
    mode = str(cfg.get("mode") or "").strip().lower()
    requested = _int_config(cfg.get("concurrency"), 0)
    if mode in {"safe", "??"}:
        return 2
    if mode in {"balanced", "??"}:
        return 5
    if mode in {"high", "??", "high_speed"}:
        return 10
    if mode in {"extreme", "??"}:
        return 15
    if mode in {"custom", "???"} and requested > 0:
        return min(max(requested, 1), 20)
    return 1

def _register_retry_multiplier(extra: dict[str, Any], default: int = 3) -> int:
    identity_provider = str((extra or {}).get("identity_provider") or "").strip().lower()
    register_mode = str((extra or {}).get("register_mode") or "").strip().lower()
    if identity_provider in {"phone", "sms", "mobile"} or register_mode == "phone":
        return 1
    cfg = dict((extra or {}).get("high_concurrency") or {})
    raw = cfg.get("retry_multiplier", extra.get("retry_multiplier"))
    return min(max(_int_config(raw, default), 1), 8)


def _enforce_sub2_registration_requirements(
    platform_name: str,
    extra: dict[str, Any],
) -> bool:
    """Require phone verification when a mailbox registration targets Sub2."""
    if str(platform_name or "").strip().lower() != "chatgpt":
        return False
    try:
        from core.sub2api_sync import sub2api_auto_sync_enabled

        enabled = _bool_config(
            extra.get("sub2api_auto_sync"),
            sub2api_auto_sync_enabled(),
        )
    except Exception:
        enabled = _bool_config(extra.get("sub2api_auto_sync"), False)
    if not enabled:
        return False

    extra["sub2api_auto_sync"] = True
    # Sub2 只走 Codex OAuth 直传，禁止无 RT 成功 / 禁止 Agent Identity 默认路径
    extra["sub2api_auth_mode"] = str(extra.get("sub2api_auth_mode") or "oauth").strip() or "oauth"
    if "sub2api_allow_agent_identity" not in extra:
        extra["sub2api_allow_agent_identity"] = False
    if "require_codex_refresh_token" not in extra:
        extra["require_codex_refresh_token"] = True
    if "auto_download_agent_identity" not in extra:
        extra["auto_download_agent_identity"] = False
    identity_provider = str(extra.get("identity_provider") or "mailbox").strip().lower()
    if identity_provider == "mailbox":
        extra["require_phone_verification"] = True
        extra["register_mode"] = "email_then_phone"
    return True


def _gmail_base_key(email: str) -> str:
    text = str(email or "").strip().lower()
    if "@" not in text:
        return text
    local, domain = text.rsplit("@", 1)
    if domain not in {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return text
    return f"{local.split('+', 1)[0]}@{domain}"


def _inline_mailbox_concurrency_cap(pool_text: str, extra: dict[str, Any]) -> tuple[int, int, int]:
    try:
        from core.local_ms_mailbox import parse_local_ms_pool_rows
    except Exception:
        return 0, 0, 0
    entries = parse_local_ms_pool_rows(pool_text)
    bases = {_gmail_base_key(entry.email) for entry in entries if _gmail_base_key(entry.email)}
    if not bases:
        return 0, 0, 0
    cfg = dict((extra or {}).get("high_concurrency") or {})
    per_base = min(max(_int_config(cfg.get("gmail_base_concurrency"), 1), 1), 3)
    return max(len(bases) * per_base, 1), len(bases), per_base


def _resolve_registration_proxy_for_platform(
    platform_name: str,
    *,
    explicit_proxy: str | None,
    proxy_getter: Callable[[], str | None],
    allow_pool: bool = True,
) -> str | None:
    if explicit_proxy:
        return explicit_proxy
    return proxy_getter() if allow_pool else None


def _pin_chatgpt_registration_proxy(
    proxy_url: str | None,
    *,
    region: str,
    session_id: str,
) -> str | None:
    """Give each ChatGPT worker a maximum-length 711Proxy sticky route.

    Imported 711Proxy rows commonly default to ``sessTime-5``.  That is shorter
    than a complete signup/phone/OAuth workflow and lets several concurrent
    accounts share one rapidly changing exit.  Registration needs one stable,
    isolated route; non-711 URLs pass through unchanged.
    """
    if not proxy_url:
        return proxy_url
    from core.proxy_utils import infer_proxy_region, pin_711proxy_session

    route_region = str(region or infer_proxy_region(proxy_url) or "").strip().upper()
    if len(route_region) != 2:
        return proxy_url
    return pin_711proxy_session(
        proxy_url,
        region=route_region,
        session_id=session_id,
        session_minutes=180,
    ) or proxy_url


def _chatgpt_registration_proxy_policy(
    extra: dict[str, Any],
    *,
    explicit_proxy: str | None,
) -> tuple[str, str]:
    """Resolve the workbench proxy choice without changing legacy API defaults."""
    workbench = dict((extra or {}).get("chatgpt_register_workbench") or {})
    raw_strategy = str(
        (extra or {}).get("proxy_strategy")
        or workbench.get("proxy_strategy")
        or ""
    ).strip().lower()
    region = str(
        (extra or {}).get("proxy_country")
        or workbench.get("proxy_country")
        or ""
    ).strip().upper()
    if explicit_proxy:
        return "manual_template", region
    if raw_strategy in {"direct", "none", "off", "disabled"}:
        return "direct", region
    if raw_strategy in {"polling", "sticky"}:
        return raw_strategy, region
    # Older callers did not send a strategy and historically used the pool
    # when it existed, otherwise they continued without a proxy.
    return "auto", region


class _RegistrationProxyPreflightError(RuntimeError):
    code = "proxy_preflight_failed"
    proxy_failure = True


def _probe_chatgpt_registration_proxy(
    proxy: str | None,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> str:
    from platforms.chatgpt.http_client import OpenAIHTTPClient

    last_detail = "网络连接失败"
    for attempt in range(1, 4):
        client = OpenAIHTTPClient(proxy_url=proxy)
        try:
            ok, location = client.check_ip_location()
            if not ok:
                if location:
                    raise _RegistrationProxyPreflightError(
                        f"ChatGPT 协议注册代理预检失败: 地区 {location}"
                    )
                last_detail = "网络连接失败"
            else:
                nextauth_ok, status = client.check_nextauth_access()
                if nextauth_ok:
                    return str(location or "")
                if status:
                    raise _RegistrationProxyPreflightError(
                        f"ChatGPT 协议注册代理预检失败: NextAuth HTTP {status}"
                    )
                last_detail = "NextAuth 网络连接失败"
        finally:
            client.close()

        if attempt < 3:
            if callable(log_fn):
                log_fn(f"ChatGPT 协议代理预检网络中断，重试 ({attempt}/3)")
            time.sleep(attempt)

    raise _RegistrationProxyPreflightError(
        f"ChatGPT 协议注册代理预检失败: {last_detail}"
    )


def _preflight_chatgpt_registration_proxy(
    proxy: str | None,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> str:
    """Validate and briefly cache an OpenAI route before reserving a mailbox."""
    if not proxy:
        return ""

    proxy_key = str(proxy)
    while True:
        now = time.monotonic()
        with _chatgpt_proxy_preflight_cache_lock:
            cached = _chatgpt_proxy_preflight_cache.get(proxy_key)
            if cached is not None:
                checked_at, location = cached
                if now - checked_at < _CHATGPT_PROXY_PREFLIGHT_CACHE_TTL_SECONDS:
                    return location
                _chatgpt_proxy_preflight_cache.pop(proxy_key, None)

            pending = _chatgpt_proxy_preflight_inflight.get(proxy_key)
            if pending is None:
                pending = threading.Event()
                _chatgpt_proxy_preflight_inflight[proxy_key] = pending
                break

        pending.wait()

    try:
        location = _probe_chatgpt_registration_proxy(proxy, log_fn=log_fn)
    except BaseException:
        with _chatgpt_proxy_preflight_cache_lock:
            pending = _chatgpt_proxy_preflight_inflight.pop(proxy_key, None)
            if pending is not None:
                pending.set()
        raise

    with _chatgpt_proxy_preflight_cache_lock:
        checked_at = time.monotonic()
        expired_keys = [
            key
            for key, (cached_at, _location) in _chatgpt_proxy_preflight_cache.items()
            if checked_at - cached_at >= _CHATGPT_PROXY_PREFLIGHT_CACHE_TTL_SECONDS
        ]
        for key in expired_keys:
            _chatgpt_proxy_preflight_cache.pop(key, None)
        _chatgpt_proxy_preflight_cache[proxy_key] = (checked_at, str(location or ""))
        while len(_chatgpt_proxy_preflight_cache) > _CHATGPT_PROXY_PREFLIGHT_CACHE_MAX_ENTRIES:
            oldest_key = min(
                _chatgpt_proxy_preflight_cache,
                key=lambda key: _chatgpt_proxy_preflight_cache[key][0],
            )
            _chatgpt_proxy_preflight_cache.pop(oldest_key, None)
        pending = _chatgpt_proxy_preflight_inflight.pop(proxy_key, None)
        if pending is not None:
            pending.set()
    return str(location or "")


def _registration_error_counts_as_proxy_failure(exc: BaseException) -> bool:
    return bool(getattr(exc, "proxy_failure", False))


def _auto_followup_windsurf_payment(
    *,
    platform_name: str,
    payload: dict[str, Any],
    platform,
    account,
    logger: "TaskLogger",
) -> None:
    if platform_name != "windsurf":
        return
    executor_type = str(payload.get("executor_type", "") or "").strip()
    use_browser = executor_type in {"headless", "headed"}
    if not use_browser:
        extra_cfg = dict(payload.get("extra") or {})
        if not _bool_config(extra_cfg.get("auto_payment_link"), True):
            return
    if not str(getattr(account, "password", "") or "").strip() and use_browser:
        logger.log("Windsurf 注册后自动升级已跳过: 账号缺少密码", level="error")
        return
    extra = dict(payload.get("extra") or {})
    turnstile_token = str(extra.get("turnstile_token") or "").strip()
    if use_browser:
        action_id = "payment_link_browser"
        params = {
            "timeout": _int_config(extra.get("windsurf_payment_timeout"), 240),
            "headless": "true" if _bool_config(extra.get("windsurf_payment_headless"), False) else "false",
            "payment_channel": "checkout",
        }
        if turnstile_token:
            params["turnstile_token"] = turnstile_token
    else:
        action_id = "payment_link"
        params = {}
        if turnstile_token:
            params["turnstile_token"] = turnstile_token
    logger.log("注册成功，开始自动生成 Windsurf Pro Trial Stripe 链接")
    try:
        result = platform.execute_action(action_id, account, params)
    except Exception as exc:
        message = f"Windsurf 注册后自动升级失败: {exc}"
        logger.record_error(message)
        logger.log(message, level="error")
        return
    if not result.get("ok"):
        message = f"Windsurf 注册后自动升级失败: {result.get('error') or 'unknown error'}"
        logger.record_error(message)
        logger.log(message, level="error")
        return
    data = dict(result.get("data") or {})
    if data:
        merged_extra = dict(getattr(account, "extra", {}) or {})
        merged_extra.update(data)
        account.extra = merged_extra
        save_account(account)
    cashier_url = str(data.get("cashier_url") or data.get("url") or "").strip()
    if cashier_url:
        logger.log(f"Windsurf 自动升级链接已生成: {cashier_url}")
        logger.add_cashier_url(cashier_url)


def _shortlink_payment_enabled(payload: dict[str, Any]) -> bool:
    """CtfGptPlus 注册任务是否开了短链物理复用付款。"""
    extra = dict(payload.get("extra") or {})
    if not _bool_config(extra.get("auto_chatgpt_plus_payment"), False):
        return False
    payment_cfg = dict(extra.get("chatgpt_payment") or {})
    raw = payment_cfg.get("use_short_link")
    return raw is True or str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def _build_inbrowser_shortlink_checkout(
    *,
    payload: dict[str, Any],
    logger: "TaskLogger",
    proxy: str | None,
    sms_pool_override: str = "",
):
    """构造短链物理复用回调：注册完、浏览器还开着时，在**同一 page** 上生成
    短链 → 打开 → 跑 PayPal checkout。返回 ``post_register_in_browser(page,
    session_info) -> dict`` 给 ChatGPTBrowserRegister。

    付款参数从 ``extra.chatgpt_payment`` 取（country/currency/超时/captcha/
    sms_pool 等），跟 ``_auto_followup_chatgpt_plus_payment`` 一套来源，只是
    改成在已存在 page 上跑（不另开浏览器）。
    """
    from platforms.chatgpt import payment as payment_module

    extra = dict(payload.get("extra") or {})
    payment_cfg = dict(extra.get("chatgpt_payment") or {})
    country = str(payment_cfg.get("country") or "ID").strip() or "ID"
    currency = str(payment_cfg.get("currency") or "IDR").strip() or "IDR"
    checkout_timeout = _int_config(payment_cfg.get("checkout_timeout"), 180)
    address_region = str(payment_cfg.get("address_region") or "US").strip().upper() or "US"
    use_captcha = _bool_config(payment_cfg.get("use_captcha_service"), True)
    sms_pool_raw = sms_pool_override or str(payment_cfg.get("sms_pool") or "")
    sms_pool = []
    try:
        if sms_pool_raw.strip():
            sms_pool = payment_module.parse_sms_pool(sms_pool_raw)
    except Exception:
        sms_pool = []

    def _post_register(page, session_info: dict) -> dict:
        class _A:
            pass
        a = _A()
        a.access_token = str(session_info.get("access_token") or "")
        a.cookies = str(session_info.get("cookies") or "")
        if not a.access_token:
            logger.log("短链复用(PayPal)：注册结果没有 access_token，无法生成短链")
            return {"_shortlink_checkout": {"ok": False, "error": "no access_token"}}
        short_url = payment_module.generate_plus_link(
            a, proxy=None, country=country, currency=currency, use_short_link=True,
        )
        logger.log(f"短链已生成（PayPal 同浏览器复用）: {short_url[:70]}…")
        # turnstile solver：默认按设置走（这里简化为 None，captcha 用页面点击 +
        # 等待；要接 YesCaptcha 可在此按 use_captcha 注入 solver）。
        turnstile_solver = None
        res = payment_module.complete_paypal_checkout(
            checkout_url=short_url,
            cookies_str=a.cookies,
            proxy=None,
            email=str(session_info.get("email") or ""),
            payment_method="paypal",
            timeout=checkout_timeout,
            log_fn=logger.log,
            cancel_check=logger.is_cancel_requested,
            turnstile_solver=turnstile_solver,
            sms_pool=sms_pool or None,
            address_region=address_region,
            existing_page=page,  # 物理复用：在注册浏览器同一 page 上跑
        )
        return {"_shortlink_checkout": res}

    return _post_register


def _auto_followup_chatgpt_plus_payment(
    *,
    platform_name: str,
    payload: dict[str, Any],
    platform,
    account,
    logger: "TaskLogger",
    sms_pool_override: str = "",
    phone_swap_callback: Optional[Callable[[str], Optional[dict]]] = None,
) -> str:
    if platform_name != "chatgpt":
        return ""
    extra = dict(payload.get("extra") or {})
    if not _bool_config(extra.get("auto_chatgpt_plus_payment"), False):
        return ""

    payment_cfg = dict(extra.get("chatgpt_payment") or {})
    params: dict[str, Any] = {
        "plan": "plus",
        "country": str(payment_cfg.get("country") or "ID").strip() or "ID",
        "currency": str(payment_cfg.get("currency") or "IDR").strip() or "IDR",
        "auto_checkout": str(payment_cfg.get("auto_checkout", "true")).lower(),
        "payment_method": str(payment_cfg.get("payment_method") or "paypal").strip().lower() or "paypal",
        "headless": str(payment_cfg.get("headless", "false")).lower(),
        "checkout_timeout": _int_config(payment_cfg.get("checkout_timeout"), 180),
    }
    # 账单地址来源（meiguodizhi 接口分路）："US" / "JP"。空 / 非法值 plugin 层会
    # fallback 到 US，这里只做格式化透传。
    if payment_cfg.get("address_region") not in (None, ""):
        params["address_region"] = str(payment_cfg.get("address_region") or "").strip().upper()
    if payment_cfg.get("checkout_hold_seconds") not in (None, ""):
        params["checkout_hold_seconds"] = _int_config(payment_cfg.get("checkout_hold_seconds"), 10)
    if payment_cfg.get("proxy_region") not in (None, ""):
        params["proxy_region"] = str(payment_cfg.get("proxy_region") or "").strip().upper()
    if payment_cfg.get("checkout_mode") not in (None, ""):
        params["checkout_mode"] = str(payment_cfg.get("checkout_mode") or "").strip().lower()
    # Stripe 协议长链开关（accessToken → pay.openai.com，纯协议生成 cashier_url）
    if payment_cfg.get("use_stripe_init") not in (None, ""):
        params["use_stripe_init"] = str(payment_cfg.get("use_stripe_init")).strip().lower()
    # 短链开关（checkout_ui_mode=custom → chatgpt.com/checkout/openai_llc 短链）
    if payment_cfg.get("use_short_link") not in (None, ""):
        params["use_short_link"] = str(payment_cfg.get("use_short_link")).strip().lower()
    # bitbrowser_* 模式下需要 BitBrowser 客户端里手工建好的 profile ID
    # （见 platforms/_browser_backend.py BrowserBackendConfig.bitbrowser）。
    # 留空时插件层会回退到 BIT_PROFILE_ID 环境变量。
    if payment_cfg.get("bit_profile_id") not in (None, ""):
        params["bit_profile_id"] = str(payment_cfg.get("bit_profile_id") or "").strip()
    if payment_cfg.get("record_har") not in (None, ""):
        params["record_har"] = str(payment_cfg.get("record_har")).strip().lower()
    # 是否启用 YesCaptcha 求解；缺省 / 空 视为 true。"false" 时插件层会把
    # turnstile_solver 强制置 None，captcha 路径退化为"鼠标点击 + 10s 等待"。
    if payment_cfg.get("use_captcha_service") not in (None, ""):
        params["use_captcha_service"] = str(
            payment_cfg.get("use_captcha_service")
        ).strip().lower()
    # SMS 号码池：调用方（``_execute_register_task._do_one``）在并发槽里
    # acquire 了一条号字符串后通过 ``sms_pool_override`` 传进来，这里直接当
    # ``sms_pool`` 透传给 plugin。下游 ``parse_sms_pool`` 仍按原 textarea
    # 路径解析，但只看到一条号，不会跨线程偷其它槽的号。
    # 没传 override 时退化到原行为（把 textarea 全量传下去），保持兼容
    # 单测 / 老调用路径。
    if sms_pool_override:
        params["sms_pool"] = sms_pool_override
    elif payment_cfg.get("sms_pool") not in (None, ""):
        params["sms_pool"] = str(payment_cfg.get("sms_pool") or "")
    # 透传 phone swap callback —— Camoufox checkout 在 PayPal 拒号时会
    # 回调换一条全局空闲号继续。callback 由 ``_execute_register_task``
    # 持有 slot_queue 的闭包构造。
    if callable(phone_swap_callback):
        params["phone_swap_callback"] = phone_swap_callback

    logger.log("注册成功，开始自动生成 ChatGPT Plus 测试支付链接")
    try:
        result = platform.execute_action("payment_link", account, params)
    except Exception as exc:
        return f"ChatGPT Plus 支付链接生成失败: {exc}"

    data = dict(result.get("data") or {})
    cashier_url = str(data.get("cashier_url") or data.get("checkout_url") or data.get("url") or "").strip()
    open_url = str(
        data.get("paypal_authorize_url")
        or data.get("checkout_url")
        or data.get("url")
        or cashier_url
        or ""
    ).strip()
    protocol_extract = data.get("paypal_protocol_extract")
    action_ok = bool(result.get("ok"))
    if data or action_ok:
        merged_extra = dict(getattr(account, "extra", {}) or {})
        merged_extra.update(data)
        if cashier_url:
            merged_extra["cashier_url"] = cashier_url
        if action_ok:
            overview = dict(merged_extra.get("account_overview") or {})
            chips = [
                str(item)
                for item in (overview.get("chips") or [])
                if str(item or "").strip()
            ]
            if "Plus" not in chips:
                chips.append("Plus")
            overview.update(
                {
                    "plan_state": "subscribed",
                    "plan_name": "Plus",
                    "plan": "plus",
                    "membership_type": "plus",
                    "lifecycle_status": AccountStatus.SUBSCRIBED.value,
                    "chips": chips,
                }
            )
            if cashier_url:
                overview["cashier_url"] = cashier_url
            merged_extra["account_overview"] = overview
            account.status = AccountStatus.SUBSCRIBED
        account.extra = merged_extra
        save_account(account)
        logger.set_result_data({
            "account_email": getattr(account, "email", ""),
            "payment": data,
        })
    if open_url and (action_ok or not protocol_extract):
        logger.log(f"ChatGPT Plus 测试支付链接已生成: {open_url}")
        if cashier_url and cashier_url != open_url:
            logger.log(f"原始 cashier_url: {cashier_url}")
        logger.add_cashier_url(open_url)

    if not result.get("ok"):
        return f"ChatGPT Plus 支付链接生成失败: {result.get('error') or 'unknown error'}"
    return ""


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.build_info import build_identity
    from core.proxy_pool import proxy_pool

    payload = dict(payload or {})
    count = max(int(payload.get("count", 1) or 1), 1)
    platform_name = str(payload.get("platform", ""))
    email = payload.get("email") or None
    password = payload.get("password") or None
    proxy = payload.get("proxy") or None
    extra = dict(payload.get("extra") or {})
    logger.log(f"运行构建: {build_identity()}")
    # Policy enforcement may turn an ordinary mailbox task into an
    # email-then-phone flow (for example when Codex/Sub2 credentials are
    # required).  Apply it before deriving the effective registration mode,
    # concurrency window, SMS activation policy, and retry budget.
    sub2_sync_required = _enforce_sub2_registration_requirements(platform_name, extra)
    is_chatgpt_phone_registration = (
        platform_name == "chatgpt"
        and (
            str(extra.get("identity_provider") or "").strip().lower()
            in {"phone", "sms", "mobile"}
            or str(extra.get("register_mode") or "").strip().lower() == "phone"
        )
    )
    is_chatgpt_email_then_phone = (
        platform_name == "chatgpt"
        and _bool_config(extra.get("require_phone_verification"), False)
        and not is_chatgpt_phone_registration
    )
    # 高并发注册采用“先填满窗口、再排空窗口”的语义：
    # 例如目标成功 1 个、并发 5 时，5 个已经启动的流程都会继续走到
    # 各自的邮箱/手机号收尾；达到目标后只停止投放新尝试，不中断在途流程。
    complete_started_attempts = _bool_config(
        extra.get("complete_started_attempts"),
        False,
    )
    concurrency_cap = _register_concurrency_cap(platform_name, extra)
    requested_concurrency = max(int(payload.get("concurrency", 1) or 1), 1)
    # Email + phone registration spends most of its time waiting for SMS. A
    # target of one successful account should still be able to keep a small
    # window of independent mailbox/account attempts in flight. The account
    # itself remains sequential: phone retries rebuild its own OAuth session.
    concurrency = min(
        requested_concurrency,
        concurrency_cap,
        20 if (is_chatgpt_email_then_phone or complete_started_attempts) else count,
    )
    if email and (count > 1 or concurrency > 1):
        error = (
            "固定邮箱只能用于单账号、单并发注册；批量或并发任务必须由邮箱 provider "
            "为每个 worker 分配独立邮箱"
        )
        logger.log(error, level="error")
        logger.finish(TASK_STATUS_FAILED, error=error)
        return

    inline_mailbox_pool_text = ""
    inline_mailbox_pool_size = 0
    inline_mailbox_allow_reuse = _bool_config(extra.get("local_ms_pool_allow_reuse"), False)
    inline_api_lines = str(
        extra.get("chatgpt_api_mailbox_lines")
        or extra.get("api_mailbox_lines")
        or ""
    )
    if platform_name == "chatgpt" and inline_api_lines.strip():
        try:
            from core.local_ms_mailbox import (
                InlineMailboxPoolBuildResult,
                build_inline_mailbox_url_pool_text,
                split_unused_local_ms_pool_rows,
            )

            expanded_result = build_inline_mailbox_url_pool_text(
                inline_api_lines,
                gmail_alias_enabled=_bool_config(extra.get("gmail_alias_enabled"), False),
                gmail_alias_count=max(_int_config(extra.get("gmail_alias_count"), 1), 1),
            )
            unused_result = split_unused_local_ms_pool_rows(
                expanded_result.pool_text,
                state_file=str(extra.get("local_ms_pool_state_file") or ""),
            )
            if unused_result.unused_count <= 0:
                raise ValueError(
                    "邮箱列表中没有未使用账号: "
                    f"已用={unused_result.used_count}, 重复={unused_result.duplicate_count}, "
                    f"无效={unused_result.invalid_count}"
                )

            inline_result = InlineMailboxPoolBuildResult(
                pool_text=unused_result.pool_text,
                source_count=expanded_result.source_count,
                expanded_count=unused_result.unused_count,
                alias_count=min(expanded_result.alias_count, unused_result.unused_count),
            )
        except Exception as exc:
            msg = f"API mailbox list parse failed: {exc}"
            logger.log(msg, level="error")
            logger.finish(TASK_STATUS_FAILED, error=msg)
            return
        if not inline_result.pool_text.strip() or inline_result.expanded_count <= 0:
            msg = "API mailbox list has no valid rows. Expected one row per mailbox: email----https://..."
            logger.log(msg, level="error")
            logger.finish(TASK_STATUS_FAILED, error=msg)
            return

        inline_mailbox_pool_text = inline_result.pool_text
        inline_mailbox_pool_size = inline_result.expanded_count
        extra["identity_provider"] = "mailbox"
        extra["mail_provider"] = "local_ms_pool"
        extra["local_ms_pool_text"] = inline_mailbox_pool_text
        # The inline pool is already expanded above. Disable provider-level
        # expansion so aliases are not generated a second time at runtime.
        extra["gmail_alias_enabled"] = False
        extra["mailbox_alias_enabled"] = False
        extra.setdefault("local_ms_mailbox_url_timeout", 15)
        extra.setdefault("local_ms_mailbox_url_poll_interval", 2)

        if count > inline_mailbox_pool_size and not inline_mailbox_allow_reuse:
            msg = (
                f"API mailbox list only has {inline_mailbox_pool_size} usable row(s), "
                f"but requested count is {count}."
            )
            logger.log(msg, level="error")
            logger.finish(TASK_STATUS_FAILED, error=msg)
            return
        mailbox_cap, base_count, gmail_base_limit = _inline_mailbox_concurrency_cap(inline_mailbox_pool_text, extra)
        effective_mailbox_cap = mailbox_cap or inline_mailbox_pool_size
        concurrency = min(
            requested_concurrency,
            inline_mailbox_pool_size,
            concurrency_cap,
            effective_mailbox_cap,
            20 if is_chatgpt_email_then_phone else count,
        )
        logger.log(
            "API mailbox pool loaded: "
            f"source_rows={inline_result.source_count}, usable={inline_result.expanded_count}, "
            f"mailbox_aliases={inline_result.alias_count}, mailbox_bases={base_count}, "
            f"mailbox_base_limit={gmail_base_limit}, count={count}, concurrency={concurrency}"
        )
        if requested_concurrency > concurrency:
            logger.log(
                "High concurrency adjusted by mailbox safety: "
                f"requested={requested_concurrency}, effective={concurrency}. "
                "同一个 Gmail base 的 +alias 会限流，避免验证码串码。"
            )
    if platform_name == "chatgpt":
        hc = dict(extra.get("high_concurrency") or {})
        logger.log(
            "High concurrency profile: "
            f"mode={hc.get('mode') or 'default'}, requested={payload.get('concurrency')}, "
            f"effective={concurrency}, cap={concurrency_cap}, retry_multiplier={_register_retry_multiplier(extra)}"
        )
        if complete_started_attempts and concurrency > 1:
            logger.log(
                f"并发窗口已启用：先启动 {concurrency} 个流程；目标达成后，"
                "已启动流程继续完成当前邮箱/手机号步骤，不再投放新尝试"
            )
        if sub2_sync_required:
            if str(extra.get("identity_provider") or "").strip().lower() == "mailbox":
                logger.log(
                    "Sub2 auto sync enabled: registration will use email creation + "
                    "phone verification + Codex RT, then OAuth upload to Sub2"
                )
            else:
                logger.log(
                    "Sub2 auto sync enabled: phone verification and Agent Identity "
                    "upload are required"
                )

    payload["extra"] = extra

    # 强校验：ChatGPT Plus 自动支付链接 + sms_pool 模式下，**每个并发线程
    # 独占一条 SMS 号**——所以数量约束是 ``len(pool) >= concurrency``，**不是**
    # ``>= count``（注册数量）。多个 batch 跑下来，每个并发槽会被复用，但同
    # 一时刻同一条号只在一个线程里跑，不会错乱。
    sms_pool_slots: list[str] = []  # 启动后每个 slot 一条号字符串（"+phone----url"）
    sms_pool_extras: list[dict] = []  # 备份池：当某线程被 PayPal 拒号时换号用
    sms_pool_lock = threading.Lock()  # 保护 extras 的并发读取
    # 当某线程触发 swap 但 extras 为空时置 set —— 整个任务级别立刻停止投新任务，
    # 让正在跑的任务自然失败结束，避免下一批又抢同一条死号继续被拒。
    sms_pool_exhausted = threading.Event()
    if platform_name == "chatgpt" and _bool_config(
        extra.get("auto_chatgpt_plus_payment"), False
    ):
        payment_cfg = dict(extra.get("chatgpt_payment") or {})
        sms_pool_raw = str(payment_cfg.get("sms_pool") or "")
        if sms_pool_raw.strip():
            from platforms.chatgpt import payment as _chatgpt_payment_module
            try:
                parsed_pool = _chatgpt_payment_module.parse_sms_pool(sms_pool_raw)
            except Exception as exc:
                msg = f"SMS 号码池解析失败: {exc}"
                logger.log(msg, level="error")
                logger.finish(TASK_STATUS_FAILED, error=msg)
                return
            if len(parsed_pool) < concurrency:
                msg = (
                    f"SMS 号码池数量不足：并发数 {concurrency}，号码池仅 "
                    f"{len(parsed_pool)} 条。每个并发线程必须独占一条号，"
                    f"请在 SMS 号码池里至少填 {concurrency} 条 +phone----relay_url。"
                )
                logger.log(msg, level="error")
                logger.finish(TASK_STATUS_FAILED, error=msg)
                return
            # 前 concurrency 条作为初始并发槽；其余作为 extras 备份池——
            # 当某线程的号被 PayPal 拒后从 extras 换一条继续，extras 用完了
            # 就让该线程结束失败（前端会显示"号码不可用"）。
            sms_pool_slots = [
                (
                    f"{entry.get('phone_e164') or '+' + str(entry.get('phone', ''))}"
                    f"----{entry.get('relay_url', '')}"
                )
                for entry in parsed_pool[:concurrency]
            ]
            sms_pool_extras = list(parsed_pool[concurrency:])
            logger.log(
                f"SMS 号码池校验通过：{len(parsed_pool)} 条 ≥ 并发数 {concurrency}，"
                f"前 {concurrency} 条作并发槽，剩余 {len(sms_pool_extras)} 条作"
                "拒号换号备份池"
            )
    # 并发槽 → SMS 号映射：用 queue 让每个并发任务 acquire/release 一个槽位，
    # 同一时刻一个槽位只被一个线程占用，跑完归还供下一批复用。
    sms_slot_queue: "queue.Queue[int]" = queue.Queue()
    for slot_index in range(len(sms_pool_slots)):
        sms_slot_queue.put(slot_index)
    sms_provider_key, sms_settings = _resolve_sms_provider_for_task(extra)
    sms_activation_provider = sms_provider_key in {
        "herosms",
        "herosms_api",
        "smsbower",
        "smsbower_api",
    }
    sms_registration_flow = is_chatgpt_phone_registration or is_chatgpt_email_then_phone
    sms_reuse_enabled = _bool_config(
        sms_settings.get("register_reuse_phone_to_max"),
        True,
    )
    if (
        concurrency > 1
        and sms_registration_flow
        and sms_activation_provider
        and sms_reuse_enabled
    ):
        # Reusing one activation requires serial number -> code -> validation
        # ownership.  Keeping that mode enabled for a concurrent registration
        # task made the UI say "5 concurrent" while four workers waited behind
        # the process-wide reuse lock and never entered their SMS phase.  A
        # concurrent task instead gives every worker its own activation; the
        # provider's reuse optimisation remains available for concurrency=1.
        extra["register_reuse_phone_to_max"] = False
        sms_settings = dict(sms_settings)
        sms_settings["register_reuse_phone_to_max"] = False
        payload["extra"] = extra
        logger.log(
            "接码并发隔离已启用: "
            f"{concurrency} 个 worker 将各自租用独立 activation；"
            "号码复用仅在单并发任务中启用"
        )
    herosms_enabled = sms_provider_key in ("herosms", "herosms_api") and bool(str(sms_settings.get("herosms_api_key") or "").strip())
    hero_extra_max = max(_int_config(sms_settings.get("register_phone_extra_max"), 3), 0) if herosms_enabled else 0
    hero_reuse_to_max = _bool_config(sms_settings.get("register_reuse_phone_to_max"), True) if herosms_enabled else False
    target_success = count
    max_success = count + hero_extra_max if herosms_enabled and hero_reuse_to_max else count
    progress_total = max_success if herosms_enabled else count
    proxy_strategy, proxy_region = (
        _chatgpt_registration_proxy_policy(extra, explicit_proxy=proxy)
        if platform_name == "chatgpt"
        else ("auto", "")
    )
    allow_proxy_pool = proxy_strategy != "direct"
    registration_base_proxy = _resolve_registration_proxy_for_platform(
        platform_name,
        explicit_proxy=proxy,
        proxy_getter=lambda: proxy_pool.get_next(proxy_region),
        allow_pool=allow_proxy_pool,
    )
    if proxy_strategy in {"polling", "sticky"} and not registration_base_proxy:
        error = "代理策略要求使用代理池，但当前没有启用且可分配的代理"
        logger.log(error, level="error")
        logger.finish(TASK_STATUS_FAILED, error=error)
        return

    logger.set_progress(0, progress_total)
    if herosms_enabled:
        if hero_reuse_to_max:
            logger.log(
                f"HeroSMS 复用模式: 成功目标 {target_success}，失败自动补尝试，"
                f"号码仍可复用时最多额外成功 {hero_extra_max} 个"
            )
        else:
            logger.log(
                f"HeroSMS 独占模式: 成功目标 {target_success}，失败自动补尝试，"
                "每个并发 worker 使用独立 activation"
            )

    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    success = 0
    errors: list[str] = []
    successful_account_ids: list[int] = []
    successful_account_ids_lock = threading.Lock()
    delivery_pending_account_ids: list[int] = []
    delivery_pending_account_ids_lock = threading.Lock()
    failure_policy = str(
        extra.get("failure_policy")
        or ("retry_then_continue" if platform_name == "chatgpt" else "")
    ).strip().lower()
    network_circuit_break_threshold = min(
        max(_int_config(extra.get("network_circuit_break_threshold"), 3), 0),
        20,
    )
    network_failure_streak = 0
    network_circuit_open = False
    network_circuit_reason = ""
    if platform_name == "chatgpt" and network_circuit_break_threshold > 0:
        logger.log(
            "注册网络熔断已启用: "
            f"连续 {network_circuit_break_threshold} 次代理/网络失败后停止投放新尝试"
        )

    # Pre-create a shared mailbox instance for the entire task to avoid
    # concurrent initialization issues (e.g. MoeMail auto-registering
    # multiple provider accounts simultaneously).
    shared_mailbox = None
    try:
        from core.base_identity import normalize_identity_provider
        from core.base_mailbox import create_mailbox

        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
        if identity_provider == "mailbox":
            if inline_mailbox_pool_text:
                from core.local_ms_mailbox import LocalMicrosoftMailboxPool

                shared_mailbox = LocalMicrosoftMailboxPool(
                    pool_text=inline_mailbox_pool_text,
                    state_file=str(extra.get("local_ms_pool_state_file") or ""),
                    allow_reuse=_bool_config(extra.get("local_ms_pool_allow_reuse"), False),
                    proxy=registration_base_proxy or None,
                    mailbox_url_timeout=max(_int_config(extra.get("local_ms_mailbox_url_timeout"), 15), 1),
                    mailbox_url_poll_interval=max(_int_config(extra.get("local_ms_mailbox_url_poll_interval"), 2), 1),
                )
            elif not extra.get("mail_provider"):
                from infrastructure.provider_settings_repository import ProviderSettingsRepository
                extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
            if shared_mailbox is None:
                shared_mailbox = create_mailbox(
                    provider=extra.get("mail_provider", ""),
                    extra=extra,
                    proxy=registration_base_proxy or None,
                )
    except Exception as exc:
        logger.log(f"邮箱初始化失败: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"邮箱初始化失败: {exc}")
        return

    def _do_one(index: int) -> bool | str:
        if logger.is_cancel_requested():
            return "__cancel_requested__"
        # 占用一个 SMS 槽位（如果配了 sms_pool_slots）。每个并发线程独占
        # 一条号；跑完归还供下一批任务复用。slot_queue 大小 = concurrency，
        # 启动前已校验过；这里只在配了池时阻塞 acquire。
        sms_slot_id: int | None = None
        sms_slot_value: str = ""
        if sms_pool_slots:
            sms_slot_id = sms_slot_queue.get()
            sms_slot_value = sms_pool_slots[sms_slot_id]
            logger.log(
                f"任务 #{index + 1} 占用 SMS 槽 {sms_slot_id + 1}/{len(sms_pool_slots)}: "
                f"{sms_slot_value.split('----', 1)[0]}"
            )
        # 给当前线程绑定 subtask 标签——后续所有 ``logger.log`` 都自动带上
        # ``subtask_id``，前端按这个分组折叠展示。优先用 SMS 槽 ID 做稳定
        # subtask（同一号一直在同一组）；没号池就退化到注册序号。
        if sms_slot_id is not None:
            subtask_id = f"worker_{sms_slot_id + 1}"
            subtask_label = (
                f"Worker {sms_slot_id + 1} ({sms_slot_value.split('----', 1)[0]})"
            )
        else:
            subtask_id = f"task_{index + 1}"
            subtask_label = f"账号 #{index + 1}"
        logger.set_subtask(subtask_id, subtask_label)

        # 构造 swap callback：当 checkout 中途 PayPal 拒号时，从 extras 备份池里
        # 取一条新号继续；同时把当前线程的当前号"标坏"（即不再放回 slot_queue
        # 让下个任务用），并把新号作为当前线程后续可能再次被拒时的回退基础。
        # callback 返回 None 表示备份池空 → 当前线程任务失败、前端可识别为
        # "号码不可用"。
        slot_state = {
            "slot_value": sms_slot_value,
            "swapped_or_dead": False,  # 标记当前 slot 是死号，finally 不归还
        }

        def _swap_phone(rejected_e164: str) -> Optional[dict]:
            with sms_pool_lock:
                if not sms_pool_extras:
                    # 备份池空：把当前 slot 标为死号 + 全局通知"池耗尽"，
                    # 防止 finally 误把这条死号归还、调度层再投新任务又抢
                    # 到这条号继续被拒。
                    slot_state["swapped_or_dead"] = True
                    sms_pool_exhausted.set()
                    return None
                next_entry = sms_pool_extras.pop(0)
            phone_e164 = str(next_entry.get("phone_e164") or "").strip()
            relay_url = str(next_entry.get("relay_url") or "").strip()
            if not (phone_e164 and relay_url):
                slot_state["swapped_or_dead"] = True
                sms_pool_exhausted.set()
                return None
            new_value = f"{phone_e164}----{relay_url}"
            slot_state["slot_value"] = new_value
            slot_state["swapped_or_dead"] = True
            # 更新 subtask label，让前端分组里"号码"信息也跟着换
            label_idx = sms_slot_id + 1 if sms_slot_id is not None else index + 1
            logger.set_subtask(subtask_id, f"Worker {label_idx} ({phone_e164})")
            logger.log(
                f"任务 #{index + 1} 切换 SMS 号到备份池：{phone_e164}（剩余备份 {len(sms_pool_extras)} 条）"
            )
            return next_entry

        resolved_proxy = _resolve_registration_proxy_for_platform(
            platform_name,
            explicit_proxy=proxy,
            proxy_getter=lambda: proxy_pool.acquire_next(proxy_region),
            allow_pool=allow_proxy_pool,
        )
        leased_proxy_url = resolved_proxy
        proxy_pool_lease = bool(
            leased_proxy_url
            and not proxy
            and allow_proxy_pool
        )
        if proxy_strategy in {"polling", "sticky"} and not resolved_proxy:
            error = "代理池暂时没有可分配代理"
            logger.record_error(error)
            logger.log(f"✗ 注册失败: {error}", level="error")
            return error
        if platform_name == "chatgpt" and resolved_proxy:
            pinned_proxy = _pin_chatgpt_registration_proxy(
                resolved_proxy,
                region=proxy_region,
                session_id=f"reg{uuid.uuid4().hex[:8]}",
            )
            if pinned_proxy and pinned_proxy != resolved_proxy:
                resolved_proxy = pinned_proxy
                logger.log("711Proxy 注册路由已隔离并固定 180 分钟")
        # 短链物理复用（CtfGptPlus / PayPal）：注册和打开短链必须同一浏览器。
        # 把 post_register 回调 + backend_config 注入 config.extra，让注册器在
        # 注册完、浏览器还开着时，在同一 page 上生成短链 → 跑 PayPal checkout。
        _shortlink_reuse = (
            platform_name == "chatgpt" and _shortlink_payment_enabled(payload)
        )
        # Every worker owns an isolated payload.  Proxy preflight and browser
        # reuse inject attempt-specific values into ``extra``; sharing the task
        # dictionary allowed one worker's route/session data to overwrite
        # another worker while both were registering.
        _build_payload = copy.deepcopy(payload)
        _sl_acquired_profile = ""
        email_account_created = False
        if _shortlink_reuse:
            from platforms._browser_backend import parse_checkout_mode
            _pcfg = dict((_build_payload.get("extra") or {}).get("chatgpt_payment") or {})
            _ckmode = str(_pcfg.get("checkout_mode") or "camoufox_headed").strip().lower()
            if _ckmode == "protocol":
                _ckmode = "camoufox_headed"  # 短链复用必须用浏览器
            # BitBrowser 模式：从池里 acquire 一个 profile（跟正常 PayPal 流程
            # 一致），否则 backend_config 缺 profile_id 会直接报错。
            _sl_bit_profile = str(_pcfg.get("bit_profile_id") or "")
            if _ckmode.startswith("bitbrowser"):
                from application.bitbrowser_profiles import acquire_profile_for_browser_mode
                _sl_bit_profile, _sl_acquired_profile = acquire_profile_for_browser_mode(
                    _ckmode, fallback=_sl_bit_profile, log_fn=logger.log,
                )
                if not _sl_bit_profile:
                    logger.log(
                        "短链复用：BitBrowser 池为空且未配 bit_profile_id，"
                        "回退 Camoufox 前台",
                        level="error",
                    )
                    _ckmode = "camoufox_headed"
            _cb = _build_inbrowser_shortlink_checkout(
                payload=payload, logger=logger, proxy=resolved_proxy,
                sms_pool_override=slot_state["slot_value"] or sms_slot_value,
            )
            _reuse_extra = dict(_build_payload.get("extra") or {})
            _reuse_extra["_reuse_backend_config"] = parse_checkout_mode(
                _ckmode, bit_profile_id=_sl_bit_profile,
            )
            _reuse_extra["_post_register_in_browser"] = _cb
            _build_payload = dict(_build_payload)
            _build_payload["extra"] = _reuse_extra
            # **关键**：短链物理复用必须走浏览器注册（headed/headless），
            # 否则 base_platform.register 会走 ProtocolMailboxFlow（协议邮箱
            # 注册，根本不开浏览器，post_register_in_browser 回调永远不触发）。
            # 从付款 checkout_mode 推导注册 executor：headless 模式→headless，
            # 其余（含 bitbrowser_*/camoufox_headed）→headed。
            _reuse_executor = "headless" if _ckmode.endswith("_headless") else "headed"
            _build_payload["executor_type"] = _reuse_executor
            logger.log(
                f"短链物理复用：注册+打开短链+PayPal 付款将在同一浏览器"
                f"（注册执行器={_reuse_executor}, 浏览器={_ckmode}）里完成"
            )
        if platform_name == "chatgpt":
            try:
                from platforms.chatgpt.workspace_join import workspace_join_enabled

                if workspace_join_enabled(dict(_build_payload.get("extra") or {})):
                    logger.log("注意：Workspace Join 将在注册成功后作为独立任务执行")
            except Exception as exc:
                logger.log(f"Workspace Join 配置检查失败，继续原注册流程: {exc}", level="error")
        platform = None
        account = None
        saved_account_id = 0
        try:
            if (
                platform_name == "chatgpt"
                and str(_build_payload.get("executor_type") or "protocol") == "protocol"
                and resolved_proxy
            ):
                route_location = _preflight_chatgpt_registration_proxy(
                    resolved_proxy,
                    log_fn=logger.log,
                )
                logger.log(f"ChatGPT 协议代理预检通过: {route_location or 'unknown'}")
                route_extra = dict(_build_payload.get("extra") or {})
                route_extra["proxy_route_country"] = str(route_location or "").upper()
                _build_payload["extra"] = route_extra
            platform = _build_platform_instance(platform_name, _build_payload, logger, resolved_proxy=resolved_proxy, shared_mailbox=shared_mailbox)
            # 失败不计进度的模式（chatgpt_plus_must_succeed）下 index 可能 > count，
            # 显示成"已成功 X/N，本次为第 M 次尝试"更直观。
            if chatgpt_plus_must_succeed:
                logger.log(
                    f"开始注册账号（已成功 {success}/{count}，本次第 {index + 1} 次尝试）"
                )
            else:
                logger.log(
                    f"开始第 {index + 1} 次注册尝试（目标成功 {count} 个，当前成功 {success} 个）"
                )
            if resolved_proxy:
                if proxy_pool_lease:
                    proxy_label = proxy_pool.assignment_label(leased_proxy_url)
                    logger.log(
                        f"使用代理池条目 {proxy_label}: {mask_proxy_url(resolved_proxy)}"
                    )
                else:
                    logger.log(f"使用手动代理: {mask_proxy_url(resolved_proxy)}")
            account = platform.register(email=email, password=password)
            if resolved_proxy:
                account_extra = dict(getattr(account, "extra", {}) or {})
                account_extra.setdefault("auth_proxy_url", resolved_proxy)
                account.extra = account_extra
            email_account_created = True
            _registration_pipeline_update(account, "account_created", "passed")
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            require_phone_verification = (
                platform_name == "chatgpt"
                and _bool_config(extra.get("require_phone_verification"), False)
            )
            if require_phone_verification:
                _registration_pipeline_update(account, "phone_verified", "in_progress")
                try:
                    _complete_required_chatgpt_phone_verification(
                        platform=platform,
                        account=account,
                        extra=extra,
                        logger=logger,
                        country_offset=index,
                    )
                    _registration_pipeline_update(account, "phone_verified", "passed")
                except Exception as phone_exc:
                    account.status = AccountStatus.PENDING_VERIFICATION
                    _registration_pipeline_update(
                        account,
                        "phone_verified",
                        "failed",
                        error=str(phone_exc),
                    )
                    account_extra = dict(getattr(account, "extra", {}) or {})
                    overview = dict(account_extra.get("account_overview") or {})
                    overview["phone_binding"] = {
                        "status": "failed",
                        "provider": str(extra.get("sms_provider") or ""),
                        "error": str(phone_exc),
                        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                    account_extra["account_overview"] = overview
                    account.extra = account_extra
                    save_account(account)
                    raise
            else:
                _registration_pipeline_update(account, "phone_verified", "not_required")
            require_codex_rt = _bool_config(
                extra.get("require_codex_refresh_token"),
                _bool_config(extra.get("sub2api_auto_sync"), False)
                or _bool_config(extra.get("require_phone_verification"), False),
            )
            _registration_pipeline_update(account, "credentials_ready", "in_progress")
            try:
                _upgrade_protocol_codex_credentials(
                    platform_name=platform_name,
                    platform=platform,
                    account=account,
                    executor_type=str(_build_payload.get("executor_type") or "protocol"),
                    logger=logger,
                    require_success=require_codex_rt,
                )
            except Exception as codex_exc:
                if require_phone_verification or require_codex_rt:
                    account.status = AccountStatus.PENDING_VERIFICATION
                    _registration_pipeline_update(
                        account,
                        "credentials_ready",
                        "failed",
                        error=str(codex_exc),
                    )
                    account_extra = dict(getattr(account, "extra", {}) or {})
                    account_extra["codex_credential_status"] = {
                        "status": "failed",
                        "error": str(codex_exc)[:500],
                        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                    account.extra = account_extra
                    save_account(account)
                    raise
            if require_codex_rt and not _account_has_codex_rt(account):
                err = "CODEX_RT_MISSING: 注册流程结束时仍无 Codex refresh_token，拒绝记成功/上传 Sub2"
                account.status = AccountStatus.PENDING_VERIFICATION
                _registration_pipeline_update(
                    account,
                    "credentials_ready",
                    "failed",
                    error=err,
                )
                save_account(account)
                logger.record_error(err)
                _save_task_log(platform_name, account.email, "failed", error=err)
                return err
            _registration_pipeline_update(
                account,
                "credentials_ready",
                "passed" if _account_has_codex_rt(account) else "not_required",
            )
            _registration_pipeline_update(account, "liveness", "in_progress")
            liveness_error = _post_registration_chatgpt_liveness_error(
                platform_name=platform_name,
                platform=platform,
                account=account,
                extra=extra,
                logger=logger,
            )
            if liveness_error:
                _registration_pipeline_update(
                    account,
                    "liveness",
                    "failed",
                    error=liveness_error,
                )
                save_account(account)
                logger.record_error(liveness_error)
                _save_task_log(platform_name, account.email, "failed", error=liveness_error)
                return liveness_error
            account_overview = dict((getattr(account, "extra", {}) or {}).get("account_overview") or {})
            liveness_status = str(account_overview.get("validity_status") or "unknown").strip().lower()
            _registration_pipeline_update(
                account,
                "liveness",
                "passed" if liveness_status == "valid" else "unknown",
                detail={"validity_status": liveness_status or "unknown"},
            )
            _registration_pipeline_update(account, "persisted", "passed")
            saved_model = save_account(account)
            saved_account_id = int(getattr(saved_model, "id", 0) or 0)
            probation_enabled = (
                platform_name == "chatgpt"
                and _bool_config(extra.get("post_registration_probation_enabled"), True)
                and (
                    require_phone_verification
                    or str((getattr(account, "extra", {}) or {}).get("register_mode") or "").strip().lower()
                    in {"phone", "phone_with_email", "email_then_phone"}
                )
            )
            if probation_enabled and saved_account_id > 0:
                offsets = _post_registration_probation_offsets(extra)
                if offsets:
                    try:
                        from core.lifecycle import schedule_account_probation

                        interval_seconds = int(offsets[0])
                        probation = schedule_account_probation(
                            saved_account_id,
                            interval_seconds=interval_seconds,
                        )
                        logger.log(
                            "已安排持续存活复检: "
                            f"每 {interval_seconds} 秒检查一次，明确失效后停止高频监控"
                        )
                        _registration_pipeline_update(
                            account,
                            "probation",
                            "monitoring",
                            detail={
                                "mode": "continuous",
                                "interval_seconds": interval_seconds,
                                "next_check_at": probation.get("next_check_at", ""),
                            },
                        )
                        save_account(account)
                    except Exception as probation_exc:
                        logger.log(
                            f"持续存活复检排程失败，账号已保留: {probation_exc}",
                            level="warning",
                        )
            workspace_join_error = _chatgpt_workspace_join_failure(account)
            if workspace_join_error:
                _registration_pipeline_update(
                    account,
                    "workspace_join",
                    "failed",
                    error=workspace_join_error,
                )
                save_account(account)
                logger.record_error(workspace_join_error)
                logger.log(workspace_join_error, level="error")
                _save_task_log(platform_name, account.email, "failed", error=workspace_join_error)
                return workspace_join_error
            _mark_outlook_mailbox_event(shared_mailbox, account, "registration_success", logger)
            _mark_protocol_mailbox_success(shared_mailbox, platform, logger)
            _auto_followup_windsurf_payment(
                platform_name=platform_name,
                payload=payload,
                platform=platform,
                account=account,
                logger=logger,
            )
            chatgpt_plus_enabled = (
                platform_name == "chatgpt"
                and _bool_config(extra.get("auto_chatgpt_plus_payment"), False)
            )
            _registration_pipeline_update(
                account,
                "payment",
                "in_progress" if chatgpt_plus_enabled else "not_required",
            )
            if _shortlink_reuse:
                # 短链复用：PayPal checkout 已在注册浏览器里跑完，结果挂在
                # account.extra["_shortlink_checkout"]（由注册器回调合并进
                # registration_state/result，再由 _map_chatgpt_result 透传）。
                # 这里直接判定，不再调 _auto_followup（那会另开浏览器）。
                _sl_res = {}
                try:
                    _sl_res = dict((getattr(account, "extra", {}) or {}).get("_shortlink_checkout") or {})
                except Exception:
                    _sl_res = {}
                if _sl_res and not _sl_res.get("ok"):
                    chatgpt_plus_error = f"短链复用 PayPal 付款失败: {_sl_res.get('error') or _sl_res.get('status') or 'unknown'}"
                    _registration_pipeline_update(
                        account,
                        "payment",
                        "failed",
                        error=chatgpt_plus_error,
                    )
                    save_account(account)
                    logger.record_error(chatgpt_plus_error)
                    logger.log(chatgpt_plus_error, level="error")
                    _save_task_log(platform_name, account.email, "failed", error=chatgpt_plus_error)
                    return chatgpt_plus_error
                logger.log("短链复用 PayPal 付款完成（同一浏览器）")
                chatgpt_plus_error = ""
            else:
                chatgpt_plus_error = _auto_followup_chatgpt_plus_payment(
                    platform_name=platform_name,
                    payload=payload,
                    platform=platform,
                    account=account,
                    logger=logger,
                    sms_pool_override=slot_state["slot_value"] or sms_slot_value,
                    phone_swap_callback=_swap_phone if sms_pool_slots else None,
                )
            if chatgpt_plus_error:
                _registration_pipeline_update(
                    account,
                    "payment",
                    "failed",
                    error=chatgpt_plus_error,
                )
                save_account(account)
                logger.record_error(chatgpt_plus_error)
                logger.log(chatgpt_plus_error, level="error")
                _save_task_log(platform_name, account.email, "failed", error=chatgpt_plus_error)
                # SMS 号池耗尽错误（payment.py 抛 SMS_POOL_EXHAUSTED:）→
                # 整个任务级别停止投新任务（兜底，正常路径已经在 _swap_phone
                # 里 set 过；这里覆盖那种 payment 内部直接 raise 没经 callback
                # 的边角情况）。
                if _is_global_sms_pool_exhausted_error(chatgpt_plus_error):
                    sms_pool_exhausted.set()
                elif _is_current_sms_phone_exhausted_error(chatgpt_plus_error):
                    slot_state["swapped_or_dead"] = True
                return chatgpt_plus_error
            if chatgpt_plus_enabled:
                _registration_pipeline_update(account, "payment", "passed")
                _mark_outlook_mailbox_event(shared_mailbox, account, "plus_success", logger)
            if resolved_proxy:
                # 711Proxy workers receive a per-attempt pinned session URL,
                # but pool health belongs to the original leased row. Reporting
                # the rewritten URL cannot match the stored proxy record.
                proxy_pool.report_success(leased_proxy_url or resolved_proxy)
            logger.record_success()
            logger.log(f"✓ 注册成功: {account.email}")
            _save_task_log(platform_name, account.email, "success")
            if saved_account_id > 0:
                with successful_account_ids_lock:
                    successful_account_ids.append(saved_account_id)
            _auto_upload_cpa(logger, account)
            _auto_push_any2api(logger, account)
            sub2_result = _auto_push_sub2api(logger, account, options=extra)
            delivery_status = (
                "not_configured"
                if sub2_result is None
                else "delivered"
                if sub2_result
                else "pending"
            )
            _registration_pipeline_update(
                account,
                "delivery",
                delivery_status,
                error=("Sub2API 自动交付未完成，等待补传" if sub2_result is False else ""),
                detail={"sub2api": delivery_status},
            )
            save_account(account)
            if delivery_status == "pending" and saved_account_id > 0:
                with delivery_pending_account_ids_lock:
                    delivery_pending_account_ids.append(saved_account_id)
            account_extra = dict(account.extra or {})
            overview = dict(account_extra.get("account_overview") or {})
            cashier_url = str(account_extra.get("cashier_url") or overview.get("cashier_url") or "")
            if cashier_url:
                logger.log(f"  [升级链接] {cashier_url}")
                logger.add_cashier_url(cashier_url)
            return True
        except Exception as exc:
            error_code = str(getattr(exc, "code", "") or "").strip()
            error_text = str(exc)
            email_identity_rejected = (
                error_code in {"email_account_deactivated", "user_already_exists"}
                or "account already exists for this email" in error_text.lower()
            )
            mailbox_worker = getattr(platform, "_last_protocol_mailbox_worker", None)
            mailbox_account = getattr(mailbox_worker, "mailbox_account", None)
            if mailbox_account is not None and not email_account_created:
                failure_mailbox = _resolve_mailbox_for_method(
                    shared_mailbox,
                    mailbox_account,
                    "mark_registration_failure" if email_identity_rejected else "mark_attempt_failure",
                )
                marker = getattr(
                    failure_mailbox,
                    "mark_registration_failure" if email_identity_rejected else "mark_attempt_failure",
                    None,
                )
                if callable(marker):
                    try:
                        marker(mailbox_account, str(exc))
                        if email_identity_rejected:
                            logger.log(
                                f"邮箱身份已被远端拒绝，已淘汰当前子地址并切换下一条: {mailbox_account.email}",
                                level="warning",
                            )
                    except Exception as mark_exc:
                        logger.log(f"邮箱失败状态写入失败: {mark_exc}", level="warning")
            # Phone-bind failures after email creation used to halt the whole task.
            # Keep going with a fresh mailbox when the failure is number/session
            # related so we can rotate Indonesia (or other) numbers until success.
            if email_account_created and _bool_config(extra.get("require_phone_verification"), False):
                err_text = str(exc or "")
                account_deactivated = (
                    "account_deactivated" in err_text.lower()
                    or "deleted or deactivated" in err_text.lower()
                    or "you do not have an account" in err_text.lower()
                )
                if account_deactivated:
                    logger.log(
                        "当前邮箱账号已被目标服务停用，已释放号码并停止该账号流程；"
                        "不会在当前账号上继续换号，仍可由下一邮箱补足目标数量",
                        level="error",
                    )
                retryable_phone = any(
                    marker in err_text
                    for marker in (
                        "PHONE_ACCOUNT_RATE_LIMITED",
                        "phone_number_rejected",
                        "fraud_guard",
                        "rate_limit_exceeded",
                        "too many phone",
                        "等待短信验证码超时",
                        "接码",
                        "SMSBower",
                        "HeroSMS",
                        "手机号",
                        "add-phone",
                        "add_phone",
                    )
                ) and not account_deactivated
                if retryable_phone:
                    logger.log(
                        "邮箱已建号但手机验证未通过，释放号码后换下一邮箱继续: "
                        f"{err_text[:180]}",
                        level="warning",
                    )
                else:
                    logger.log(
                        "当前邮箱账号已完成建号，但后续步骤失败；仅结束本次 attempt，"
                        "调度器会按目标成功数决定是否使用下一邮箱补位",
                        level="warning",
                    )
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            if resolved_proxy and _registration_error_counts_as_proxy_failure(exc):
                proxy_pool.report_fail(leased_proxy_url or resolved_proxy)
            error = str(exc)
            if account is not None and email_account_created:
                try:
                    account_extra = dict(getattr(account, "extra", {}) or {})
                    account_overview = dict(account_extra.get("account_overview") or {})
                    current_stage = str(
                        (account_overview.get("registration_pipeline") or {}).get("current_stage")
                        or "registration"
                    )
                    _registration_pipeline_update(
                        account,
                        current_stage,
                        "failed",
                        error=error,
                    )
                    save_account(account)
                except Exception as pipeline_exc:
                    logger.log(
                        f"注册失败状态保存异常: {pipeline_exc}",
                        level="warning",
                    )
            logger.record_error(error)
            logger.log(f"✗ 注册失败: {error}", level="error")
            failure_email = str(
                getattr(account, "email", "")
                or getattr(mailbox_account, "email", "")
                or email
                or ""
            )
            _save_task_log(platform_name, failure_email, "failed", error=error)
            return error
        finally:
            # 归还 SMS 槽位：``swapped_or_dead`` 为 True 表示原号在跑过程中被
            # PayPal 拒了（不论备份池有没有补到新号），原号永久标坏，**不能**
            # 再放回 slot_queue 让下一个任务复用——否则下一个任务又抢到死号
            # 继续被拒。备份池还有就用备份号补位 slot；备份池也空就丢弃 slot。
            # 没换号（``swapped_or_dead`` False）→ 原号没被拒，正常归还。
            if sms_slot_id is not None:
                with sms_pool_lock:
                    if not slot_state["swapped_or_dead"]:
                        sms_slot_queue.put(sms_slot_id)
                    elif sms_pool_extras:
                        next_entry = sms_pool_extras.pop(0)
                        phone_e164 = str(next_entry.get("phone_e164") or "").strip()
                        relay_url = str(next_entry.get("relay_url") or "").strip()
                        if phone_e164 and relay_url:
                            sms_pool_slots[sms_slot_id] = (
                                f"{phone_e164}----{relay_url}"
                            )
                            sms_slot_queue.put(sms_slot_id)
                            logger.log(
                                f"SMS 槽 {sms_slot_id + 1} 用过备份号补位为 "
                                f"{phone_e164}（剩余备份 {len(sms_pool_extras)} 条）"
                            )
                        else:
                            sms_pool_exhausted.set()
                    else:
                        sms_pool_exhausted.set()
            if proxy_pool_lease:
                proxy_pool.release(leased_proxy_url)
            # 解除 thread-local subtask 绑定，避免 ThreadPool 复用线程时
            # 把上一个任务的标签泄露到下一个任务。
            logger.clear_subtask()
            # 短链复用从 BitBrowser 池 acquire 的 profile，跑完归还计数。
            if _sl_acquired_profile:
                try:
                    from application.bitbrowser_profiles import release_acquired_profile
                    release_acquired_profile(_sl_acquired_profile, log_fn=logger.log)
                except Exception:
                    pass

    try:
        submitted = 0
        completed = 0
        futures: dict[Any, int] = {}
        # ChatGPT Plus 自动支付链接场景：用户诉求"设置生成 N 个必须生成 N 个
        # 成功"——失败的账号进入 gpt 账户池但**不增加进度**，调度继续投新任务
        # 直到 success 达到 count。最多投 ``count * 5`` 次防止号池烂掉时无限
        # 循环。其它平台 / 不开自动支付 → 退化为原"投 count 次就停"语义。
        chatgpt_plus_must_succeed = (
            platform_name == "chatgpt"
            and _bool_config(extra.get("auto_chatgpt_plus_payment"), False)
        )
        inline_mailbox_no_reuse = bool(inline_mailbox_pool_text and not inline_mailbox_allow_reuse)
        is_chatgpt_phone_registration = (
            platform_name == "chatgpt"
            and (
                str(extra.get("identity_provider") or "").strip().lower() in {"phone", "sms", "mobile"}
                or str(extra.get("register_mode") or "").strip().lower() == "phone"
            )
        )
        if is_chatgpt_phone_registration:
            # Phone-first registration already rotates numbers inside one account
            # transaction, so task-level retries stay disabled.
            max_attempts = count
        elif is_chatgpt_email_then_phone:
            email_identity_attempts = min(
                max(_int_config(extra.get("email_pre_phone_max_attempts"), 5), 1),
                12,
            )
            max_attempts = count * email_identity_attempts
            if inline_mailbox_no_reuse:
                max_attempts = min(max_attempts, inline_mailbox_pool_size)
            logger.log(
                "邮箱 + 手机流程: "
                f"目标成功 {count} 个，每个目标最多 {email_identity_attempts} 次"
                f"（邮箱建号后手机失败会释放号码并换邮箱继续；并发窗口={concurrency}）"
            )
        elif inline_mailbox_no_reuse:
            retry_multiplier = _register_retry_multiplier(extra)
            max_attempts = min(
                inline_mailbox_pool_size,
                count * retry_multiplier,
            ) if failure_policy in {"retry_then_continue", "retry", "failed_retry"} else min(count, inline_mailbox_pool_size)
            logger.log(
                "API mailbox pool is non-reusable; failed accounts will use the next mailbox "
                f"without reusing consumed rows (max_attempts={max_attempts})."
            )
        elif chatgpt_plus_must_succeed:
            max_attempts = max(count * 5, count, 1)
        else:
            retry_register_failures = failure_policy in {"retry_then_continue", "retry", "failed_retry"}
            retry_multiplier = _register_retry_multiplier(extra)
            max_attempts = max(
                (count * retry_multiplier if retry_register_failures else count)
                if not herosms_enabled else max_success * 3,
                1,
            )

        if complete_started_attempts:
            # ``count`` 表示目标成功数，不再代表已经启动的尝试数。
            # 当用户选择并发 5、目标 1 时，至少允许 5 个 worker 被提交，
            # 但仍受邮箱池、号码池和上面的安全并发上限约束。
            max_attempts = max(max_attempts, concurrency)

        def _hero_phone_alive() -> bool:
            if not (herosms_enabled and hero_reuse_to_max):
                return False
            try:
                from core.base_sms import is_herosms_phone_cache_alive
                alive, info = is_herosms_phone_cache_alive(sms_settings)
                if alive:
                    logger.log(
                        "HeroSMS 号码仍可复用: "
                        f"{str(info.get('phone_number') or '')[:5]}**** "
                        f"剩余 {int(info.get('remaining_seconds') or 0)} 秒，"
                        f"已成功 {int(info.get('use_count') or 0)} 次"
                    )
                return bool(alive)
            except Exception:
                return False

        def _should_submit_more() -> bool:
            if submitted >= max_attempts or logger.is_cancel_requested():
                return False
            if network_circuit_open:
                return False
            if failure_policy in {"stop", "stop_on_failure", "fail_fast"} and errors:
                return False
            # SMS 号池被耗尽（某条号被拒 + 备份池空）→ 整个任务级别停止
            # 投新任务，让正在跑的任务跑完后退出。否则下一批又抢同一条死号
            # 继续被拒（用户实战日志 "开始注册第 2/1 个账号" 即此场景）。
            if sms_pool_exhausted.is_set():
                return False
            # 如果配了 sms_pool_slots，slot_queue 实际可用 + 在跑数 < 待补的
            # success 缺口才能再投。slot 全死光了（chatgpt_plus_must_succeed
            # 模式下号码池+备份池全被 PayPal 拒）就不再投，避免 _do_one 的
            # ``sms_slot_queue.get()`` 永久阻塞。
            if sms_pool_slots:
                # qsize 是近似的（多线程下不严格），但作为"全死光"判定够用
                if sms_slot_queue.qsize() == 0 and len(futures) >= concurrency:
                    return False
            # 明确启用“完成已启动流程”时，先把并发窗口填满。
            # 这一步必须放在目标成功判断之前，否则 count=1、concurrency=5
            # 会在第一个 future 提交后提前停止，剩余流程永远不会进入接码。
            if (
                complete_started_attempts
                and success < count
                and submitted < min(concurrency, max_attempts)
            ):
                return True
            if chatgpt_plus_must_succeed:
                # 必须达到 count 个 success；失败不计 progress，继续投。
                # 已成功 + 在跑的 ≥ count 时不再投（避免超额）。
                return success + len(futures) < count
            if is_chatgpt_email_then_phone:
                # Normal target semantics reserve only the missing number of
                # successes: target=5/concurrency=5 starts five workers, keeps
                # all five alive through SMS, and only replaces a worker when
                # it fails.  Speculative "fill the whole window" behaviour is
                # still available through complete_started_attempts.
                if complete_started_attempts:
                    return success < count and submitted < max_attempts
                return success + len(futures) < count and submitted < max_attempts
            if not herosms_enabled:
                if inline_mailbox_no_reuse:
                    if failure_policy in {"retry_then_continue", "retry", "failed_retry"}:
                        return success + len(futures) < count and submitted < max_attempts
                    return submitted < count
                if failure_policy in {"retry_then_continue", "retry", "failed_retry"}:
                    return success + len(futures) < count and submitted < max_attempts
                return submitted < count
            if success + len(futures) >= max_success:
                return False
            if success < target_success:
                return True
            if success >= max_success:
                return False
            return _hero_phone_alive()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while _should_submit_more() and len(futures) < concurrency:
                futures[pool.submit(_do_one, submitted)] = submitted
                submitted += 1

            while futures:
                done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future, None)
                    result = future.result()
                    completed += 1
                    if result is True:
                        success += 1
                        network_failure_streak = 0
                    elif result != "__cancel_requested__":
                        result_error = str(result)
                        errors.append(result_error)
                        failure_code, _failure_label = _registration_failure_category(result_error)
                        if (
                            platform_name == "chatgpt"
                            and network_circuit_break_threshold > 0
                            and failure_code == "proxy_network"
                        ):
                            network_failure_streak += 1
                            if (
                                network_failure_streak >= network_circuit_break_threshold
                                and not network_circuit_open
                            ):
                                network_circuit_open = True
                                network_circuit_reason = result_error[:500]
                                logger.log(
                                    "注册网络熔断已触发: "
                                    f"连续 {network_failure_streak} 次代理/网络失败，"
                                    "停止投放新账号；已在运行的 worker 将自然收尾",
                                    level="error",
                                )
                        else:
                            network_failure_streak = 0
                    logger.set_progress(
                        min(
                            success
                            if (
                                herosms_enabled
                                or chatgpt_plus_must_succeed
                                or failure_policy in {"retry_then_continue", "retry", "failed_retry"}
                            )
                            else completed,
                            progress_total,
                        ),
                        progress_total,
                    )
                while _should_submit_more() and len(futures) < concurrency:
                    futures[pool.submit(_do_one, submitted)] = submitted
                    submitted += 1
                if logger.is_cancel_requested() and not futures:
                    break
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    result_data: dict[str, Any] = {
        "account_ids": list(dict.fromkeys(successful_account_ids)),
        "delivery_pending_account_ids": list(dict.fromkeys(delivery_pending_account_ids)),
        "failure_summary": _registration_failure_summary(errors),
        "network_circuit_breaker": {
            "enabled": platform_name == "chatgpt" and network_circuit_break_threshold > 0,
            "threshold": network_circuit_break_threshold,
            "open": network_circuit_open,
            "consecutive_failures": network_failure_streak,
            "reason": network_circuit_reason,
        },
    }
    if herosms_enabled:
        result_data.update({
            "target_count": target_success,
            "attempts": submitted,
            "success": success,
            "fail": len(errors),
            "extra_success": max(0, success - target_success),
            "hero_sms_reuse": hero_reuse_to_max,
        })
    logger.set_result_data(result_data)
    outcome = _register_task_outcome(
        target_count=target_success,
        success_count=success,
        submitted_attempts=submitted,
        attempt_errors=errors,
    )
    logger.log(
        outcome["summary"],
        event_type="summary",
        detail={
            "target_count": outcome["target_count"],
            "success_count": outcome["success_count"],
            "failure_count": outcome["failure_count"],
            "attempt_count": outcome["attempt_count"],
            "attempt_failure_count": outcome["attempt_failure_count"],
        },
    )
    if result_data["failure_summary"]:
        logger.log(
            "失败分类: "
            + "；".join(
                f"{item['label']} {item['count']} 次"
                for item in result_data["failure_summary"]
            ),
            event_type="summary",
            detail={"failure_summary": result_data["failure_summary"]},
        )
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    logger.finish(outcome["status"], error=outcome["final_error"])


def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    command_platform = str(payload.get("platform", ""))
    account_id = int(payload.get("account_id", 0) or 0)
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    runtime = PlatformRuntime()
    result = runtime.execute_action(
        type("Command", (), {
            "platform": command_platform,
            "account_id": account_id,
            "action_id": action_id,
            "params": params,
        })(),
        log_fn=logger.log,
        cancel_check=logger.is_cancel_requested,
    )
    if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    if not result.ok:
        logger.record_error(result.error)
        logger.finish(TASK_STATUS_FAILED, error=result.error)
        return
    logger.set_result_data(result.data)
    message = ""
    if isinstance(result.data, dict):
        message = str(result.data.get("message", "") or "")
    if message:
        logger.log(message, event_type="summary")
    logger.set_progress(1, 1)
    logger.finish(TASK_STATUS_SUCCEEDED)


def _execute_phone_bind_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    fallback_ids = [int(item) for item in payload.get("fallback_ids") or [] if int(item or 0) > 0]
    total = len(ids) if ids else max(len(fallback_ids), 1)
    logger.set_progress(0, total)
    logger.log(
        f"开始绑定手机号：目标账号 {total} 个，浏览器模式 {payload.get('browser_mode') or 'camoufox_headed'}"
    )
    try:
        result = PhoneBindingService().bind(
            platform=str(payload.get("platform") or "chatgpt"),
            ids=ids,
            fallback_ids=fallback_ids,
            phone_lines=str(payload.get("phone_lines") or ""),
            browser_mode=str(payload.get("browser_mode") or "camoufox_headed"),
            bit_profile_id=str(payload.get("bit_profile_id") or ""),
            concurrency=max(int(payload.get("concurrency") or 1), 1),
            log_fn=logger.log,
        )
    except ValueError as exc:
        logger.record_error(str(exc))
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return
    except Exception as exc:
        logger.record_error(str(exc))
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    for _ in range(int(result.get("success_count") or 0)):
        logger.record_success()
    for item in result.get("results") or []:
        if item.get("ok"):
            logger.log(f"✓ 绑定成功: {item.get('email')} -> {item.get('phone')}")
        else:
            error = str(item.get("error") or "unknown error")
            logger.record_error(error)
            logger.log(f"✗ 绑定失败: {item.get('email')} -> {error}", level="error")
    logger.set_result_data(result)
    done = int(result.get("total") or total)
    logger.set_progress(done, done)
    final_status = TASK_STATUS_SUCCEEDED if int(result.get("failure_count") or 0) == 0 else TASK_STATUS_FAILED
    logger.finish(final_status)


def _execute_codex_oauth_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    if not ids:
        account_id = int(payload.get("account_id") or 0)
        if account_id > 0:
            ids = [account_id]
    if not ids:
        logger.finish(TASK_STATUS_FAILED, error="缺少 account_id")
        return
    total = len(ids)
    concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)
    requested_browser_mode = str(payload.get("browser_mode") or "camoufox_headed")
    browser_mode = resolve_runtime_browser_mode(requested_browser_mode)
    if browser_mode != requested_browser_mode.strip().lower():
        logger.log("当前云端环境未检测到 DISPLAY，Camoufox 已自动切换为后台模式")
    bit_profile_id = str(payload.get("bit_profile_id") or "")
    logger.set_progress(0, total)
    logger.log(f"开始 Codex OAuth：账号 {total} 个，并发 {concurrency}，浏览器模式 {browser_mode}")

    results: list[dict[str, Any] | None] = [None] * total
    completed = 0

    def run_one(index: int, account_id: int) -> dict[str, Any]:
        logger.set_subtask(f"worker_{index + 1}", f"账号 {account_id}")
        try:
            if logger.is_cancel_requested():
                return {"ok": False, "account_id": account_id, "error": "任务已取消"}
            logger.log(f"[{index + 1}/{total}] 开始 Codex OAuth: {account_id}")
            result = CtfPlusAccountsService().run_codex_oauth_browser(
                account_id=account_id,
                browser_mode=browser_mode,
                bit_profile_id=bit_profile_id,
                log_fn=logger.log,
            )
            logger.log(f"[{index + 1}/{total}] Codex OAuth 成功: {result.get('email') or account_id}")
            return {"ok": True, **(result or {}), "account_id": account_id}
        except Exception as exc:
            error = str(exc)
            logger.log(f"[{index + 1}/{total}] Codex OAuth 失败 {account_id}: {error}", level="error")
            return {"ok": False, "account_id": account_id, "error": error}
        finally:
            logger.clear_subtask()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_map = {}
        next_index = 0
        while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
            future = pool.submit(run_one, next_index, ids[next_index])
            future_map[future] = next_index
            next_index += 1

        while future_map:
            done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                index = future_map.pop(future)
                try:
                    item = future.result()
                except Exception as exc:
                    item = {"ok": False, "account_id": ids[index], "error": str(exc)}
                results[index] = item
                if item.get("ok"):
                    logger.record_success()
                else:
                    logger.record_error(str(item.get("error") or "unknown error"))
                completed += 1
                logger.set_progress(completed, total)

            while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
                future = pool.submit(run_one, next_index, ids[next_index])
                future_map[future] = next_index
                next_index += 1

    final_results = [item for item in results if item is not None]
    success_count = sum(1 for item in final_results if item.get("ok"))
    failure_count = len(final_results) - success_count
    result_data = {
        "total": total,
        "success_count": success_count,
        "failure_count": failure_count,
        "results": final_results,
        "concurrency": concurrency,
    }
    logger.set_result_data(result_data)
    if logger.is_cancel_requested() and len(final_results) < total:
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = TASK_STATUS_SUCCEEDED if failure_count == 0 else TASK_STATUS_FAILED
    logger.finish(final_status)


def _execute_get_rt_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """批量获取 refresh_token（跳过手机验证）。"""
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    if not ids:
        account_id = int(payload.get("account_id") or 0)
        if account_id > 0:
            ids = [account_id]
    if not ids:
        logger.finish(TASK_STATUS_FAILED, error="缺少 account_id")
        return
    total = len(ids)
    concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)
    requested_browser_mode = str(payload.get("browser_mode") or "camoufox_headed")
    browser_mode = resolve_runtime_browser_mode(requested_browser_mode)
    if browser_mode != requested_browser_mode.strip().lower():
        logger.log("当前云端环境未检测到 DISPLAY，Camoufox 已自动切换为后台模式")
    logger.set_progress(0, total)
    logger.log(f"开始获取rt：账号 {total} 个，并发 {concurrency}，浏览器模式 {browser_mode}")

    from infrastructure.platform_runtime import PlatformRuntime
    from core.db import engine, AccountModel
    from sqlmodel import Session

    sms_provider = str(payload.get("sms_provider") or "").strip().lower()
    try:
        phone_reuse_count = max(int(payload.get("phone_reuse_count") or 3), 3)
    except Exception:
        phone_reuse_count = 3
    phone_reuse_pool = None
    if sms_provider in {"smspool", "smsapi"}:
        try:
            from platforms.chatgpt.browser_get_rt import build_get_rt_phone_reuse_pool

            phone_reuse_pool, phone_pool_error = build_get_rt_phone_reuse_pool(
                sms_provider=sms_provider,
                smspool_api_key=str(payload.get("smspool_api_key") or ""),
                smspool_max_price=str(payload.get("smspool_max_price") or "0.13"),
                smsapi_phone=str(payload.get("smsapi_phone") or ""),
                smsapi_url=str(payload.get("smsapi_url") or ""),
                reuse_count=phone_reuse_count,
                log_fn=logger.log,
            )
            if phone_reuse_pool:
                logger.log(
                    f"获取rt: 启用任务级手机号复用 provider={sms_provider}, "
                    f"每号成功 {phone_reuse_count} 次后换号"
                )
            else:
                logger.log(f"获取rt: 手机号复用池创建失败: {phone_pool_error}", level="error")
        except Exception as exc:
            logger.log(f"获取rt: 手机号复用池初始化异常: {exc}", level="error")
            phone_reuse_pool = None
    elif sms_provider:
        logger.log(f"获取rt: 未知手机号 provider={sms_provider}，将按原流程继续", level="error")

    results: list[dict[str, Any] | None] = [None] * total
    completed = 0

    def run_one(index: int, account_id: int) -> dict[str, Any]:
        logger.set_subtask(f"get_rt_{index + 1}", f"账号 {account_id}")
        try:
            if logger.is_cancel_requested():
                return {"ok": False, "account_id": account_id, "error": "任务已取消"}
            logger.log(f"[{index + 1}/{total}] 获取rt: 账号 #{account_id}")
            runtime = PlatformRuntime()
            command_params = {
                "browser_mode": browser_mode,
                "record_har": str(payload.get("record_har") or "").strip().lower(),
                "sms_provider": sms_provider,
                "smspool_api_key": str(payload.get("smspool_api_key") or ""),
                "smspool_max_price": str(payload.get("smspool_max_price") or "0.13"),
                "smsapi_phone": str(payload.get("smsapi_phone") or ""),
                "smsapi_url": str(payload.get("smsapi_url") or ""),
                "phone_reuse_count": str(phone_reuse_count),
            }
            if phone_reuse_pool:
                command_params["phone_callback"] = phone_reuse_pool.make_callback(
                    label=f"{index + 1}/{total}"
                )
            result = runtime.execute_action(
                type("Command", (), {
                    "platform": "chatgpt",
                    "account_id": account_id,
                    "action_id": "get_rt",
                    "params": command_params,
                })(),
                log_fn=logger.log,
                cancel_check=logger.is_cancel_requested,
            )
            if result.ok:
                logger.log(f"[{index + 1}/{total}] 获取rt成功: 账号 #{account_id}")
                return {"ok": True, "account_id": account_id, "data": result.data}
            else:
                error = str(result.error or "unknown error")
                logger.log(f"[{index + 1}/{total}] 获取rt失败 #{account_id}: {error}", level="error")
                return {"ok": False, "account_id": account_id, "error": error}
        except Exception as exc:
            error = str(exc)
            logger.log(f"[{index + 1}/{total}] 获取rt异常 #{account_id}: {error}", level="error")
            return {"ok": False, "account_id": account_id, "error": error}
        finally:
            logger.clear_subtask()

    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            future_map = {}
            next_index = 0
            while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
                future = pool.submit(run_one, next_index, ids[next_index])
                future_map[future] = next_index
                next_index += 1

            while future_map:
                done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    index = future_map.pop(future)
                    try:
                        item = future.result()
                    except Exception as exc:
                        item = {"ok": False, "account_id": ids[index], "error": str(exc)}
                    results[index] = item
                    if item.get("ok"):
                        logger.record_success()
                    else:
                        logger.record_error(str(item.get("error") or "unknown error"))
                    completed += 1
                    logger.set_progress(completed, total)

                while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
                    future = pool.submit(run_one, next_index, ids[next_index])
                    future_map[future] = next_index
                    next_index += 1
    finally:
        if phone_reuse_pool:
            try:
                phone_reuse_pool.cleanup()
            except Exception as exc:
                logger.log(f"获取rt: 手机号复用池清理异常: {exc}", level="error")

    final_results = [item for item in results if item is not None]
    success_count = sum(1 for item in final_results if item.get("ok"))
    failure_count = len(final_results) - success_count
    result_data = {
        "total": total,
        "success_count": success_count,
        "failure_count": failure_count,
        "results": final_results,
    }
    logger.set_result_data(result_data)
    if logger.is_cancel_requested() and len(final_results) < total:
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = TASK_STATUS_SUCCEEDED if failure_count == 0 else TASK_STATUS_FAILED
    logger.finish(final_status)


def _execute_get_rt_bypass_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """批量获取 refresh_token（绕过手机号，session/select 拦截）。"""
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    ids = [int(item) for item in payload.get("ids") or [] if int(item or 0) > 0]
    if not ids:
        logger.finish(TASK_STATUS_FAILED, error="缺少 account_id")
        return
    total = len(ids)
    concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)
    requested_browser_mode = str(payload.get("browser_mode") or "camoufox_headed")
    browser_mode = resolve_runtime_browser_mode(requested_browser_mode)
    if browser_mode != requested_browser_mode.strip().lower():
        logger.log("当前云端环境未检测到 DISPLAY，Camoufox 已自动切换为后台模式")
    logger.set_progress(0, total)
    logger.log(f"开始获取rt(绕过)：账号 {total} 个，并发 {concurrency}，{browser_mode}")

    from infrastructure.platform_runtime import PlatformRuntime
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    results: list[dict[str, Any] | None] = [None] * total
    completed = 0

    def run_one(index: int, account_id: int) -> dict[str, Any]:
        logger.set_subtask(f"get_rt_bypass_{index + 1}", f"账号 {account_id}")
        try:
            if logger.is_cancel_requested():
                return {"ok": False, "account_id": account_id, "error": "任务已取消"}
            logger.log(f"[{index + 1}/{total}] 获取rt(绕过): 账号 #{account_id}")
            runtime = PlatformRuntime()
            result = runtime.execute_action(
                type("Command", (), {
                    "platform": "chatgpt",
                    "account_id": account_id,
                    "action_id": "get_rt_bypass",
                    "params": {"browser_mode": browser_mode},
                })(),
                log_fn=logger.log,
                cancel_check=logger.is_cancel_requested,
            )
            if result.ok:
                logger.log(f"[{index + 1}/{total}] 获取rt(绕过)成功: 账号 #{account_id}")
                return {"ok": True, "account_id": account_id, "data": result.data}
            else:
                error = str(result.error or "unknown error")
                logger.log(f"[{index + 1}/{total}] 获取rt(绕过)失败 #{account_id}: {error}", level="error")
                return {"ok": False, "account_id": account_id, "error": error}
        except Exception as exc:
            logger.log(f"[{index + 1}/{total}] 获取rt(绕过)异常 #{account_id}: {exc}", level="error")
            return {"ok": False, "account_id": account_id, "error": str(exc)}
        finally:
            logger.clear_subtask()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_map = {}
        next_index = 0
        while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
            future = pool.submit(run_one, next_index, ids[next_index])
            future_map[future] = next_index
            next_index += 1
        while future_map:
            done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)
            for future in done:
                index = future_map.pop(future)
                try:
                    item = future.result()
                except Exception as exc:
                    item = {"ok": False, "account_id": ids[index], "error": str(exc)}
                results[index] = item
                if item.get("ok"):
                    logger.record_success()
                else:
                    logger.record_error(str(item.get("error") or "unknown error"))
                completed += 1
                logger.set_progress(completed, total)
            while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
                future = pool.submit(run_one, next_index, ids[next_index])
                future_map[future] = next_index
                next_index += 1

    final_results = [item for item in results if item is not None]
    success_count = sum(1 for item in final_results if item.get("ok"))
    failure_count = len(final_results) - success_count
    logger.set_result_data({"total": total, "success_count": success_count, "failure_count": failure_count, "results": final_results})
    if logger.is_cancel_requested() and len(final_results) < total:
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    logger.finish(TASK_STATUS_SUCCEEDED if failure_count == 0 else TASK_STATUS_FAILED)


def _execute_gopay_register_account_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """只注册 GoPay 账号并设置 PIN，不进入 ChatGPT Plus 付款流程。"""
    from application.gopay_pay_chatgpt import register_gopay_account

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return

    pin = str(payload.get("gopay_pin") or payload.get("pin") or "147258").strip() or "147258"
    sms_provider = str(payload.get("sms_provider") or "herosms").strip().lower() or "herosms"
    max_price = str(payload.get("max_price") or "").strip()
    auto_rebind = _bool_config(payload.get("auto_rebind"), False)

    logger.set_progress(0, 1)
    logger.log(
        f"开始协议注册 GoPay 账户并设置 PIN：sms_provider={sms_provider}, "
        f"pin={'*' * len(pin)}"
    )

    try:
        model = register_gopay_account(
            herosms_api_key=str(payload.get("herosms_api_key") or "").strip(),
            pin=pin,
            proxy=str(payload.get("proxy") or "").strip(),
            envelope_url=str(payload.get("envelope_url") or "").strip(),
            sms_provider=sms_provider,
            smspool_api_key=str(payload.get("smspool_api_key") or "").strip(),
            smsbower_api_key=str(payload.get("smsbower_api_key") or "").strip(),
            smsapi_url=str(payload.get("smsapi_url") or "").strip(),
            smsapi_phone=str(payload.get("smsapi_phone") or "").strip(),
            herosms_max_price_usd=max_price,
            smspool_max_price=max_price,
            auto_rebind=auto_rebind,
            rebind_provider=str(payload.get("rebind_provider") or "herosms").strip().lower(),
            rebind_sms_key=str(payload.get("rebind_sms_key") or "").strip(),
            rebind_country=str(payload.get("rebind_country") or "").strip(),
            rebind_service=str(payload.get("rebind_service") or "").strip(),
            log=logger.log,
        )
    except Exception as exc:
        error = f"GoPay 协议注册任务异常: {exc}"
        logger.log(error, level="error")
        logger.record_error(error)
        logger.set_progress(1, 1)
        logger.finish(TASK_STATUS_FAILED, error=error)
        return

    if not model:
        error = "GoPay 协议注册失败：未产出可用账号"
        logger.log(error, level="error")
        logger.record_error(error)
        logger.set_progress(1, 1)
        logger.finish(TASK_STATUS_FAILED, error=error)
        return

    extra = dict(getattr(model, "extra", {}) or {})
    account_id = int(getattr(model, "id", 0) or 0)
    phone = str(extra.get("phone") or getattr(model, "user_id", "") or getattr(model, "email", "") or "")
    balance_raw = extra.get("balance_rp", 0)
    try:
        balance_rp = int(balance_raw or 0)
    except (TypeError, ValueError):
        balance_rp = 0

    logger.record_success()
    logger.set_progress(1, 1)
    logger.set_result_data({
        "account_id": account_id,
        "email": getattr(model, "email", ""),
        "phone": phone,
        "balance_rp": balance_rp,
        "sms_provider": sms_provider,
    })
    logger.log(f"GoPay 协议注册完成: #{account_id} {phone}")
    logger.finish(TASK_STATUS_SUCCEEDED)


def _execute_gopay_pay_chatgpt_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """GoPay 协议付款 ChatGPT Plus 任务执行入口。

    并发处理 ``payload['chatgpt_account_ids']`` 里的每个 ChatGPT 账号：
    每条账号按"协议拿 cashier_url → 浏览器抓 midtrans_url → 协议付款"三步
    流水线跑一遍，失败不阻塞其它账号。

    若未选 ChatGPT 账号（``chatgpt_account_ids`` 为空）但给了
    ``register_count``，则先注册 N 个 ChatGPT 账号再跑付款。
    """
    from application.gopay_pay_chatgpt import execute_gopay_pay_chatgpt

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return

    chatgpt_ids = [int(v) for v in payload.get("chatgpt_account_ids") or [] if int(v or 0) > 0]
    midtrans_url_override_early = str(payload.get("midtrans_url_override") or "").strip()

    # 需求 2：填了 midtrans_url 就不再注册 ChatGPT，直接拿这个 url 付款。
    # 用占位 chatgpt_account_id=0（execute 会跳过 ChatGPT 相关的标记逻辑）。
    if not chatgpt_ids and midtrans_url_override_early:
        logger.log("已提供 midtrans_url，跳过 ChatGPT 注册，直接付款")
        chatgpt_ids = [0]
    elif not chatgpt_ids:
        # 需求 5：没选 ChatGPT 账号也没 midtrans_url，则先从注册开始。
        register_count = max(int(payload.get("register_count") or 0), 0)
        if register_count <= 0:
            logger.finish(
                TASK_STATUS_FAILED,
                error="未选择 ChatGPT 账号，且未设置 register_count（无法从注册开始）",
            )
            return
        register_extra = dict(payload.get("register_extra") or {})
        # 注册阶段也按任务并发数并行（之前是串行 for 循环，导致 10 个号一个
        # 一个排队注册）。并发上限 = min(payload.concurrency, register_count)。
        register_concurrency = min(
            max(int(payload.get("concurrency") or 1), 1), register_count
        )
        # 短链模式：注册浏览器物理复用——在同一浏览器里注册→拿短链→抓 midtrans。
        _short_link_early = payload.get("use_short_link")
        _use_short_link_early = (
            _short_link_early is True
            or str(_short_link_early or "").strip().lower() in ("1", "true", "yes", "on")
        )
        if _use_short_link_early:
            try:
                logger.log(
                    f"短链复用模式：注册 {register_count} 个 ChatGPT，每个在同一浏览器里"
                    f"注册→拿短链→抓 midtrans（并发 {register_concurrency}）"
                )
                _sl_results = _register_chatgpt_shortlink_grab_for_gopay(
                    register_count, register_extra, logger,
                    concurrency=register_concurrency,
                    checkout_mode=str(payload.get("checkout_mode") or "camoufox_headed"),
                    bit_profile_id=str(payload.get("bit_profile_id") or ""),
                    country=str(payload.get("country") or "ID").upper(),
                    currency=str(payload.get("currency") or "IDR").upper(),
                    grab_timeout=max(int(payload.get("grab_timeout") or 300), 60),
                    proxy=payload.get("proxy") or None,
                )
            except Exception as exc:
                logger.finish(TASK_STATUS_FAILED, error=f"短链复用注册失败: {exc}")
                return
            if not _sl_results:
                logger.finish(TASK_STATUS_FAILED, error="短链复用：没产出任何 (账号+midtrans)")
                return
            chatgpt_ids = [int(r["account_id"]) for r in _sl_results]
            # 把每账号抓到的 midtrans_url 存进 payload，供付款循环按账号取用。
            payload["_shortlink_midtrans_map"] = {
                int(r["account_id"]): str(r["midtrans_url"]) for r in _sl_results
            }
        else:
            try:
                logger.log(
                    f"未选 ChatGPT 账号，先注册 {register_count} 个（并发 {register_concurrency}）"
                )
                chatgpt_ids = _register_chatgpt_accounts_for_gopay(
                    register_count, register_extra, logger,
                    concurrency=register_concurrency,
                )
            except Exception as exc:
                logger.finish(TASK_STATUS_FAILED, error=f"ChatGPT 注册失败: {exc}")
                return
            if not chatgpt_ids:
                logger.finish(TASK_STATUS_FAILED, error="ChatGPT 注册没产出任何账号")
                return

    gopay_account_id = int(payload.get("gopay_account_id") or 0) or None
    cashier_url_override = str(payload.get("cashier_url_override") or "")
    midtrans_url_override = str(payload.get("midtrans_url_override") or "")
    herosms_api_key_override = str(payload.get("herosms_api_key") or "")
    # **设计选择**：override 是手动调试用的（已经手动拿到一个 cashier 或
    # midtrans URL，只想试 GoPay 协议付款这一段）。它绑定在某一个具体的
    # ChatGPT 账号上，在多账号循环里**没法广播**复用——所以只允许单账号
    # 任务用 override，多账号时静默忽略让流水线全自动跑（每个账号都重新
    # 协议拿 cashier，浏览器抓 midtrans）。
    use_override = len(chatgpt_ids) == 1
    country = str(payload.get("country") or "ID").upper()
    currency = str(payload.get("currency") or "IDR").upper()
    headless = bool(payload.get("headless", False))
    checkout_mode = str(payload.get("checkout_mode") or "camoufox_headed")
    bit_profile_id = str(payload.get("bit_profile_id") or "")
    envelope_url = str(payload.get("envelope_url") or "")
    proxy = payload.get("proxy") or None
    grab_timeout = max(int(payload.get("grab_timeout") or 300), 60)
    phone_ttl_seconds = max(int(payload.get("phone_ttl_seconds") or 1200), 60)
    # 没有可用 GoPay 号时是否自动注册新号（默认开启——这是用户要的行为：
    # 抓到 midtrans 后没号就现注册，而不是直接失败）。
    auto_register_gopay = bool(payload.get("auto_register_gopay", True))
    gopay_pin = str(payload.get("gopay_pin") or "147258")
    sms_provider = str(payload.get("sms_provider") or "herosms").strip().lower()
    smspool_api_key = str(payload.get("smspool_api_key") or "")
    smsbower_api_key = str(payload.get("smsbower_api_key") or "")
    # smsapi（固定号 + 查最新短信 API）渠道参数
    smsapi_url = str(payload.get("smsapi_url") or "")
    smsapi_phone = str(payload.get("smsapi_phone") or "")
    # 拿号价格上限（USD）。herosms 与 smspool 都用 USD 计价，默认 0.11；
    # 空串交给插件用默认值。
    max_price = str(payload.get("max_price") or "").strip()
    # GoPay 号来源：auto（默认，先池后注册）/ pool（只用池）/ register（强制注册）。
    gopay_source = str(payload.get("gopay_source") or "auto").strip().lower()
    # #2：付款成功后自动换绑，把 GoPay 号占用的印尼号释放出来。
    _rebind_raw = payload.get("auto_rebind")
    auto_rebind = (
        _rebind_raw is True
        or str(_rebind_raw or "").strip().lower() in ("1", "true", "yes", "on")
    )
    # 换绑专用接码渠道（独立于注册渠道——注册用 smsapi 固定号时换绑仍要买
    # 一次性外国号）。默认 herosms。
    rebind_provider = str(payload.get("rebind_provider") or "herosms").strip().lower()
    rebind_sms_key = str(payload.get("rebind_sms_key") or "")
    rebind_country = str(payload.get("rebind_country") or "")
    rebind_service = str(payload.get("rebind_service") or "")
    # 调试抓包开关（前端）：开启后抓到 midtrans_url 不关浏览器，停在付款页让
    # 人工手动走完 GoPay 网页付款，全程录 HAR + dump 每页 HTML，不跑协议付款。
    _capture_raw = payload.get("capture_payment")
    capture_payment = (
        _capture_raw is True
        or str(_capture_raw or "").strip().lower() in ("1", "true", "yes", "on")
    )
    capture_dir = str(payload.get("capture_dir") or "")
    # 用 Stripe payment_pages/init 协议生成 cashier_url（accessToken →
    # pay.openai.com 长链，纯协议）。
    _stripe_init_raw = payload.get("use_stripe_init")
    use_stripe_init = (
        _stripe_init_raw is True
        or str(_stripe_init_raw or "").strip().lower() in ("1", "true", "yes", "on")
    )
    # 短链模式：checkout_ui_mode=custom → chatgpt.com/checkout/openai_llc 短链。
    _short_link_raw = payload.get("use_short_link")
    use_short_link = (
        _short_link_raw is True
        or str(_short_link_raw or "").strip().lower() in ("1", "true", "yes", "on")
    )

    total = len(chatgpt_ids)
    concurrency = min(max(int(payload.get("concurrency") or 1), 1), total)
    logger.set_progress(0, total)
    logger.log(
        f"开始 GoPay 付款 ChatGPT Plus：账号 {total} 个，并发 {concurrency}，"
        f"checkout_mode={checkout_mode}, country={country}, currency={currency}, "
        f"grab_timeout={grab_timeout}s, phone_ttl={phone_ttl_seconds}s"
    )
    logger.log(
        f"GoPay 号选择：gopay_source={gopay_source}, "
        f"gopay_account_id={gopay_account_id}, sms_provider={sms_provider}"
    )

    results: list[dict[str, Any] | None] = [None] * total
    completed = 0

    def run_one(index: int, chatgpt_account_id: int) -> dict[str, Any]:
        logger.set_subtask(
            f"chatgpt_{chatgpt_account_id}", f"ChatGPT 账号 {chatgpt_account_id}"
        )
        acquired_profile = ""
        try:
            if logger.is_cancel_requested():
                return {"ok": False, "chatgpt_account_id": chatgpt_account_id, "error": "任务已取消"}
            # BitBrowser 模式：从「设置 → BitBrowser」的 profile 池里取一个，
            # 每个 worker 独占一个 profile，跑完归还。前端不再让用户手填
            # profile id。acquire 放进 try 里——池空/读取异常都算该账号失败，
            # 不连累其它并发账号。
            effective_bit_profile = bit_profile_id
            if checkout_mode.startswith("bitbrowser"):
                from application.bitbrowser_profiles import (
                    acquire_profile_for_browser_mode,
                )
                effective_bit_profile, acquired_profile = acquire_profile_for_browser_mode(
                    checkout_mode,
                    fallback=bit_profile_id,
                    log_fn=logger.log,
                )
            logger.log(f"[{index + 1}/{total}] 处理账号 #{chatgpt_account_id}")
            # 短链复用模式：midtrans_url 已经在注册同浏览器里抓好了，按账号取
            # 出来当 override 传进去，execute 内部会跳过自己的拿 cashier + 抓
            # midtrans（不会再开新浏览器）。
            _sl_map = payload.get("_shortlink_midtrans_map") or {}
            _sl_midtrans = str(_sl_map.get(int(chatgpt_account_id)) or "")
            _eff_midtrans_override = _sl_midtrans or (midtrans_url_override if use_override else "")
            out = execute_gopay_pay_chatgpt(
                chatgpt_account_id=chatgpt_account_id,
                gopay_account_id=gopay_account_id,
                cashier_url_override=cashier_url_override if use_override else "",
                midtrans_url_override=_eff_midtrans_override,
                country=country,
                currency=currency,
                headless=headless,
                checkout_mode=checkout_mode,
                bit_profile_id=effective_bit_profile,
                envelope_url=envelope_url,
                proxy=proxy,
                grab_timeout=grab_timeout,
                herosms_api_key_override=herosms_api_key_override,
                phone_ttl_seconds=phone_ttl_seconds,
                auto_register_gopay=auto_register_gopay,
                gopay_pin=gopay_pin,
                sms_provider=sms_provider,
                smspool_api_key=smspool_api_key,
                smsbower_api_key=smsbower_api_key,
                smsapi_url=smsapi_url,
                smsapi_phone=smsapi_phone,
                max_price=max_price,
                gopay_source=gopay_source,
                auto_rebind=auto_rebind,
                rebind_provider=rebind_provider,
                rebind_sms_key=rebind_sms_key,
                rebind_country=rebind_country,
                rebind_service=rebind_service,
                capture_payment=capture_payment,
                capture_dir=capture_dir,
                use_stripe_init=use_stripe_init,
                use_short_link=use_short_link,
                log=logger.log,
                cancel_check=logger.is_cancel_requested,
            )
            logger.log(f"[{index + 1}/{total}] 成功: #{chatgpt_account_id}")
            if int(chatgpt_account_id or 0) > 0:
                try:
                    with Session(engine) as session:
                        model = session.get(AccountModel, int(chatgpt_account_id))
                        if model:
                            marked_account = build_platform_account(session, model)
                            _mark_outlook_mailbox_event(None, marked_account, "plus_success", logger)
                except Exception as exc:
                    logger.log(f"outlookEmail Plus 自动打标签检查失败（忽略）: {exc}", level="warning")
            return {"ok": True, **out}
        except Exception as exc:
            error = str(exc)
            logger.log(f"[{index + 1}/{total}] 失败: {error}", level="error")
            return {"ok": False, "chatgpt_account_id": chatgpt_account_id, "error": error}
        finally:
            if acquired_profile:
                from application.bitbrowser_profiles import release_acquired_profile
                release_acquired_profile(acquired_profile, log_fn=logger.log)
            logger.clear_subtask()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_map = {}
        next_index = 0
        while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
            fut = pool.submit(run_one, next_index, chatgpt_ids[next_index])
            future_map[fut] = next_index
            next_index += 1

        while future_map:
            done, _pending = wait(future_map.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                idx = future_map.pop(fut)
                try:
                    item = fut.result()
                except Exception as exc:
                    item = {"ok": False, "chatgpt_account_id": chatgpt_ids[idx], "error": str(exc)}
                results[idx] = item
                if item.get("ok"):
                    logger.record_success()
                else:
                    logger.record_error(str(item.get("error") or "unknown error"))
                completed += 1
                logger.set_progress(completed, total)

            while next_index < total and len(future_map) < concurrency and not logger.is_cancel_requested():
                fut = pool.submit(run_one, next_index, chatgpt_ids[next_index])
                future_map[fut] = next_index
                next_index += 1

    final_results = [item for item in results if item is not None]
    success_count = sum(1 for item in final_results if item.get("ok"))
    logger.set_result_data({
        "total": total,
        "success_count": success_count,
        "failure_count": len(final_results) - success_count,
        "results": final_results,
    })
    if logger.is_cancel_requested() and success_count < total:
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = (
        TASK_STATUS_SUCCEEDED if success_count == total else TASK_STATUS_FAILED
    )
    logger.finish(final_status)


def _register_chatgpt_shortlink_grab_for_gopay(
    register_count: int,
    register_extra: dict[str, Any],
    logger: "TaskLogger",
    *,
    concurrency: int = 1,
    checkout_mode: str = "camoufox_headed",
    bit_profile_id: str = "",
    country: str = "ID",
    currency: str = "IDR",
    grab_timeout: int = 300,
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    """短链复用流程：每个账号在**同一个浏览器**里注册 → 拿短链 → 打开 → 抓
    midtrans_url，返回 ``[{"account_id", "midtrans_url"}, ...]``。

    物理复用注册浏览器（不关、不换）：短链是 ChatGPT 托管页、URL 无 token，
    必须用注册时那个已登录的浏览器打开。通过给 ChatGPTBrowserRegister 注入
    ``post_register_in_browser`` 回调，在注册拿到 session 后、浏览器还开着时，
    在同一 page 上 ``generate_plus_link(use_short_link)`` 拿短链 → goto → 抓
    midtrans。支持 Camoufox / BitBrowser，N 个并发各占一个浏览器。
    """
    from platforms._browser_backend import parse_checkout_mode
    from platforms.chatgpt import payment as chatgpt_payment

    payload = {
        "platform": "chatgpt",
        "executor_type": str(register_extra.get("executor_type") or "headless"),
        "captcha_solver": str(register_extra.get("captcha_solver") or "auto"),
        "extra": dict(register_extra or {}),
    }
    concurrency = min(max(int(concurrency or 1), 1), max(int(register_count), 1))

    results: list[dict[str, Any]] = []
    results_lock = threading.Lock()

    def _one(seq: int) -> None:
        if logger.is_cancel_requested():
            return
        logger.set_subtask(f"reg_pay_{seq + 1}", f"注册+短链 #{seq + 1}")
        acquired_profile = ""
        midtrans_holder: dict[str, str] = {}
        try:
            resolved_proxy = _resolve_registration_proxy_for_platform(
                "chatgpt", explicit_proxy=None, proxy_getter=lambda: None,
            )
            # 每个并发槽独占一个 BitBrowser profile（同 profile 不能并发开）。
            effective_bit_profile = bit_profile_id
            if checkout_mode.startswith("bitbrowser"):
                from application.bitbrowser_profiles import acquire_profile_for_browser_mode
                effective_bit_profile, acquired_profile = acquire_profile_for_browser_mode(
                    checkout_mode, fallback=bit_profile_id, log_fn=logger.log,
                )
            backend_config = parse_checkout_mode(checkout_mode, bit_profile_id=effective_bit_profile)

            # post_register_in_browser：注册完、浏览器还开着时，在同一 page 上
            # 拿短链 + 抓 midtrans。
            def _post_register(page, session_info: dict) -> dict:
                class _A:
                    pass
                a = _A()
                a.access_token = str(session_info.get("access_token") or "")
                a.cookies = str(session_info.get("cookies") or "")
                if not a.access_token:
                    logger.log("短链复用：注册结果没有 access_token，无法生成短链")
                    return {}
                short_url = chatgpt_payment.generate_plus_link(
                    a, proxy=None, country=country, currency=currency,
                    use_short_link=True,
                )
                logger.log(f"短链已生成（同浏览器复用）: {short_url[:70]}…")
                midtrans = chatgpt_payment.grab_midtrans_on_existing_page(
                    page, short_url, timeout_seconds=grab_timeout,
                    cancel_check=logger.is_cancel_requested, log=logger.log,
                )
                midtrans_holder["midtrans_url"] = midtrans
                return {"midtrans_url": midtrans}

            reg_extra = dict(payload["extra"])
            reg_extra["_reuse_backend_config"] = backend_config
            reg_extra["_post_register_in_browser"] = _post_register
            slot_payload = dict(payload)
            slot_payload["extra"] = reg_extra

            platform = _build_platform_instance(
                "chatgpt", slot_payload, logger, resolved_proxy=resolved_proxy,
            )
            account = platform.register()
            save_account(account)
            with Session(engine) as session:
                fresh = session.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == "chatgpt")
                    .where(AccountModel.email == account.email)
                ).first()
                acc_id = int(fresh.id) if fresh else 0
            midtrans_url = midtrans_holder.get("midtrans_url", "")
            if acc_id and midtrans_url:
                with results_lock:
                    results.append({"account_id": acc_id, "midtrans_url": midtrans_url})
                logger.log(f"注册+短链+抓 midtrans 成功 #{seq + 1}: {account.email} -> ...{midtrans_url[-32:]}")
            elif acc_id:
                logger.log(f"注册成功但没抓到 midtrans #{seq + 1}: {account.email}（短链复用失败）", level="error")
            else:
                logger.log(f"注册后查不到账号 #{seq + 1}", level="error")
        except Exception as exc:
            logger.log(f"注册+短链失败 #{seq + 1}: {exc}", level="error")
        finally:
            if acquired_profile:
                try:
                    from application.bitbrowser_profiles import release_acquired_profile
                    release_acquired_profile(acquired_profile, log_fn=logger.log)
                except Exception:
                    pass
            logger.clear_subtask()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        next_seq = 0
        while next_seq < register_count and len(futures) < concurrency and not logger.is_cancel_requested():
            futures[pool.submit(_one, next_seq)] = next_seq
            next_seq += 1
        while futures:
            done, _pending = wait(futures.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                futures.pop(fut, None)
            while next_seq < register_count and len(futures) < concurrency and not logger.is_cancel_requested():
                futures[pool.submit(_one, next_seq)] = next_seq
                next_seq += 1

    return results


def _register_chatgpt_accounts_for_gopay(
    register_count: int,
    register_extra: dict[str, Any],
    logger: "TaskLogger",
    *,
    concurrency: int = 1,
) -> list[int]:
    """为 GoPay 付款流水线先注册 N 个 ChatGPT 账号，返回新账号 id 列表。

    复用现有 ``_build_platform_instance`` + ``platform.register`` + ``save_account``
    **并发**注册（``concurrency`` 由外层任务的并发数决定）。之前是串行 for
    循环，10 个号只能一个一个排队注册；现在用 ThreadPoolExecutor 同时跑，
    跟后续付款阶段一样的并发模型。

    **默认走浏览器后台模式（headless）**：协议注册当前过不去 ChatGPT 风控，
    浏览器后台更稳。调用方可以用 ``register_extra.executor_type`` 覆盖。
    """
    payload = {
        "platform": "chatgpt",
        "executor_type": str(register_extra.get("executor_type") or "headless"),
        "captcha_solver": str(register_extra.get("captcha_solver") or "auto"),
        "extra": dict(register_extra or {}),
    }
    concurrency = min(max(int(concurrency or 1), 1), max(int(register_count), 1))

    new_ids: list[int] = []
    new_ids_lock = threading.Lock()

    def _register_one(seq: int) -> None:
        if logger.is_cancel_requested():
            return
        logger.set_subtask(f"register_{seq + 1}", f"注册 ChatGPT #{seq + 1}")
        try:
            resolved_proxy = _resolve_registration_proxy_for_platform(
                "chatgpt",
                explicit_proxy=None,
                proxy_getter=lambda: None,
            )
            platform = _build_platform_instance(
                "chatgpt", payload, logger, resolved_proxy=resolved_proxy
            )
            # SMS provider 配置诊断
            sms_provider = extra.get("sms_provider") or ""
            sms_key = str(extra.get("smspool_api_key") or "").strip()
            if sms_key:
                logger.log(f"SMS provider: {sms_provider} (使用任务内 API Key)")
            else:
                from infrastructure.provider_settings_repository import ProviderSettingsRepository
                default_provider = ProviderSettingsRepository().get_default_provider_key("sms")
                if default_provider:
                    logger.log(f"SMS provider: {default_provider} (使用设置页默认)")
                else:
                    logger.log("SMS provider: 未配置，手机号验证将在 add_phone 页面中断")
            account = platform.register()
            # 检测未完成注册页面，强制标 pending_verification
            extra_dict = dict(getattr(account, "extra", {}) or {})
            page_type = extra_dict.get("page_type", "")
            if page_type in ("add_phone", "email_otp", "about_you", "verify-email") or                str(getattr(account, "status", "") or "").endswith("_required"):
                from core.base_platform import AccountStatus
                account.status = AccountStatus.PENDING_VERIFICATION
            save_account(account)
            _mark_outlook_mailbox_event(getattr(platform, "mailbox", None), account, "registration_success", logger)
            # save_account 返回的 model 出 session 即 detached，访问 .id 会抛
            # DetachedInstanceError。用 email 重新查一次拿稳定 id。
            with Session(engine) as session:
                fresh = session.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == "chatgpt")
                    .where(AccountModel.email == account.email)
                ).first()
                if fresh:
                    with new_ids_lock:
                        new_ids.append(int(fresh.id))
            logger.log(f"ChatGPT 注册成功 #{seq + 1}: {account.email}")
        except Exception as exc:
            logger.log(f"ChatGPT 注册失败 #{seq + 1}: {exc}", level="error")
        finally:
            logger.clear_subtask()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        next_seq = 0
        # 先填满并发窗口
        while next_seq < register_count and len(futures) < concurrency and not logger.is_cancel_requested():
            futures[pool.submit(_register_one, next_seq)] = next_seq
            next_seq += 1
        # 完成一个补一个，直到投满 register_count
        while futures:
            done, _pending = wait(futures.keys(), return_when=FIRST_COMPLETED)
            for fut in done:
                futures.pop(fut, None)
            while next_seq < register_count and len(futures) < concurrency and not logger.is_cancel_requested():
                futures[pool.submit(_register_one, next_seq)] = next_seq
                next_seq += 1

    return new_ids


def _execute_account_check_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    account_id = int(payload.get("account_id", 0) or 0)
    if account_id <= 0:
        logger.finish(TASK_STATUS_FAILED, error="缺少 account_id")
        return
    try:
        _, result = _run_single_account_check(account_id, logger)
        logger.set_result_data(result)
        logger.set_progress(1, 1)
        logger.finish(TASK_STATUS_SUCCEEDED)
    except Exception as exc:
        logger.record_error(str(exc))
        logger.finish(TASK_STATUS_FAILED, error=str(exc))


def _execute_account_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "") or "")
    limit = max(int(payload.get("limit", 50) or 50), 1)

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q.limit(limit)).all()

    total = len(accounts)
    logger.set_progress(0, total)
    if total == 0:
        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    results = {"valid": 0, "invalid": 0, "unknown": 0, "error": 0}
    completed = 0
    for model in accounts:
        if logger.is_cancel_requested():
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        try:
            valid, _ = _run_single_account_check(int(model.id or 0), logger)
            if valid is True:
                results["valid"] += 1
            elif valid is False:
                results["invalid"] += 1
            else:
                results["unknown"] += 1
        except Exception as exc:
            results["error"] += 1
            logger.record_error(str(exc))
            logger.log(f"{model.email}: 检测异常 {exc}", level="error")
        completed += 1
        logger.set_progress(completed, total)
    logger.set_result_data(results)
    logger.finish(TASK_STATUS_SUCCEEDED)
