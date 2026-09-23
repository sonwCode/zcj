"""Service-level atomic reservation for pooled resources.

The mailbox and SMS pools already track in-process usage, but that state lives
in a Python object or a JSON file and cannot stop a second process (or a second
worker pool) from claiming the same email or phone.  This repository adds a
database-backed claim with compare-and-set semantics:

    claim  -> INSERT (pool, resource_key); the unique constraint picks a winner
    renew  -> conditional UPDATE guarded by the previously observed values
    release-> conditional UPDATE back to a free state
    sweep  -> expired claims become reclaimable instead of leaking

It is deliberately storage-agnostic: it only uses SQLModel, so it works on both
the SQLite default and a PostgreSQL deployment.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core.db import ResourceReservationModel, engine

POOL_MAILBOX = "mailbox"
POOL_SMS = "sms"
POOL_PROXY = "proxy"

STATUS_RESERVED = "reserved"
STATUS_RELEASED = "released"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

DEFAULT_TTL_SECONDS = 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_key(value: str | None) -> str:
    return str(value or "").strip().lower()


@dataclass
class ReservationRecord:
    id: int
    pool: str
    resource_key: str
    owner: str
    status: str
    metadata: dict
    reserved_at: datetime | None
    expires_at: datetime | None
    updated_at: datetime | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pool": self.pool,
            "resource_key": self.resource_key,
            "owner": self.owner,
            "status": self.status,
            "metadata": self.metadata,
            "reserved_at": self.reserved_at.isoformat() if self.reserved_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _to_record(model: ResourceReservationModel) -> ReservationRecord:
    return ReservationRecord(
        id=int(model.id or 0),
        pool=str(model.pool or ""),
        resource_key=str(model.resource_key or ""),
        owner=str(model.owner or ""),
        status=str(model.status or ""),
        metadata=model.get_metadata(),
        reserved_at=model.reserved_at,
        expires_at=model.expires_at,
        updated_at=model.updated_at,
    )


class ReservationRepository:
    def reserve(
        self,
        *,
        pool: str,
        resource_key: str,
        owner: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        metadata: dict | None = None,
    ) -> ReservationRecord | None:
        """Atomically claim a resource; return None when someone else holds it."""
        key = _normalize_key(resource_key)
        if not pool or not key:
            return None
        now = _utcnow()
        expires = now + timedelta(seconds=max(int(ttl_seconds or 0), 1))
        payload = json.dumps(metadata or {}, ensure_ascii=False)

        with Session(engine) as session:
            existing = session.exec(
                select(ResourceReservationModel)
                .where(ResourceReservationModel.pool == pool)
                .where(ResourceReservationModel.resource_key == key)
            ).first()

            if existing is None:
                model = ResourceReservationModel(
                    pool=pool,
                    resource_key=key,
                    owner=owner,
                    status=STATUS_RESERVED,
                    metadata_json=payload,
                    reserved_at=now,
                    expires_at=expires,
                    updated_at=now,
                )
                session.add(model)
                try:
                    session.commit()
                    session.refresh(model)
                    return _to_record(model)
                except IntegrityError:
                    session.rollback()
                    return None

            held_elsewhere = (
                str(existing.status or "") == STATUS_RESERVED
                and existing.expires_at is not None
                and existing.expires_at > now
                and str(existing.owner or "")
                and str(existing.owner or "") != owner
            )
            if held_elsewhere:
                return None

            statement = (
                update(ResourceReservationModel)
                .where(ResourceReservationModel.id == existing.id)
                .where(ResourceReservationModel.status == existing.status)
                .where(ResourceReservationModel.expires_at == existing.expires_at)
                .values(
                    owner=owner,
                    status=STATUS_RESERVED,
                    metadata_json=payload,
                    reserved_at=now,
                    expires_at=expires,
                    updated_at=now,
                )
            )
            result = session.exec(statement)
            session.commit()
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                return None
            refreshed = session.get(ResourceReservationModel, existing.id)
            return _to_record(refreshed) if refreshed is not None else None

    def release(
        self,
        *,
        pool: str,
        resource_key: str,
        owner: str = "",
        status: str = STATUS_RELEASED,
    ) -> bool:
        key = _normalize_key(resource_key)
        if not pool or not key:
            return False
        now = _utcnow()
        with Session(engine) as session:
            statement = (
                update(ResourceReservationModel)
                .where(ResourceReservationModel.pool == pool)
                .where(ResourceReservationModel.resource_key == key)
                .where(ResourceReservationModel.status == STATUS_RESERVED)
            )
            if owner:
                statement = statement.where(ResourceReservationModel.owner == owner)
            statement = statement.values(status=status, expires_at=now, updated_at=now)
            result = session.exec(statement)
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def get(self, *, pool: str, resource_key: str) -> ReservationRecord | None:
        key = _normalize_key(resource_key)
        if not pool or not key:
            return None
        with Session(engine) as session:
            model = session.exec(
                select(ResourceReservationModel)
                .where(ResourceReservationModel.pool == pool)
                .where(ResourceReservationModel.resource_key == key)
            ).first()
        return _to_record(model) if model is not None else None

    def list(self, *, pool: str = "", status: str = "", limit: int = 500) -> list[ReservationRecord]:
        bounded = min(max(int(limit or 0), 1), 2000)
        with Session(engine) as session:
            query = select(ResourceReservationModel)
            if pool:
                query = query.where(ResourceReservationModel.pool == pool)
            if status:
                query = query.where(ResourceReservationModel.status == status)
            query = query.order_by(ResourceReservationModel.id.desc()).limit(bounded)
            rows = session.exec(query).all()
        return [_to_record(row) for row in rows]

    def sweep_expired(self, *, pool: str = "") -> int:
        now = _utcnow()
        with Session(engine) as session:
            statement = (
                update(ResourceReservationModel)
                .where(ResourceReservationModel.status == STATUS_RESERVED)
                .where(ResourceReservationModel.expires_at <= now)
            )
            if pool:
                statement = statement.where(ResourceReservationModel.pool == pool)
            statement = statement.values(status=STATUS_EXPIRED, updated_at=now)
            result = session.exec(statement)
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    def claim_first(
        self,
        *,
        pool: str,
        candidates: Iterable[str],
        owner: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str | None:
        """Return the first candidate that could be claimed atomically."""
        for candidate in candidates:
            record = self.reserve(
                pool=pool,
                resource_key=candidate,
                owner=owner,
                ttl_seconds=ttl_seconds,
            )
            if record is not None:
                return record.resource_key
        return None


reservation_repository = ReservationRepository()


class ReservationService:
    """Thin policy layer over the repository (pool names + default TTLs)."""

    def __init__(self, repository: ReservationRepository | None = None) -> None:
        self.repository = repository or reservation_repository

    def claim_mailbox(self, resource_key: str, *, owner: str = "", ttl_seconds: int = DEFAULT_TTL_SECONDS):
        return self.repository.reserve(
            pool=POOL_MAILBOX,
            resource_key=resource_key,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )

    def release_mailbox(self, resource_key: str, *, owner: str = "", status: str = STATUS_RELEASED) -> bool:
        return self.repository.release(
            pool=POOL_MAILBOX,
            resource_key=resource_key,
            owner=owner,
            status=status,
        )

    def claim_sms(self, resource_key: str, *, owner: str = "", ttl_seconds: int = 900):
        return self.repository.reserve(
            pool=POOL_SMS,
            resource_key=resource_key,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )

    def release_sms(self, resource_key: str, *, owner: str = "", status: str = STATUS_RELEASED) -> bool:
        return self.repository.release(
            pool=POOL_SMS,
            resource_key=resource_key,
            owner=owner,
            status=status,
        )

    def snapshot(self) -> dict:
        self.repository.sweep_expired()
        return {
            "reserved": len(self.repository.list(status=STATUS_RESERVED)),
            "mailbox": len(self.repository.list(pool=POOL_MAILBOX, status=STATUS_RESERVED)),
            "sms": len(self.repository.list(pool=POOL_SMS, status=STATUS_RESERVED)),
            "proxy": len(self.repository.list(pool=POOL_PROXY, status=STATUS_RESERVED)),
        }


reservation_service = ReservationService()