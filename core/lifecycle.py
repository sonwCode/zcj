"""账号生命周期管理 — 定时检测、自动续期、过期预警。"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.base_platform import AccountStatus, RegisterConfig
from core.db import AccountModel, AccountOverviewModel, engine
from core.platform_accounts import build_platform_account
from core.registry import get

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _utcnow_ts() -> int:
    return int(_utcnow().timestamp())


def _iso_from_ts(value: int | float) -> str:
    return (
        datetime.fromtimestamp(float(value), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Account validity check
# ---------------------------------------------------------------------------

def check_account_validity(
    account_id: int,
    *,
    log_fn=None,
) -> dict[str, Any]:
    """Check and persist one account without sharing a DB session or plugin.

    Keeping the unit of work account-scoped makes it safe for both the regular
    scheduler and the short post-registration probation queue to use bounded
    concurrency.
    """
    log = log_fn or logger.info
    with Session(engine) as session:
        current = session.get(AccountModel, int(account_id))
        if not current:
            return {
                "account_id": int(account_id),
                "email": "",
                "platform": "",
                "validity_status": "skipped",
                "valid": None,
            }
        email = str(current.email or "")
        platform_name = str(current.platform or "")
        account_obj = build_platform_account(session, current)

    platform_cls = get(platform_name)
    plugin = platform_cls(config=RegisterConfig())
    valid = bool(plugin.check_valid(account_obj))
    check_overview = (
        dict(plugin.get_last_check_overview() or {})
        if hasattr(plugin, "get_last_check_overview")
        else {}
    )
    check_credential_updates = (
        dict(plugin.get_last_check_credential_updates() or {})
        if hasattr(plugin, "get_last_check_credential_updates")
        else {}
    )
    validity_status = str(
        check_overview.get("validity_status") or ("valid" if valid else "invalid")
    ).strip().lower()
    if validity_status not in {"valid", "invalid", "unknown"}:
        validity_status = "unknown"

    with Session(engine) as session:
        model = session.get(AccountModel, int(account_id))
        if not model:
            return {
                "account_id": int(account_id),
                "email": email,
                "platform": platform_name,
                "validity_status": "skipped",
                "valid": None,
            }
        model.updated_at = _utcnow()
        summary_updates = {
            "checked_at": _utcnow_iso(),
            "valid": (
                True
                if validity_status == "valid"
                else False
                if validity_status == "invalid"
                else None
            ),
            "validity_status": validity_status,
            **check_overview,
        }
        lifecycle_status = None
        if validity_status == "valid":
            current_graph = load_account_graphs(session, [int(account_id)]).get(
                int(account_id), {}
            )
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

    if validity_status == "invalid":
        try:
            from core.sub2api_sync import delete_synced_account

            delete_synced_account(
                int(account_id),
                reason=str(check_overview.get("validity_reason") or "invalid"),
                log_fn=log,
            )
        except Exception as exc:
            log(f"  [Sub2API] Auto delete error: {exc}")
        log(f"  {email} ({platform_name}): 失效")
    elif validity_status == "unknown":
        log(f"  {email} ({platform_name}): 检测状态未知")

    return {
        "account_id": int(account_id),
        "email": email,
        "platform": platform_name,
        "validity_status": validity_status,
        "valid": (
            True
            if validity_status == "valid"
            else False
            if validity_status == "invalid"
            else None
        ),
        "overview": check_overview,
    }


def check_accounts_validity(
    *,
    platform: str = "",
    limit: int = 100,
    include_inactive: bool = False,
    exclude_probation_pending: bool = False,
    concurrency: int = 1,
    log_fn=None,
) -> dict[str, int]:
    """Check accounts and keep transient check failures in ``unknown``.

    ``include_inactive`` is used by the retained-account scheduler so a row
    previously marked invalid can recover after reauthentication.  Other
    lifecycle callers retain their narrower active-only behavior.
    """
    log = log_fn or logger.info

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q.limit(limit)).all()
        graphs = load_account_graphs(session, [int(a.id) for a in accounts if a.id])

    # Only check accounts that are in an active lifecycle state
    active_statuses = {"registered", "trial", "subscribed"}
    targets = list(accounts) if include_inactive else [
        a for a in accounts
        if graphs.get(int(a.id or 0), {}).get("lifecycle_status") in active_statuses
    ]
    if exclude_probation_pending:
        targets = [
            account
            for account in targets
            if str(
                (
                    (graphs.get(int(account.id or 0), {}).get("overview") or {})
                    .get("probation")
                    or {}
                ).get("status")
                or ""
            )
            != "pending"
        ]

    results = {
        "valid": 0,
        "invalid": 0,
        "unknown": 0,
        "error": 0,
        "skipped": len(accounts) - len(targets),
    }

    def _record(
        acc,
        result: dict[str, Any] | None = None,
        exc: Exception | None = None,
    ) -> None:
        if exc is not None:
            results["error"] += 1
            log(f"  {acc.email} ({acc.platform}): 检测异常 {exc}")
            return
        status = str((result or {}).get("validity_status") or "unknown")
        if status in {"valid", "invalid", "unknown"}:
            results[status] += 1
        else:
            results["skipped"] += 1

    workers = min(max(int(concurrency or 1), 1), 20, max(len(targets), 1))
    if workers == 1:
        for acc in targets:
            try:
                _record(acc, check_account_validity(int(acc.id or 0), log_fn=log))
            except Exception as exc:
                _record(acc, exc=exc)
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="validity") as pool:
            futures = {
                pool.submit(check_account_validity, int(acc.id or 0), log_fn=log): acc
                for acc in targets
            }
            for future in as_completed(futures):
                acc = futures[future]
                try:
                    _record(acc, future.result())
                except Exception as exc:
                    _record(acc, exc=exc)

    log(f"检测完成: 有效 {results['valid']}, 失效 {results['invalid']}, 未知 {results['unknown']}, "
        f"异常 {results['error']}, 跳过 {results['skipped']}")
    return results


# ---------------------------------------------------------------------------
# Post-registration probation checks
# ---------------------------------------------------------------------------

DEFAULT_PROBATION_INTERVAL_SECONDS = 60
# Compatibility alias for callers that still pass the former offset list.
# A probation is now continuous; the first value becomes its repeat interval.
DEFAULT_PROBATION_OFFSETS_SECONDS = (DEFAULT_PROBATION_INTERVAL_SECONDS,)


def _normalize_probation_offsets(values) -> list[int]:
    result: set[int] = set()
    for value in values or []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.add(min(parsed, 86400))
    return sorted(result)


def _normalize_probation_interval(value: Any, default: int = DEFAULT_PROBATION_INTERVAL_SECONDS) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return min(max(parsed, 15), 3600)


def _ts_from_iso(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def schedule_account_probation(
    account_id: int,
    *,
    offsets_seconds: list[int] | tuple[int, ...] | None = None,
    interval_seconds: int | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Persist a continuous, non-blocking liveness monitor for one account."""
    start_ts = int(now_ts if now_ts is not None else _utcnow_ts())
    legacy_offsets = _normalize_probation_offsets(
        offsets_seconds or DEFAULT_PROBATION_OFFSETS_SECONDS
    )
    interval = _normalize_probation_interval(
        interval_seconds
        if interval_seconds is not None
        else (legacy_offsets[0] if legacy_offsets else DEFAULT_PROBATION_INTERVAL_SECONDS)
    )
    state = {
        "status": "pending",
        "mode": "continuous",
        "started_at": _iso_from_ts(start_ts),
        "started_at_ts": start_ts,
        "interval_seconds": interval,
        "offsets_seconds": [interval],
        "completed_offsets_seconds": [],
        "next_offset_seconds": interval,
        "next_check_at": _iso_from_ts(start_ts + interval),
        "next_check_at_ts": start_ts + interval,
        "last_known_valid_at": _iso_from_ts(start_ts),
        "last_known_valid_at_ts": start_ts,
        "last_status": "",
        "retry_count": 0,
        "check_count": 0,
        "valid_check_count": 0,
        "unknown_check_count": 0,
        "error_count": 0,
    }
    with Session(engine) as session:
        model = session.get(AccountModel, int(account_id))
        if not model:
            return {}
        patch_account_graph(session, model, summary_updates={"probation": state})
        session.add(model)
        session.commit()
    return state


