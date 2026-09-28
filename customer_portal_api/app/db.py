from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
import os

from sqlmodel import Session, SQLModel, create_engine


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_database_path() -> Path:
    """Where the portal database lives when PORTAL_DATABASE_URL is unset.

    Anchored to this package, not to the process working directory, so a cloud
    deployment that starts the process somewhere else still stores its data (and the
    signing key that config.py keeps beside it) in a stable place.
    """
    return Path(__file__).resolve().parent.parent / "customer_portal.db"


def _default_database_url() -> str:
    return f"sqlite:///{default_database_path()}"


DATABASE_URL = os.getenv("PORTAL_DATABASE_URL", _default_database_url())
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


def init_portal_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
