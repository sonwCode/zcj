"""注册成功率仪表盘 API。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter
from sqlmodel import Session, select, func

from core.db import TaskLog, ProxyModel, AccountModel, AccountOverviewModel, engine
from core.registration.attribution import attribution_table, summarize_attributions
from core.registration.retry_policy import retry_policy_table

router = APIRouter(prefix="/stats", tags=["stats"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/overview")
def stats_overview():
    """全局概览：总注册数、成功率、账号状态分布。"""
    with Session(engine) as session:
        total = int(session.exec(
            select(func.count()).select_from(TaskLog)
        ).one() or 0)
        success = int(session.exec(
            select(func.count()).select_from(TaskLog)
            .where(TaskLog.status == "success")
        ).one() or 0)
        failed = total - success

        # Account status distribution
        statuses = session.exec(
            select(
                AccountOverviewModel.lifecycle_status,
                func.count(),
            ).group_by(AccountOverviewModel.lifecycle_status)
        ).all()
        account_distribution = {row[0]: row[1] for row in statuses}

        # Total accounts
        total_accounts = int(session.exec(
            select(func.count()).select_from(AccountModel)
        ).one() or 0)

    return {
        "total_registrations": total,
        "success": success,
        "failed": failed,
        "success_rate": round(success / total * 100, 1) if total else 0,
        "total_accounts": total_accounts,
        "account_distribution": account_distribution,
    }


@router.get("/by-platform")
def stats_by_platform():
    """按平台统计成功率。"""
    with Session(engine) as session:
        rows = session.exec(
            select(
                TaskLog.platform,
                TaskLog.status,
                func.count(),
            ).group_by(TaskLog.platform, TaskLog.status)
        ).all()

    platforms: dict[str, dict] = {}
    for platform, status, count in rows:
        if platform not in platforms:
            platforms[platform] = {"platform": platform, "success": 0, "failed": 0, "total": 0}
        if status == "success":
            platforms[platform]["success"] += count
        else:
            platforms[platform]["failed"] += count
        platforms[platform]["total"] += count

    for p in platforms.values():
        p["success_rate"] = round(p["success"] / p["total"] * 100, 1) if p["total"] else 0

    return sorted(platforms.values(), key=lambda x: x["total"], reverse=True)


@router.get("/by-day")
def stats_by_day(days: int = 30, platform: str = ""):
    """按天统计注册趋势。"""
    cutoff = _utcnow() - timedelta(days=days)
    with Session(engine) as session:
        q = select(TaskLog).where(TaskLog.created_at >= cutoff)
        if platform:
            q = q.where(TaskLog.platform == platform)
        logs = session.exec(q.order_by(TaskLog.created_at)).all()

    daily: dict[str, dict] = {}
    for log in logs:
        day = log.created_at.strftime("%Y-%m-%d") if log.created_at else "unknown"
        if day not in daily:
            daily[day] = {"date": day, "success": 0, "failed": 0, "total": 0}
        if log.status == "success":
            daily[day]["success"] += 1
        else:
            daily[day]["failed"] += 1
        daily[day]["total"] += 1

    for d in daily.values():
        d["success_rate"] = round(d["success"] / d["total"] * 100, 1) if d["total"] else 0

    return sorted(daily.values(), key=lambda x: x["date"])


@router.get("/by-proxy")
def stats_by_proxy():
    """代理成功率排行。"""
    with Session(engine) as session:
        proxies = session.exec(
            select(ProxyModel).order_by(ProxyModel.success_count.desc())
        ).all()

    return [
        {
            "id": p.id,
            "url": p.url,
            "region": p.region,
            "success": p.success_count,
            "fail": p.fail_count,
            "total": p.success_count + p.fail_count,
            "success_rate": round(
                p.success_count / (p.success_count + p.fail_count) * 100, 1
            ) if (p.success_count + p.fail_count) else 0,
            "is_active": p.is_active,
        }
        for p in proxies
    ]


@router.get("/errors")
def stats_errors(days: int = 7, platform: str = "", limit: int = 20):
    """最近的失败错误聚合（按错误信息分组）。"""
    cutoff = _utcnow() - timedelta(days=days)
    with Session(engine) as session:
        q = (
            select(TaskLog.error, func.count().label("count"))
            .where(TaskLog.status == "failed")
            .where(TaskLog.created_at >= cutoff)
            .where(TaskLog.error != "")
        )
        if platform:
            q = q.where(TaskLog.platform == platform)
        q = q.group_by(TaskLog.error).order_by(func.count().desc()).limit(limit)
        rows = session.exec(q).all()

    return [{"error": row[0], "count": row[1]} for row in rows]


@router.get("/attribution")
def stats_attribution(days: int = 7, platform: str = "", limit: int = 200):
    """失败归因聚合：把原始错误串映射为稳定的根因分类。"""
    cutoff = _utcnow() - timedelta(days=days)
    with Session(engine) as session:
        q = (
            select(TaskLog.error, func.count().label("count"))
            .where(TaskLog.status == "failed")
            .where(TaskLog.created_at >= cutoff)
            .where(TaskLog.error != "")
        )
        if platform:
            q = q.where(TaskLog.platform == platform)
        q = q.group_by(TaskLog.error).order_by(func.count().desc()).limit(limit)
        rows = session.exec(q).all()

    raw = [{"error": row[0], "count": int(row[1] or 0)} for row in rows]
    buckets = summarize_attributions(raw)
    total = sum(item["count"] for item in buckets)
    for item in buckets:
        item["share"] = round(item["count"] / total * 100, 1) if total else 0
    return {
        "window_days": days,
        "platform": platform,
        "total_failures": total,
        "categories": buckets,
        "table": attribution_table(),
        "retry_policy": retry_policy_table(),
    }


@router.get("/diagnostics")
def stats_diagnostics():
    """只读运行诊断：凭据加密、CF 清关、入库边界与前置检查。"""
    from core.cloudflare_clearance import clearance_status
    from core.registration.persistence import boundary_summary
    from core.registration.preflight import run_preflight
    from core.vault import vault_status

    try:
        # 带上 sentinel：注册路径对 ChatGPT 会跑这项检查，诊断端点若省略，
        # 哨兵失效时会一边报"前置检查通过"一边让注册全部失败。
        preflight = run_preflight(sentinel=True).to_dict()
    except Exception as exc:
        preflight = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "vault": vault_status(),
        "cloudflare_clearance": clearance_status(),
        "persistence_boundary": boundary_summary(),
        "preflight": preflight,
    }