def ensure_continuous_probation_monitors(
    *,
    platform: str = "chatgpt",
    interval_seconds: int = DEFAULT_PROBATION_INTERVAL_SECONDS,
    now_ts: int | None = None,
) -> dict[str, int]:
    """Migrate every active account onto the persisted continuous monitor.

    This closes the deployment gap for accounts created before continuous
    monitoring existed. Invalid accounts stay stopped; the slower retained-row
    sweep can still detect a later manual reauthentication and re-enable them.
    """
    current_ts = int(now_ts if now_ts is not None else _utcnow_ts())
    interval = _normalize_probation_interval(interval_seconds)
    active_lifecycle_statuses = {"registered", "trial", "subscribed"}
    result = {
        "eligible": 0,
        "active": 0,
        "started": 0,
        "migrated": 0,
        "skipped_invalid": 0,
    }

    with Session(engine) as session:
        accounts = session.exec(
            select(AccountModel).where(AccountModel.platform == str(platform or "chatgpt"))
        ).all()
        account_ids = [int(account.id or 0) for account in accounts if account.id]
        graphs = load_account_graphs(session, account_ids)
        changed = False

        for account in accounts:
            account_id = int(account.id or 0)
            if account_id <= 0:
                continue
            graph = dict(graphs.get(account_id) or {})
            lifecycle_status = str(graph.get("lifecycle_status") or "registered").strip().lower()
            overview = dict(graph.get("overview") or {})
            validity_status = str(
                graph.get("validity_status")
                or overview.get("validity_status")
                or "unknown"
            ).strip().lower()
            if lifecycle_status not in active_lifecycle_statuses:
                continue
            if validity_status == "invalid":
                result["skipped_invalid"] += 1
                continue

            result["eligible"] += 1
            state = dict(overview.get("probation") or {})
            current_mode = str(state.get("mode") or "").strip().lower()
            current_status = str(state.get("status") or "").strip().lower()
            current_interval = _normalize_probation_interval(
                state.get("interval_seconds")
                or state.get("next_offset_seconds")
                or interval
            )
            if (
                current_status == "pending"
                and current_mode == "continuous"
                and current_interval == interval
                and int(state.get("next_check_at_ts") or 0) > 0
            ):
                result["active"] += 1
                continue

            was_configured = bool(state)
            started_ts = int(state.get("started_at_ts") or current_ts)
            checked_at = str(overview.get("checked_at") or "")
            checked_at_ts = _ts_from_iso(checked_at)
            existing_next_ts = int(state.get("next_check_at_ts") or 0)
            next_check_ts = current_ts + interval
            if current_status == "pending" and existing_next_ts > current_ts:
                next_check_ts = min(existing_next_ts, next_check_ts)
            last_valid_ts = int(state.get("last_known_valid_at_ts") or 0)
            if not last_valid_ts and validity_status == "valid":
                last_valid_ts = checked_at_ts or current_ts

            state.update(
                {
                    "status": "pending",
                    "mode": "continuous",
                    "started_at": str(state.get("started_at") or _iso_from_ts(started_ts)),
                    "started_at_ts": started_ts,
                    "interval_seconds": interval,
                    "offsets_seconds": [interval],
                    "next_offset_seconds": interval,
                    "next_check_at": _iso_from_ts(next_check_ts),
                    "next_check_at_ts": next_check_ts,
                    "last_known_valid_at": (
                        _iso_from_ts(last_valid_ts) if last_valid_ts else ""
                    ),
                    "last_known_valid_at_ts": last_valid_ts,
                    "check_count": int(state.get("check_count") or 0),
                    "valid_check_count": int(state.get("valid_check_count") or 0),
                    "unknown_check_count": int(state.get("unknown_check_count") or 0),
                    "error_count": int(state.get("error_count") or 0),
                }
            )
            patch_account_graph(
                session,
                account,
                summary_updates={"probation": state},
            )
            session.add(account)
            changed = True
            result["migrated" if was_configured else "started"] += 1

        if changed:
            session.commit()
    result["active"] += result["started"] + result["migrated"]
    return result


