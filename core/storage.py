"""Storage backend abstraction.

The account manager was written against SQLite.  This module isolates the
dialect-specific decisions so the same SQLModel tables can also run on
PostgreSQL for multi-node deployments, without changing every caller.

The default stays SQLite: no configuration change and no new dependency.  Point
ACCOUNT_MANAGER_DATABASE_URL at a PostgreSQL URL to switch, for example
    postgresql+psycopg://user:pass@host:5432/zcj

What is genuinely dialect specific is migration and pragma handling.  The
legacy-schema rewrite in core.db uses SQLite PRAGMA statements and is therefore
only run when the engine really is SQLite; everything else uses portable DDL.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import create_engine

DATABASE_URL_ENV = "ACCOUNT_MANAGER_DATABASE_URL"
DEFAULT_SQLITE_FILENAME = "account_manager.db"


@dataclass(frozen=True)
class StorageBackend:
    name: str
    url: str
    is_sqlite: bool
    supports_concurrent_writers: bool
    supports_inline_migrations: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "is_sqlite": self.is_sqlite,
            "supports_concurrent_writers": self.supports_concurrent_writers,
            "supports_inline_migrations": self.supports_inline_migrations,
            "notes": list(self.notes),
        }


def default_database_url() -> str:
    database_path = Path(__file__).resolve().parent.parent / DEFAULT_SQLITE_FILENAME
    return f"sqlite:///{database_path}"


def database_url() -> str:
    return str(os.getenv(DATABASE_URL_ENV, "") or "").strip() or default_database_url()


def dialect_of(url: str | None = None) -> str:
    value = str(url or database_url()).strip().lower()
    if value.startswith("sqlite"):
        return "sqlite"
    if value.startswith("postgresql") or value.startswith("postgres"):
        return "postgresql"
    return value.split(":", 1)[0] if ":" in value else ""


def is_sqlite(url: str | None = None) -> bool:
    return dialect_of(url) == "sqlite"


def is_postgres(url: str | None = None) -> bool:
    return dialect_of(url) == "postgresql"


def describe(url: str | None = None) -> StorageBackend:
    value = str(url or database_url())
    dialect = dialect_of(value)
    if dialect == "sqlite":
        return StorageBackend(
            name="sqlite",
            url=value,
            is_sqlite=True,
            supports_concurrent_writers=False,
            supports_inline_migrations=True,
            notes=(
                "单机默认后端；使用 WAL + busy_timeout 缓解并发写入",
                "多节点部署请改用 PostgreSQL",
            ),
        )
    if dialect == "postgresql":
        return StorageBackend(
            name="postgresql",
            url=value,
            is_sqlite=False,
            supports_concurrent_writers=True,
            supports_inline_migrations=False,
            notes=("需要 psycopg 驱动；SQLite 专用迁移会被跳过",),
        )
    return StorageBackend(
        name=dialect or "unknown",
        url=value,
        is_sqlite=False,
        supports_concurrent_writers=True,
        supports_inline_migrations=False,
        notes=("未验证的方言，仅做通用 SQLModel 建表",),
    )


def build_engine(url: str | None = None) -> Engine:
    """Create an engine with backend-appropriate pooling.

    SQLite keeps the historical single-engine behaviour.  Server databases get
    pre-ping and a bounded pool so a recycled connection does not surface as a
    registration failure.
    """
    value = str(url or database_url())
    if is_sqlite(value):
        return create_engine(value)
    return create_engine(
        value,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
    )