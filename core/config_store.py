"""全局配置持久化 - 存储在 SQLite/PostgreSQL。"""
from __future__ import annotations

from sqlmodel import Field, SQLModel, Session, select
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import engine
from .vault import ENC_PREFIX, EncryptedText, vault_enabled


class ConfigItem(SQLModel, table=True):
    __tablename__ = "configs"
    key: str = Field(primary_key=True)
    # Config values include provider credentials; encrypt them at rest.
    value: str = Field(default="", sa_type=EncryptedText)


class ConfigStore:
    """配置存储；批量写入使用数据库原子 upsert，避免并发丢更新。"""

    def get(self, key: str, default: str = "") -> str:
        with Session(engine) as session:
            item = session.get(ConfigItem, key)
            return item.value if item else default

    def set(self, key: str, value: str) -> None:
        self.set_many({key: value})

    def get_all(self) -> dict[str, str]:
        with Session(engine) as session:
            items = session.exec(select(ConfigItem)).all()
            return {item.key: item.value for item in items}

    def set_many(self, data: dict[str, str]) -> None:
        rows = {str(key): str(value or "") for key, value in data.items() if str(key).strip()}
        if not rows:
            return
        table = ConfigItem.__table__
        with Session(engine) as session:
            if engine.dialect.name == "sqlite":
                statement = sqlite_insert(table).values(
                    [{"key": key, "value": value} for key, value in rows.items()]
                )
                statement = statement.on_conflict_do_update(
                    index_elements=[table.c.key],
                    set_={"value": statement.excluded.value},
                )
                session.execute(statement)
            elif engine.dialect.name == "postgresql":
                statement = postgresql_insert(table).values(
                    [{"key": key, "value": value} for key, value in rows.items()]
                )
                statement = statement.on_conflict_do_update(
                    index_elements=[table.c.key],
                    set_={"value": statement.excluded.value},
                )
                session.execute(statement)
            else:
                # Keep a functional fallback for unsupported development dialects.
                for key, value in rows.items():
                    item = session.get(ConfigItem, key)
                    if item is None:
                        session.add(ConfigItem(key=key, value=value))
                    else:
                        item.value = value
            session.commit()

    def migrate_plaintext(self) -> int:
        """Re-encrypt legacy rows once a vault key is available."""
        if not vault_enabled():
            return 0
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT key, value FROM configs "
                    "WHERE value IS NOT NULL AND value NOT LIKE :prefix"
                ),
                {"prefix": f"enc:v1:%"},
            ).mappings().all()
        values = {str(row["key"]): str(row["value"] or "") for row in rows}
        if values:
            self.set_many(values)
        return len(values)


config_store = ConfigStore()