def _finish_probation_probe(
    account_id: int,
    result: dict[str, Any] | None,
    *,
    now_ts: int,
    error: str = "",
) -> str:
    """Persist one probe and schedule the next 60-second monitor tick."""
    with Session(engine) as session:
        overview_model = session.get(AccountOverviewModel, int(account_id))
        account_model = session.get(AccountModel, int(account_id))
        if not overview_model or not account_model:
            return "skipped"
        summary = overview_model.get_summary()
        state = dict(summary.get("probation") or {})
        if state.get("status") != "pending":
            return str(state.get("status") or "skipped")

        interval = _normalize_probation_interval(
            state.get("interval_seconds")
            or state.get("next_offset_seconds")
            or DEFAULT_PROBATION_INTERVAL_SECONDS
        )
        scheduled_at_ts = int(state.get("next_check_at_ts") or now_ts)
        validity_status = str((result or {}).get("validity_status") or "unknown")
        state["mode"] = "continuous"
        state["interval_seconds"] = interval
        state["offsets_seconds"] = [interval]
        state["next_offset_seconds"] = interval
        state["last_checked_at"] = _iso_from_ts(now_ts)
        state["last_checked_at_ts"] = now_ts
        state["last_status"] = "error" if error else validity_status
        state["last_error"] = str(error or "")[:500]
        state["last_check_lag_seconds"] = max(now_ts - scheduled_at_ts, 0)
        state["check_count"] = int(state.get("check_count") or 0) + 1

        if validity_status == "invalid":
            state["status"] = "failed"
            state["next_check_at"] = ""
            state["next_check_at_ts"] = 0
            first_invalid_ts = int(state.get("first_invalid_at_ts") or now_ts)
            last_valid_ts = int(
                state.get("last_known_valid_at_ts")
                or state.get("started_at_ts")
                or now_ts
            )
            state["first_invalid_at"] = str(
                state.get("first_invalid_at") or _iso_from_ts(first_invalid_ts)
            )
            state["first_invalid_at_ts"] = first_invalid_ts
            state["last_known_valid_at"] = str(
                state.get("last_known_valid_at") or _iso_from_ts(last_valid_ts)
            )
            state["last_known_valid_at_ts"] = last_valid_ts
            state["detection_window_seconds"] = max(first_invalid_ts - last_valid_ts, 0)
            state["monitor_stopped_at"] = _iso_from_ts(now_ts)
            state["monitor_stopped_at_ts"] = now_ts
        elif validity_status == "valid":
            state["status"] = "pending"
            state["retry_count"] = 0
            state["valid_check_count"] = int(state.get("valid_check_count") or 0) + 1
            state["last_known_valid_at"] = _iso_from_ts(now_ts)
            state["last_known_valid_at_ts"] = now_ts
            state["next_check_at"] = _iso_from_ts(now_ts + interval)
            state["next_check_at_ts"] = now_ts + interval
        else:
            state["status"] = "pending"
            state["retry_count"] = int(state.get("retry_count") or 0) + 1
            state["unknown_check_count"] = int(state.get("unknown_check_count") or 0) + 1
            if error:
                state["error_count"] = int(state.get("error_count") or 0) + 1
            # A transient detector failure must not create a multi-minute blind
            # spot. Keep the same fixed cadence and try again next minute.
            state["next_check_at"] = _iso_from_ts(now_ts + interval)
            state["next_check_at_ts"] = now_ts + interval

        patch_account_graph(session, account_model, summary_updates={"probation": state})
        session.add(account_model)
        session.commit()
        return str(state.get("status") or "pending")


