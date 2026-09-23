#!/usr/bin/env python3
"""Offline registration inventory and diagnostics.

Read-only by design: it never opens a network connection, never registers an
account and never mutates pool state.  Run it during support to answer "what is
in the database and what is the current runtime posture".

    python scripts/registration_inventory.py
    python scripts/registration_inventory.py --days 14 --json
    python scripts/registration_inventory.py --limit 50
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _guard(label: str, fn, default):
    try:
        return fn()
    except Exception as exc:
        return {"error": f"{label}: {type(exc).__name__}: {exc}"} if default is None else default


def _storage_section() -> dict:
    def build():
        from core.db import storage_backend
        from core.storage import describe

        info = storage_backend()
        info["notes"] = list(describe().notes)
        return info

    return _guard("storage", build, None)


def _vault_section() -> dict:
    def build():
        from core.vault import vault_status

        return vault_status()

    return _guard("vault", build, None)


def _counts_section() -> dict:
    def build():
        from sqlmodel import Session, func, select

        from core.db import (
            AccountCredentialModel,
            AccountModel,
            ProviderAccountModel,
            ProviderResourceModel,
            ProxyModel,
            TaskLog,
            TaskModel,
            engine,
        )

        def count(session, model) -> int:
            return int(session.exec(select(func.count()).select_from(model)).one() or 0)

        with Session(engine) as session:
            platforms = session.exec(
                select(AccountModel.platform, func.count().label("count"))
                .group_by(AccountModel.platform)
                .order_by(func.count().desc())
            ).all()
            return {
                "accounts": count(session, AccountModel),
                "credentials": count(session, AccountCredentialModel),
                "provider_accounts": count(session, ProviderAccountModel),
                "provider_resources": count(session, ProviderResourceModel),
                "proxies": count(session, ProxyModel),
                "tasks": count(session, TaskModel),
                "task_logs": count(session, TaskLog),
                "accounts_by_platform": [
                    {"platform": row[0] or "unknown", "count": int(row[1] or 0)}
                    for row in platforms
                ],
            }

    return _guard("counts", build, None)


def _pipeline_section(limit: int) -> dict:
    def build():
        from sqlmodel import Session, select

        from core.db import AccountOverviewModel, engine

        stage_counts: dict[str, dict[str, int]] = {}
        with Session(engine) as session:
            rows = session.exec(
                select(AccountOverviewModel).order_by(AccountOverviewModel.account_id.desc()).limit(limit)
            ).all()
            for row in rows:
                summary = row.get_summary() if hasattr(row, "get_summary") else {}
                pipeline = (summary or {}).get("registration_pipeline") or {}
                for stage, state in (pipeline.get("stages") or {}).items():
                    status = str((state or {}).get("status") or "unknown")
                    bucket = stage_counts.setdefault(stage, {})
                    bucket[status] = bucket.get(status, 0) + 1
        return {"sampled_accounts": limit, "stages": stage_counts}

    return _guard("pipeline", build, None)


def _attribution_section(days: int, limit: int) -> dict:
    def build():
        from sqlmodel import Session, func, select

        from core.db import TaskLog, engine
        from core.registration.attribution import summarize_attributions

        cutoff = _utcnow() - timedelta(days=days)
        with Session(engine) as session:
            rows = session.exec(
                select(TaskLog.error, func.count().label("count"))
                .where(TaskLog.status == "failed")
                .where(TaskLog.created_at >= cutoff)
                .where(TaskLog.error != "")
                .group_by(TaskLog.error)
                .order_by(func.count().desc())
                .limit(limit)
            ).all()
        raw = [{"error": row[0], "count": int(row[1] or 0)} for row in rows]
        buckets = summarize_attributions(raw)
        total = sum(item["count"] for item in buckets)
        for item in buckets:
            item["share"] = round(item["count"] / total * 100, 1) if total else 0
        return {"window_days": days, "total_failures": total, "categories": buckets}

    return _guard("attribution", build, None)


def _reservation_section() -> dict:
    def build():
        from infrastructure.reservation_repository import reservation_service

        return reservation_service.snapshot()

    return _guard("reservations", build, None)


def _preflight_section() -> dict:
    def build():
        from core.registration.preflight import run_preflight

        return run_preflight().to_dict()

    return _guard("preflight", build, None)


def _clearance_section() -> dict:
    def build():
        from core.cloudflare_clearance import clearance_status

        return clearance_status()

    return _guard("clearance", build, None)


def collect(days: int, limit: int) -> dict:
    return {
        "generated_at": _utcnow().isoformat(),
        "offline": True,
        "storage": _storage_section(),
        "vault": _vault_section(),
        "counts": _counts_section(),
        "pipeline": _pipeline_section(limit),
        "failure_attribution": _attribution_section(days, limit),
        "reservations": _reservation_section(),
        "cloudflare_clearance": _clearance_section(),
        "preflight": _preflight_section(),
    }


def render_text(report: dict) -> str:
    lines = ["ZCJ 注册库存诊断（离线只读）", "=" * 34]
    lines.append(f"生成时间: {report.get('generated_at')}")
    storage = report.get("storage") or {}
    lines.append(f"存储后端: {storage.get('name', '?')}  {storage.get('url', '')}")
    vault = report.get("vault") or {}
    lines.append(f"凭据加密: {'已启用' if vault.get('enabled') else '未启用'} ({vault.get('backend', 'none')})")
    counts = report.get("counts") or {}
    if "error" in counts:
        lines.append(f"计数: {counts['error']}")
    else:
        lines.append(
            "计数: "
            + ", ".join(
                f"{key}={counts.get(key)}"
                for key in ("accounts", "credentials", "provider_accounts", "provider_resources", "proxies", "tasks", "task_logs")
            )
        )
        for row in counts.get("accounts_by_platform") or []:
            lines.append(f"  - {row['platform']}: {row['count']}")
    attr = report.get("failure_attribution") or {}
    lines.append("")
    lines.append(f"失败归因（近 {attr.get('window_days', '?')} 天，共 {attr.get('total_failures', 0)} 条）")
    for item in attr.get("categories") or []:
        lines.append(f"  {item['code']:<24} {item['count']:>6}  {item['share']:>5}%  {item['label']}")
    pipeline = report.get("pipeline") or {}
    if pipeline.get("stages"):
        lines.append("")
        lines.append("注册流水线阶段状态:")
        for stage, statuses in sorted(pipeline["stages"].items()):
            detail = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
            lines.append(f"  {stage:<18} {detail}")
    reservations = report.get("reservations") or {}
    lines.append("")
    lines.append(f"资源预占: {reservations}")
    clearance = report.get("cloudflare_clearance") or {}
    lines.append(f"CF 清关: {'已启用' if clearance.get('enabled') else '未启用'} ({clearance.get('provider', 'none')})")
    preflight = report.get("preflight") or {}
    lines.append(f"前置检查: {'通过' if preflight.get('ok') else '未通过'}")
    for check in preflight.get("checks") or []:
        mark = "OK" if check.get("ok") else ("FAIL" if check.get("required") else "warn")
        lines.append(f"  [{mark:<4}] {check.get('name')}: {check.get('detail')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ZCJ 离线只读注册库存诊断")
    parser.add_argument("--days", type=int, default=7, help="失败归因统计窗口（天）")
    parser.add_argument("--limit", type=int, default=200, help="流水线采样与错误分组上限")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    report = collect(max(args.days, 1), max(args.limit, 1))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())