def check_due_account_probations(
    *,
    limit: int = 100,
    concurrency: int = 5,
    now_ts: int | None = None,
    log_fn=None,
) -> dict[str, int]:
    """Run due continuous probes without blocking registration workers."""
    log = log_fn or logger.info
    current_ts = int(now_ts if now_ts is not None else _utcnow_ts())
    monitor_result = ensure_continuous_probation_monitors(now_ts=current_ts)
    with Session(engine) as session:
        rows = session.exec(select(AccountOverviewModel)).all()
    due_rows: list[tuple[int, int]] = []
    for row in rows:
        state = dict(row.get_summary().get("probation") or {})
        if (
            state.get("status") == "pending"
            and int(state.get("next_check_at_ts") or 0) <= current_ts
        ):
            due_rows.append(
                (int(state.get("next_check_at_ts") or 0), int(row.account_id))
            )
    due_rows.sort(key=lambda item: (item[0], item[1]))
    due_total = len(due_rows)
    selected_rows = due_rows[: max(int(limit), 1)]
    due_ids = [account_id for _scheduled_at, account_id in selected_rows]
    max_lag_seconds = max(
        (max(current_ts - scheduled_at, 0) for scheduled_at, _account_id in selected_rows),
        default=0,
    )

    results = {
        "due": due_total,
        "selected": len(due_ids),
        "checked": 0,
        "pending": 0,
        "passed": 0,
        "failed": 0,
        "unknown": 0,
        "error": 0,
        "active_monitors": int(monitor_result.get("active") or 0),
        "monitors_started": int(monitor_result.get("started") or 0),
        "monitors_migrated": int(monitor_result.get("migrated") or 0),
        "max_lag_seconds": max_lag_seconds,
    }

    def _probe(account_id: int) -> tuple[int, dict[str, Any] | None, str]:
        try:
            return account_id, check_account_validity(account_id, log_fn=log), ""
        except Exception as exc:
            return account_id, None, str(exc)

    workers = min(max(int(concurrency or 1), 1), 20, max(len(due_ids), 1))
    probe_results: list[tuple[int, dict[str, Any] | None, str]] = []
    if workers == 1:
        probe_results = [_probe(account_id) for account_id in due_ids]
    elif due_ids:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probation") as pool:
            futures = [pool.submit(_probe, account_id) for account_id in due_ids]
            probe_results = [future.result() for future in as_completed(futures)]

    for account_id, result, error in probe_results:
        results["checked"] += 1
        validity_status = str((result or {}).get("validity_status") or "unknown")
        if error:
            results["error"] += 1
            log(f"  账号 #{account_id} 持续复检异常: {error}")
        elif validity_status == "unknown":
            results["unknown"] += 1
        completed_ts = current_ts if now_ts is not None else _utcnow_ts()
        state_status = _finish_probation_probe(
            account_id,
            result,
            now_ts=completed_ts,
            error=error,
        )
        if state_status in {"pending", "passed", "failed"}:
            results[state_status] += 1

    if due_ids:
        log(
            "60 秒持续复检完成: "
            f"到期 {results['due']}, 已检查 {results['checked']}, "
            f"失效 {results['failed']}, 继续监控 {results['pending']}, "
            f"未知 {results['unknown']}, 异常 {results['error']}, "
            f"最大调度延迟 {results['max_lag_seconds']} 秒"
        )
    return results


# ---------------------------------------------------------------------------
# Token auto-refresh (ChatGPT-specific for now, extensible)
# ---------------------------------------------------------------------------

def refresh_expiring_tokens(
    *,
    platform: str = "",
    hours_before_expiry: int = 24,
    limit: int = 50,
    log_fn=None,
) -> dict[str, int]:
    """Refresh tokens that are about to expire within `hours_before_expiry` hours."""
    log = log_fn or logger.info
    results = {"refreshed": 0, "failed": 0, "skipped": 0}

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        accounts = session.exec(q.limit(limit)).all()
        graphs = load_account_graphs(session, [int(a.id) for a in accounts if a.id])

    active_statuses = {"registered", "trial", "subscribed"}
    for acc in accounts:
        graph = graphs.get(int(acc.id or 0), {})
        if graph.get("lifecycle_status") not in active_statuses:
            results["skipped"] += 1
            continue

        # Currently only ChatGPT has token refresh support
        if acc.platform != "chatgpt":
            results["skipped"] += 1
            continue

        credentials = {
            c["key"]: c["value"]
            for c in (graph.get("credentials") or [])
            if c.get("scope") == "platform"
        }
        refresh_token = credentials.get("refresh_token", "")
        session_token = credentials.get("session_token", "")
        if not refresh_token and not session_token:
            results["skipped"] += 1
            continue

        try:
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            class _Account:
                pass

            a = _Account()
            a.email = acc.email
            a.session_token = session_token
            a.refresh_token = refresh_token
            a.client_id = credentials.get("client_id", "")

            # OAuth endpoints are region-restricted. Reuse the proxy captured
            # during registration when one is available; direct access remains
            # the fallback for legacy accounts without a saved route.
            proxy = str(
                credentials.get("auth_proxy_url")
                or credentials.get("proxy_url")
                or credentials.get("proxy")
                or ""
            ).strip() or None
            manager = TokenRefreshManager(proxy_url=proxy)
            result = manager.refresh_account(a)

            if result.success:
                credential_updates = {}
                if result.access_token:
                    credential_updates["access_token"] = result.access_token
                if result.refresh_token:
                    credential_updates["refresh_token"] = result.refresh_token

                with Session(engine) as session:
                    model = session.get(AccountModel, acc.id)
                    if model and credential_updates:
                        model.updated_at = _utcnow()
                        patch_account_graph(
                            session, model,
                            credential_updates=credential_updates,
                            summary_updates={
                                "last_refresh_at": _utcnow_iso(),
                                "refresh_success": True,
                            },
                        )
                        session.add(model)
                        session.commit()
                results["refreshed"] += 1
                log(f"  ✓ {acc.email}: token 刷新成功")
            else:
                results["failed"] += 1
                log(f"  ✗ {acc.email}: {result.error_message}")
        except Exception as exc:
            results["failed"] += 1
            log(f"  ✗ {acc.email}: 刷新异常 {exc}")

    log(f"刷新完成: 成功 {results['refreshed']}, 失败 {results['failed']}, "
        f"跳过 {results['skipped']}")
    return results


# ---------------------------------------------------------------------------
# Trial expiry warning
# ---------------------------------------------------------------------------

def flag_expiring_trials(
    *,
    hours_warning: int = 48,
    log_fn=None,
) -> dict[str, int]:
    """Flag trial accounts that will expire within `hours_warning` hours."""
    log = log_fn or logger.info
    now_ts = _utcnow_ts()
    warning_ts = now_ts + hours_warning * 3600
    results = {"warned": 0, "expired": 0, "skipped": 0}

    with Session(engine) as session:
        overviews = session.exec(
            select(AccountOverviewModel)
            .where(AccountOverviewModel.lifecycle_status == "trial")
        ).all()

    for overview in overviews:
        summary = overview.get_summary()
        trial_end = int(summary.get("trial_end_time") or 0)
        if not trial_end:
            results["skipped"] += 1
            continue

        if trial_end < now_ts:
            # Already expired
            with Session(engine) as session:
                model = session.get(AccountModel, overview.account_id)
                if model:
                    model.updated_at = _utcnow()
                    patch_account_graph(
                        session, model,
                        lifecycle_status=AccountStatus.EXPIRED.value,
                        summary_updates={"expiry_warning": "expired"},
                    )
                    session.add(model)
                    session.commit()
            results["expired"] += 1
        elif trial_end < warning_ts:
            # Expiring soon
            hours_left = max(0, (trial_end - now_ts) // 3600)
            with Session(engine) as session:
                model = session.get(AccountModel, overview.account_id)
                if model:
                    model.updated_at = _utcnow()
                    patch_account_graph(
                        session, model,
                        summary_updates={
                            "expiry_warning": f"expiring_in_{hours_left}h",
                            "expiry_warning_hours": hours_left,
                        },
                    )
                    session.add(model)
                    session.commit()
            results["warned"] += 1
        else:
            results["skipped"] += 1

    log(f"过期预警: 已过期 {results['expired']}, 即将过期 {results['warned']}, "
        f"跳过 {results['skipped']}")
    return results


# ---------------------------------------------------------------------------
# ChatGPT token refresh + CPA sync + liveness check
# ---------------------------------------------------------------------------

def refresh_and_sync_cpa(
    *,
    platform: str = "chatgpt",
    limit: int = 200,
    log_fn=None,
) -> dict[str, int]:
    """
    刷新 ChatGPT 账号 token，检查存活状态，重新上传到 CPA。
    - 用 session_token 刷新 access_token
    - 用 /backend-api/me 检查存活
    - 存活账号重新生成 CPA JSON 并上传
    - 封禁账号标记为 disabled
    """
    log = log_fn or logger.info
    results = {"refreshed": 0, "uploaded": 0, "dead": 0, "skipped": 0, "error": 0}

    from curl_cffi import requests as cffi_requests
    import json
    import base64

    def _decode_jwt(token: str) -> dict:
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return {}
            payload = parts[1]
            pad = 4 - len(payload) % 4
            if pad != 4:
                payload += "=" * pad
            return json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return {}

    # 读取 CPA 配置
    try:
        from core.config_store import config_store
        cpa_api_url = config_store.get("cpa_api_url", "")
        cpa_api_key = config_store.get("cpa_api_key", "")
    except Exception:
        cpa_api_url, cpa_api_key = "", ""

    # 获取所有活跃 chatgpt 账号
    with Session(engine) as session:
        q = select(AccountModel).where(AccountModel.platform == platform)
        q = q.order_by(AccountModel.created_at.desc()).limit(limit)
        accounts = session.exec(q).all()
        graphs = load_account_graphs(session, [int(a.id) for a in accounts if a.id])

    active_statuses = {"registered", "trial", "subscribed"}

    for acc in accounts:
        graph = graphs.get(int(acc.id or 0), {})
        if graph.get("lifecycle_status") not in active_statuses:
            results["skipped"] += 1
            continue

        credentials = {
            c["key"]: c["value"]
            for c in (graph.get("credentials") or [])
            if c.get("scope") == "platform"
        }
        session_token = credentials.get("session_token", "")
        if not session_token:
            results["skipped"] += 1
            continue

        try:
            # 1. 用 session_token 刷新 access_token
            proxy = credentials.get("proxy", None)
            s = cffi_requests.Session(impersonate="chrome120", proxy=proxy)
            s.cookies.set("__Secure-next-auth.session-token", session_token,
                          domain=".chatgpt.com", path="/")
            resp = s.get("https://chatgpt.com/api/auth/session",
                         headers={"accept": "application/json"}, timeout=30)

            if resp.status_code != 200:
                log(f"  ✗ {acc.email}: session 刷新失败 HTTP {resp.status_code}")
                results["error"] += 1
                continue

            data = resp.json()
            access_token = data.get("accessToken", "")
            if not access_token:
                log(f"  ✗ {acc.email}: 无 accessToken")
                results["error"] += 1
                continue

            results["refreshed"] += 1

            # 更新 credential
            new_session = s.cookies.get("__Secure-next-auth.session-token") or session_token
            # ``/api/auth/session`` returns the ChatGPT Web token.  Keep it
            # separate from the Codex OAuth access token used by Agent
            # Identity/Sub2 so lifecycle sync cannot corrupt later checks.
            credential_updates = {"web_access_token": access_token}
            if new_session != session_token:
                credential_updates["session_token"] = new_session

            with Session(engine) as sess:
                model = sess.get(AccountModel, acc.id)
                if model:
                    model.updated_at = _utcnow()
                    patch_account_graph(
                        sess, model,
                        credential_updates=credential_updates,
                        summary_updates={"last_refresh_at": _utcnow_iso(), "refresh_success": True},
                    )
                    sess.add(model)
                    sess.commit()

            # 2. 检查存活
            check_resp = cffi_requests.get(
                "https://chatgpt.com/backend-api/me",
                headers={"authorization": f"Bearer {access_token}", "accept": "application/json"},
                proxy=proxy, timeout=15, impersonate="chrome120",
            )

            if check_resp.status_code != 200:
                err_detail = ""
                try:
                    err_detail = str(check_resp.json().get("detail", ""))[:80]
                except Exception:
                    err_detail = check_resp.text[:80]
                log(f"  ✗ {acc.email}: 已封禁 ({check_resp.status_code}: {err_detail})")
                results["dead"] += 1
                with Session(engine) as sess:
                    model = sess.get(AccountModel, acc.id)
                    if model:
                        patch_account_graph(
                            sess, model,
                            lifecycle_status=AccountStatus.INVALID.value,
                            summary_updates={"deactivated_at": _utcnow_iso(), "deactivated_reason": err_detail},
                        )
                        sess.add(model)
                        sess.commit()
                continue

            # 3. 上传到 CPA
            if cpa_api_url and cpa_api_key:
                from datetime import timedelta
                tz8 = timezone(timedelta(hours=8))
                jwt_payload = _decode_jwt(access_token)
                auth_info = jwt_payload.get("https://api.openai.com/auth", {})
                account_id = auth_info.get("chatgpt_account_id", "")
                exp = jwt_payload.get("exp", 0)
                iat = jwt_payload.get("iat", 0)
                expired_str = datetime.fromtimestamp(exp, tz=tz8).strftime("%Y-%m-%dT%H:%M:%S+08:00") if exp else ""
                last_refresh = datetime.fromtimestamp(iat, tz=tz8).strftime("%Y-%m-%dT%H:%M:%S+08:00") if iat else _utcnow_iso()

                token_data = {
                    "access_token": access_token,
                    "account_id": account_id,
                    "disabled": False,
                    "email": acc.email,
                    "expired": expired_str,
                    "id_token": access_token,
                    "last_refresh": last_refresh,
                    "refresh_token": credentials.get("refresh_token", ""),
                    "type": "codex",
                }

                # Lifecycle sync must use the same normalized uploader as
                # registration-time CPA sync; keeping one request builder
                # prevents URL/header drift between the two paths.
                from platforms.chatgpt.cpa_upload import upload_to_cpa
                upload_ok, upload_message = upload_to_cpa(
                    token_data, api_url=cpa_api_url, api_key=cpa_api_key
                )
                if upload_ok:
                    results["uploaded"] += 1
                    log(f"  ✓ {acc.email}: 刷新+上传成功")
                else:
                    log(f"  ✗ {acc.email}: {upload_message}")
            else:
                log(f"  ✓ {acc.email}: 刷新成功 (CPA 未配置)")

            time.sleep(0.5)

        except Exception as exc:
            results["error"] += 1
            log(f"  ✗ {acc.email}: 异常 {exc}")

    log(f"[CPA Sync] 刷新 {results['refreshed']}, 上传 {results['uploaded']}, "
        f"封禁 {results['dead']}, 跳过 {results['skipped']}, 错误 {results['error']}")
    return results


# ---------------------------------------------------------------------------
# Lifecycle manager (combines all periodic tasks)
# ---------------------------------------------------------------------------

class LifecycleManager:
    """Runs periodic lifecycle tasks in a background thread."""

    def __init__(
        self,
        *,
        check_interval_hours: float = 6,
        refresh_interval_hours: float = 12,
        cpa_sync_interval_hours: float = 6,
        warning_hours: int = 48,
    ):
        self.check_interval = check_interval_hours * 3600
        self.refresh_interval = refresh_interval_hours * 3600
        self.cpa_sync_interval = cpa_sync_interval_hours * 3600
        self.warning_hours = warning_hours
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_check = 0.0
        self._last_refresh = 0.0
        self._last_cpa_sync = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        # Scheduler owns the fast validity loop.  Defer this legacy 6-hour
        # sweep so startup cannot launch two simultaneous checks through the
        # same account proxy.
        self._last_check = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lifecycle-manager")
        self._thread.start()
        print("[LifecycleManager] 已启动")

    def stop(self):
        self._running = False

    def _loop(self):
        # Wait a bit before first run to let the app fully initialize
        time.sleep(30)
        while self._running:
            now = time.time()
            try:
                # Trial expiry warnings — run every cycle
                flag_expiring_trials(hours_warning=self.warning_hours)

                # Validity check
                if now - self._last_check >= self.check_interval:
                    print("[LifecycleManager] 开始账号有效性检测...")
                    check_accounts_validity()
                    self._last_check = now

                # Token refresh
                if now - self._last_refresh >= self.refresh_interval:
                    print("[LifecycleManager] 开始 token 自动续期...")
                    refresh_expiring_tokens()
                    self._last_refresh = now

                # CPA sync (刷新 token + 存活检查 + 上传)
                if now - self._last_cpa_sync >= self.cpa_sync_interval:
                    print("[LifecycleManager] 开始 CPA 同步 (刷新+检查+上传)...")
                    refresh_and_sync_cpa()
                    self._last_cpa_sync = now

            except Exception as exc:
                print(f"[LifecycleManager] 错误: {exc}")

            # Sleep in small increments so stop() is responsive
            for _ in range(60):
                if not self._running:
                    break
                time.sleep(1)


lifecycle_manager = LifecycleManager()
