"""数据库模型 - SQLite via SQLModel"""
import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import UniqueConstraint, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, SQLModel, Session, select

from .storage import (
    build_engine,
    database_url,
    describe,
    is_postgres,
    is_sqlite,
    is_sqlite_memory,
    secure_sqlite_file_permissions,
)
from .vault import EncryptedText, blind_index


def _utcnow():
    return datetime.now(timezone.utc)


DATABASE_URL = database_url()
engine = build_engine(DATABASE_URL)


@contextmanager
def _migration_lock():
    """Serialize startup DDL and data migrations across service processes."""
    if is_postgres(DATABASE_URL):
        # Hold a transaction-scoped advisory lock on a dedicated connection
        # while the migration body opens its own connections.
        with engine.connect() as connection:
            with connection.begin():
                connection.exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(hashtext('zcj_account_manager_migrations'))"
                )
                yield
        return
    if not is_sqlite(DATABASE_URL) or is_sqlite_memory(DATABASE_URL) or fcntl is None:
        yield
        return
    database = str(getattr(engine.url, "database", "") or "")
    lock_path = Path(f"{database}.migration.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_TASK_DISPATCH_THREAD_LOCK = threading.RLock()


@contextmanager
def task_dispatch_lock():
    """Serialize task selection and capacity accounting across workers."""
    # fcntl locks coordinate processes, while this guard also coordinates
    # threads in the same process and keeps the SQLite path deterministic.
    with _TASK_DISPATCH_THREAD_LOCK:
        if is_postgres(DATABASE_URL):
            # A session advisory lock covers the separate SQLModel connection
            # used for the SELECT/conditional UPDATE below.
            with engine.connect() as connection:
                connection.exec_driver_sql(
                    "SELECT pg_advisory_lock(hashtext('zcj_account_manager_task_dispatch'))"
                )
                try:
                    yield
                finally:
                    connection.exec_driver_sql(
                        "SELECT pg_advisory_unlock(hashtext('zcj_account_manager_task_dispatch'))"
                    )
            return
        if not is_sqlite(DATABASE_URL) or is_sqlite_memory(DATABASE_URL) or fcntl is None:
            yield
            return
        database = str(getattr(engine.url, "database", "") or "")
        lock_path = Path(f"{database}.task_dispatch.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def storage_backend() -> dict:
    """Return the active storage backend description (read-only)."""
    return describe(DATABASE_URL).to_dict()


if is_sqlite(DATABASE_URL):


    @event.listens_for(engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _connection_record):
        """Use WAL and a sane busy timeout so concurrent writers do not fail fast.

        The registration workload runs many threads against one SQLite file; the
        default rollback-journal mode serialises readers against the writer.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
            cursor.close()
        secure_sqlite_file_permissions(DATABASE_URL)

_ACCOUNT_SAVE_LOCKS = tuple(threading.RLock() for _ in range(64))


def _account_save_lock(platform: str, email: str) -> threading.RLock:
    key = (str(platform or "").strip().lower(), str(email or "").strip().lower())
    return _ACCOUNT_SAVE_LOCKS[hash(key) % len(_ACCOUNT_SAVE_LOCKS)]


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("platform", "email", name="uq_accounts_platform_email"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    password: str = Field(sa_type=EncryptedText)
    user_id: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class AccountOverviewModel(SQLModel, table=True):
    __tablename__ = "account_overviews"

    account_id: int = Field(primary_key=True, foreign_key="accounts.id")
    lifecycle_status: str = Field(default="registered", index=True)
    validity_status: str = Field(default="unknown", index=True)
    plan_state: str = Field(default="unknown", index=True)
    plan_name: str = ""
    display_status: str = Field(default="registered", index=True)
    remote_email: str = ""
    checked_at: Optional[datetime] = None
    summary_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_summary(self) -> dict:
        return json.loads(self.summary_json or "{}")

    def set_summary(self, data: dict):
        self.summary_json = json.dumps(data or {}, ensure_ascii=False)


class AccountCredentialModel(SQLModel, table=True):
    __tablename__ = "account_credentials"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id")
    scope: str = Field(default="platform", index=True)
    provider_name: str = Field(default="", index=True)
    credential_type: str = Field(default="secret", index=True)
    key: str = Field(default="", index=True)
    value: str = Field(default="", sa_type=EncryptedText)
    is_primary: bool = False
    source: str = ""
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderAccountModel(SQLModel, table=True):
    __tablename__ = "provider_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id")
    provider_type: str = Field(default="mailbox", index=True)
    provider_name: str = Field(default="", index=True)
    login_identifier: str = Field(default="", index=True)
    display_name: str = ""
    credentials_json: str = Field(default="{}", sa_type=EncryptedText)
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_credentials(self) -> dict:
        return json.loads(self.credentials_json or "{}")

    def set_credentials(self, data: dict):
        self.credentials_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderResourceModel(SQLModel, table=True):
    __tablename__ = "provider_resources"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="accounts.id")
    provider_type: str = Field(default="mailbox", index=True)
    provider_name: str = Field(default="", index=True)
    resource_type: str = Field(default="resource", index=True)
    resource_identifier: str = Field(default="", index=True, sa_type=EncryptedText)
    handle: str = Field(default="", sa_type=EncryptedText)
    display_name: str = ""
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderDefinitionModel(SQLModel, table=True):
    __tablename__ = "provider_definitions"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_key", name="uq_provider_definitions_type_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(index=True)
    provider_key: str = Field(index=True)
    label: str = ""
    description: str = ""
    driver_type: str = ""
    default_auth_mode: str = ""
    enabled: bool = True
    is_builtin: bool = False
    category: str = ""  # "free" | "selfhost" | "custom"
    auth_modes_json: str = "[]"
    fields_json: str = "[]"
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_auth_modes(self) -> list[dict]:
        return json.loads(self.auth_modes_json or "[]")

    def set_auth_modes(self, data: list[dict]):
        self.auth_modes_json = json.dumps(data or [], ensure_ascii=False)

    def get_fields(self) -> list[dict]:
        return json.loads(self.fields_json or "[]")

    def set_fields(self, data: list[dict]):
        self.fields_json = json.dumps(data or [], ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class ProviderSettingModel(SQLModel, table=True):
    __tablename__ = "provider_settings"
    __table_args__ = (
        UniqueConstraint("provider_type", "provider_key", name="uq_provider_settings_type_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    provider_type: str = Field(index=True)
    provider_key: str = Field(index=True)
    display_name: str = ""
    auth_mode: str = ""
    enabled: bool = True
    is_default: bool = False
    config_json: str = Field(default="{}", sa_type=EncryptedText)
    auth_json: str = Field(default="{}", sa_type=EncryptedText)
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_config(self) -> dict:
        return json.loads(self.config_json or "{}")

    def set_config(self, data: dict):
        self.config_json = json.dumps(data or {}, ensure_ascii=False)

    def get_auth(self) -> dict:
        return json.loads(self.auth_json or "{}")

    def set_auth(self, data: dict):
        self.auth_json = json.dumps(data or {}, ensure_ascii=False)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class PlatformCapabilityOverrideModel(SQLModel, table=True):
    __tablename__ = "platform_capability_overrides"
    __table_args__ = (
        UniqueConstraint("platform_name", name="uq_platform_capability_overrides_platform"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform_name: str = Field(index=True)
    capabilities_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_capabilities(self) -> dict:
        return json.loads(self.capabilities_json or "{}")

    def set_capabilities(self, data: dict):
        self.capabilities_json = json.dumps(data or {}, ensure_ascii=False)


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str
    email: str
    status: str        # success | failed
    error: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)


class TaskModel(SQLModel, table=True):
    __tablename__ = "tasks"

    id: str = Field(primary_key=True)
    type: str = Field(index=True)
    platform: str = Field(default="", index=True)
    status: str = Field(default="pending", index=True)
    payload_json: str = "{}"
    result_json: str = "{}"
    progress_current: int = 0
    progress_total: int = 0
    success_count: int = 0
    error_count: int = 0
    error: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    # Cross-process worker ownership. A lease prevents an old worker from
    # writing over a task after another process has recovered it.
    worker_id: str = Field(default="", index=True)
    lease_expires_at: Optional[float] = Field(default=None, index=True)

    def get_payload(self) -> dict:
        return json.loads(self.payload_json or "{}")

    def set_payload(self, data: dict):
        self.payload_json = json.dumps(data or {}, ensure_ascii=False)

    def get_result(self) -> dict:
        return json.loads(self.result_json or "{}")

    def set_result(self, data: dict):
        self.result_json = json.dumps(data or {}, ensure_ascii=False)


class TaskEventModel(SQLModel, table=True):
    __tablename__ = "task_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    type: str = Field(default="log", index=True)
    level: str = "info"
    message: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow)

    def get_detail(self) -> dict:
        return json.loads(self.detail_json or "{}")

    def set_detail(self, data: dict):
        self.detail_json = json.dumps(data or {}, ensure_ascii=False)


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    # ``url`` embeds proxy credentials, so it is encrypted at rest.  Equality
    # lookups go through the deterministic ``url_index`` blind index instead.
    url: str = Field(sa_type=EncryptedText)
    url_index: str = Field(default="", index=True)
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None


@event.listens_for(ProxyModel, "before_insert")
@event.listens_for(ProxyModel, "before_update")
def _sync_proxy_url_index(_mapper, _connection, target) -> None:
    """Keep the blind index in step with the plaintext URL on every write."""
    raw = getattr(target, "url", "")
    if raw:
        target.url_index = blind_index(str(raw))


class SmsPoolBlacklistModel(SQLModel, table=True):
    """SMS 号码池黑名单 - 多次触发 OAS_ERROR / 风控的号码自动加入。"""

    __tablename__ = "sms_pool_blacklist"
    __table_args__ = (
        UniqueConstraint("phone_e164", name="uq_sms_pool_blacklist_phone"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    phone_e164: str = Field(index=True)
    relay_url: str = ""
    relay_host: str = Field(default="", index=True)
    reason: str = ""           # 简短原因码: oas_error / manual / other
    error_code: str = ""       # PayPal 原始错误码
    task_id: str = ""
    fail_count: int = 1
    last_error_message: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    last_attempted_at: datetime = Field(default_factory=_utcnow)


class ResourceReservationModel(SQLModel, table=True):
    """Cross-process reservation of a pool resource (mailbox, phone, proxy).

    The unique constraint on (pool, resource_key) is the atomicity primitive:
    two workers racing for the same resource can both INSERT, but only one
    COMMIT succeeds, so the loser sees IntegrityError and moves on.
    """

    __tablename__ = "resource_reservations"
    __table_args__ = (
        UniqueConstraint("pool", "resource_key", name="uq_resource_reservations_pool_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    pool: str = Field(index=True)
    resource_key: str = Field(index=True)
    owner: str = Field(default="", index=True)
    status: str = Field(default="reserved", index=True)
    metadata_json: str = "{}"
    reserved_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def get_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    def set_metadata(self, data: dict):
        self.metadata_json = json.dumps(data or {}, ensure_ascii=False)


class RegisteredEmailHistoryModel(SQLModel, table=True):
    """已成功注册过的邮箱历史 - 只记录时间戳，不保存任何凭据。

    用于避免把同一个邮箱重复投给注册流程（尤其是允许复用的邮箱池），
    并给运营界面提供"这个邮箱什么时候用过"的回答。
    """

    __tablename__ = "registered_email_history"
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "email",
            name="uq_registered_email_history_platform_email",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)
    email: str = Field(index=True)
    first_registered_at: datetime = Field(default_factory=_utcnow)
    last_registered_at: datetime = Field(default_factory=_utcnow)


class MicrosoftMailboxModel(SQLModel, table=True):
    """本地微软邮箱池库存。

    密码、恢复密码、刷新令牌和 TOTP 密钥都以密文列保存；明文只在内存中出现，
    加解密由仓库层通过 core.vault 完成。
    """

    __tablename__ = "microsoft_mailboxes"
    __table_args__ = (
        UniqueConstraint("email_key", name="uq_microsoft_mailboxes_email_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    email_key: str = Field(index=True)
    password_ciphertext: str = ""
    login_account: str = ""
    imap_host: str = ""
    imap_port: str = ""
    imap_account_type: str = ""
    imap_security: str = ""
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_security: str = ""
    note: str = ""
    proxy_mode: str = ""
    proxy: str = ""
    label: str = ""
    recovery_email: str = ""
    recovery_password_ciphertext: str = ""
    client_id: str = ""
    refresh_token_ciphertext: str = ""
    totp_secret_ciphertext: str = ""
    source_format: str = ""
    use_count: int = Field(default=0, index=True)
    max_uses: int = 6
    status: str = Field(default="available", index=True)
    allocation_version: int = Field(default=1)
    last_reserved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class MicrosoftMailboxLeaseModel(SQLModel, table=True):
    """微软邮箱别名槽租约。

    (mailbox_id, alias_index) 的唯一约束就是原子性原语：两个 worker 抢同一个
    别名槽时可以都 INSERT，但只有一个 COMMIT 成功，另一个拿到 IntegrityError。
    """

    __tablename__ = "microsoft_mailbox_leases"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_id",
            "alias_index",
            name="uq_microsoft_mailbox_leases_slot",
        ),
        UniqueConstraint("lease_token", name="uq_microsoft_mailbox_leases_token"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    mailbox_id: int = Field(index=True, foreign_key="microsoft_mailboxes.id")
    alias_index: int = Field(index=True)
    lease_token: str = Field(index=True)
    status: str = Field(default="reserved", index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


def _normalized_history_key(platform: str, email: str) -> tuple[str, str]:
    return str(platform or "").strip().lower(), str(email or "").strip().lower()


def _normalized_utc(value: datetime | None) -> datetime:
    value = value or _utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _history_key_is_usable(key: tuple[str, str]) -> bool:
    return bool(key[0]) and "@" in key[1] and not any(ch.isspace() for ch in key[1])


def record_registered_email(
    platform: str,
    email: str,
    *,
    registered_at: datetime | None = None,
) -> bool:
    """记录一次成功注册所用的邮箱，不保存凭据。"""
    key = _normalized_history_key(platform, email)
    if not _history_key_is_usable(key):
        return False
    occurred_at = _normalized_utc(registered_at)
    with Session(engine) as session:
        existing = session.exec(
            select(RegisteredEmailHistoryModel)
            .where(RegisteredEmailHistoryModel.platform == key[0])
            .where(RegisteredEmailHistoryModel.email == key[1])
        ).first()
        if existing:
            existing.first_registered_at = min(
                _normalized_utc(existing.first_registered_at),
                occurred_at,
            )
            existing.last_registered_at = max(
                _normalized_utc(existing.last_registered_at),
                occurred_at,
            )
            session.add(existing)
        else:
            session.add(
                RegisteredEmailHistoryModel(
                    platform=key[0],
                    email=key[1],
                    first_registered_at=occurred_at,
                    last_registered_at=occurred_at,
                )
            )
        session.commit()
    return True


def _backfill_registered_email_history() -> None:
    """从已保留账号和历史上成功的注册事件里回填邮箱历史。

    任务事件只在首次部署（表为空）时扫描一次；之后新增的成功注册会直接写入，
    而保留账号每次都便宜地对账。
    """
    success_prefix = "注册成功:"
    with Session(engine) as session:
        rows = session.exec(select(RegisteredEmailHistoryModel)).all()
        by_key = {(row.platform, row.email): row for row in rows}
        candidates: list[tuple[str, str, datetime]] = [
            (account.platform, account.email, account.created_at)
            for account in session.exec(select(AccountModel)).all()
        ]
        if not rows:
            event_rows = session.exec(
                select(TaskEventModel, TaskModel)
                .join(TaskModel, TaskModel.id == TaskEventModel.task_id)
                .where(TaskModel.type == "register")
                .where(TaskEventModel.message.startswith(success_prefix))
            ).all()
            candidates.extend(
                (
                    task.platform or "chatgpt",
                    event.message[len(success_prefix):].strip(),
                    event.created_at,
                )
                for event, task in event_rows
            )

        for platform, email, registered_at in candidates:
            key = _normalized_history_key(platform, email)
            if not _history_key_is_usable(key):
                continue
            occurred_at = _normalized_utc(registered_at)
            existing = by_key.get(key)
            if existing:
                existing.first_registered_at = min(
                    _normalized_utc(existing.first_registered_at),
                    occurred_at,
                )
                existing.last_registered_at = max(
                    _normalized_utc(existing.last_registered_at),
                    occurred_at,
                )
                session.add(existing)
                continue
            model = RegisteredEmailHistoryModel(
                platform=key[0],
                email=key[1],
                first_registered_at=occurred_at,
                last_registered_at=occurred_at,
            )
            session.add(model)
            by_key[key] = model
        session.commit()


def save_account(account) -> 'AccountModel':
    """从 base_platform.Account 存入数据库（同平台同邮箱则更新）"""
    from core.account_graph import sync_platform_account_graph

    platform = str(account.platform or "").strip()
    email = str(account.email or "").strip()
    with _account_save_lock(platform, email):
        with Session(engine) as session:
            model = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == platform)
                .where(AccountModel.email == email)
            ).first()
            if model is None:
                model = AccountModel(
                    platform=platform,
                    email=email,
                    password=account.password,
                    user_id=account.user_id or "",
                )
                session.add(model)
                try:
                    session.commit()
                    session.refresh(model)
                except IntegrityError:
                    # A second process may have inserted the same identity
                    # after our SELECT.  The database uniqueness constraint is
                    # the final arbiter; reload and continue as an update.
                    session.rollback()
                    model = session.exec(
                        select(AccountModel)
                        .where(AccountModel.platform == platform)
                        .where(AccountModel.email == email)
                    ).first()
                    if model is None:
                        raise

            model.password = account.password
            model.user_id = account.user_id or ""
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)
            sync_platform_account_graph(session, model, account)
            session.commit()
            # ``sync_platform_account_graph`` performs a second commit.  A
            # final refresh keeps returned scalar attributes usable after the
            # session closes instead of returning an expired detached model.
            session.refresh(model)
            return model


LEGACY_ACCOUNT_COLUMNS = (
    "region",
    "token",
    "status",
    "trial_end_time",
    "cashier_url",
    "extra_json",
)


def _load_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_int(value: Any, default: int = 0) -> int:
    """Coerce a value from a legacy row to an int, without trusting its type.

    SQLite column affinity does not enforce a type, so a column declared INTEGER can
    still hold TEXT written by an older version - ``trial_end_time`` holding a date
    string, for example. Legacy migrations exist precisely to cope with shapes the
    current code no longer produces, so this mirrors the adjacent ``_load_json``: never
    raise, fall back to a default.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _accounts_columns() -> set[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "accounts" not in tables:
        return set()
    return {column["name"] for column in inspector.get_columns("accounts")}


def _migrate_legacy_accounts_schema() -> None:
    columns = _accounts_columns()
    if not columns or not any(column in columns for column in LEGACY_ACCOUNT_COLUMNS):
        return

    from core.account_graph import sync_legacy_account_graph

    with engine.begin() as connection:
        rows = connection.exec_driver_sql(
            """
            SELECT
                id,
                platform,
                COALESCE(region, '') AS region,
                COALESCE(token, '') AS token,
                COALESCE(status, 'registered') AS status,
                COALESCE(trial_end_time, 0) AS trial_end_time,
                COALESCE(cashier_url, '') AS cashier_url,
                COALESCE(extra_json, '{}') AS extra_json
            FROM accounts
            """
        ).mappings().all()

    with Session(engine) as session:
        for row in rows:
            sync_legacy_account_graph(
                session,
                account_id=_load_int(row["id"]),
                platform=str(row["platform"] or ""),
                lifecycle_status=str(row["status"] or "registered"),
                region=str(row["region"] or ""),
                legacy_token=str(row["token"] or ""),
                trial_end_time=_load_int(row["trial_end_time"]),
                cashier_url=str(row["cashier_url"] or ""),
                extra=_load_json(str(row["extra_json"] or "{}")),
            )
        session.commit()

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql(
            """
            CREATE TABLE accounts__new (
                id INTEGER NOT NULL PRIMARY KEY,
                platform VARCHAR NOT NULL,
                email VARCHAR NOT NULL,
                password VARCHAR NOT NULL,
                user_id VARCHAR NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO accounts__new (id, platform, email, password, user_id, created_at, updated_at)
            SELECT id, platform, email, password, user_id, created_at, updated_at
            FROM accounts
            """
        )
        connection.exec_driver_sql("DROP TABLE accounts")
        connection.exec_driver_sql("ALTER TABLE accounts__new RENAME TO accounts")
        connection.exec_driver_sql("CREATE INDEX ix_accounts_platform ON accounts (platform)")
        connection.exec_driver_sql("CREATE INDEX ix_accounts_email ON accounts (email)")
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _ensure_accounts_unique_index() -> None:
    """Add the cross-process identity guard without deleting legacy rows.

    Some older databases may already contain duplicate platform/email pairs.
    Those rows are preserved for manual review; in that case the in-process
    keyed lock still prevents new duplicates and startup reports why the
    database-level index could not yet be installed.
    """
    inspector = inspect(engine)
    if "accounts" not in set(inspector.get_table_names()):
        return
    existing = {
        str(item.get("name") or "")
        for item in inspector.get_unique_constraints("accounts")
    }
    existing.update(
        str(item.get("name") or "")
        for item in inspector.get_indexes("accounts")
        if item.get("unique")
    )
    if "uq_accounts_platform_email" in existing:
        return

    with engine.begin() as connection:
        duplicate_groups = int(
            connection.exec_driver_sql(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT platform, email
                    FROM accounts
                    GROUP BY platform, email
                    HAVING COUNT(*) > 1
                ) AS duplicate_accounts
                """
            ).scalar()
            or 0
        )
        if duplicate_groups:
            print(
                "[DB] 检测到旧账号库存在重复 platform/email 组合；"
                "已保留原记录并跳过唯一索引，新的进程内写入仍会串行去重"
            )
            return
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_platform_email "
            "ON accounts (platform, email)"
        )


def init_db():
    with _migration_lock():
        _init_db_locked()


def _init_db_locked():
    SQLModel.metadata.create_all(engine)
    from core.account_graph import sync_all_account_graphs
    from core.config_store import config_store
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    if is_sqlite(DATABASE_URL):
        _migrate_legacy_accounts_schema()
    _ensure_accounts_unique_index()
    _ensure_column("provider_definitions", "category", "TEXT DEFAULT ''")
    _ensure_column("proxies", "url_index", "TEXT DEFAULT ''")
    _ensure_column("tasks", "worker_id", "TEXT DEFAULT ''")
    _ensure_column("tasks", "lease_expires_at", "REAL")
    SQLModel.metadata.create_all(engine)
    secure_sqlite_file_permissions(DATABASE_URL)
    config_store.migrate_plaintext()
    _backfill_proxy_url_index()
    _backfill_registered_email_history()

    with Session(engine) as session:
        ProviderDefinitionsRepository().ensure_seeded()
        _migrate_legacy_provider_keys()
        _cleanup_non_real_providers()
        _cleanup_empty_provider_settings()
        sync_all_account_graphs(session)
        session.commit()


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_COLUMN_TYPE_RE = re.compile(r"[A-Za-z0-9_ ,'()]+\Z")


def _ensure_column(table: str, column: str, col_type: str):
    """给已有表安全地加一列（SQLite 不支持 IF NOT EXISTS ADD COLUMN）。

    SQLite 的 ALTER TABLE ... ADD COLUMN 不接受参数绑定，表名/列名/类型只能拼接，
    因此这里对三者施加白名单校验，杜绝把外部数据拼进 DDL。
    """
    if not _IDENTIFIER_RE.match(table):
        raise ValueError(f"非法表名: {table!r}")
    if not _IDENTIFIER_RE.match(column):
        raise ValueError(f"非法列名: {column!r}")
    if not _COLUMN_TYPE_RE.match(col_type):
        raise ValueError(f"非法列类型: {col_type!r}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if table not in tables:
        return
    existing = {c["name"] for c in inspector.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    print(f"[DB] 已添加列 {table}.{column}")


def _backfill_proxy_url_index() -> None:
    """Populate ``url_index`` for proxy rows written before the blind index existed."""
    inspector = inspect(engine)
    if "proxies" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("proxies")}
    if "url_index" not in columns:
        return
    with Session(engine) as session:
        rows = session.exec(select(ProxyModel)).all()
        updated = 0
        for row in rows:
            raw = str(row.url or "")
            if not raw:
                continue
            expected = blind_index(raw)
            if str(row.url_index or "") != expected:
                row.url_index = expected
                session.add(row)
                updated += 1
        if updated:
            session.commit()
            print(f"[DB] 已回填 {updated} 条代理盲索引")


def _cleanup_empty_provider_settings():
    """清理 v1.0.7/v1.0.8 中 PR #42 自动创建的空 ProviderSetting。

    判定条件：config / auth / metadata 三个字段都为空 dict 时认为
    用户从未编辑过，可以安全删除。被删后用户能从前端"新增"按钮
    重新选择对应的 provider。"""
    with Session(engine) as session:
        items = session.exec(select(ProviderSettingModel)).all()
        removed = 0
        for item in items:
            config = item.get_config() or {}
            auth = item.get_auth() or {}
            metadata = item.get_metadata() or {}
            if not config and not auth and not metadata:
                session.delete(item)
                removed += 1
        if removed:
            session.commit()


# 旧版 provider_key → 新版 provider_key 映射
_LEGACY_PROVIDER_KEY_MAP: dict[tuple[str, str], str] = {
    # mailbox
    ("mailbox", "moemail"): "moemail_api",
    ("mailbox", "generic_http"): "generic_http_mailbox",
    ("mailbox", "tempmail_lol"): "tempmail_lol_api",
    ("mailbox", "tempmail_web"): "tempmail_web_api",
    ("mailbox", "duckmail"): "duckmail_api",
    ("mailbox", "freemail"): "freemail_api",
    ("mailbox", "cfworker"): "cfworker_admin_api",
    ("mailbox", "testmail"): "testmail_api",
    ("mailbox", "laoudo"): "laoudo_api",
    # sms
    ("sms", "sms_activate"): "sms_activate_api",
    ("sms", "herosms"): "herosms_api",
    # captcha
    ("captcha", "yescaptcha"): "yescaptcha_api",
    ("captcha", "twocaptcha"): "twocaptcha_api",
}

# 旧版 auth_mode 值 → 新版 auth_mode 值映射
_LEGACY_AUTH_MODE_MAP: dict[str, str] = {
    "endpoint_only": "password",
    "manual_login": "password",
    "bearer_token": "bearer",
    "jwt_token": "token",
    "admin_token": "token",
    "api_key": "apikey",
}


def _migrate_legacy_provider_keys():
    """将旧版 provider_key 和 auth_mode 迁移到新版命名。

    同时迁移 provider_settings 和 provider_definitions 两张表。
    如果新 key 已存在则删除旧记录（避免唯一约束冲突）。
    迁移后还会修正 auth_mode 值，使其匹配新版 definition 的有效值。
    """
    with Session(engine) as session:
        migrated = 0

        # 1. 迁移 provider_key
        for (ptype, old_key), new_key in _LEGACY_PROVIDER_KEY_MAP.items():
            # --- provider_settings ---
            old_setting = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == ptype)
                .where(ProviderSettingModel.provider_key == old_key)
            ).first()
            if old_setting:
                new_setting = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == ptype)
                    .where(ProviderSettingModel.provider_key == new_key)
                ).first()
                if new_setting:
                    session.delete(old_setting)
                else:
                    old_setting.provider_key = new_key
                    session.add(old_setting)
                migrated += 1

            # --- provider_definitions ---
            old_defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == ptype)
                .where(ProviderDefinitionModel.provider_key == old_key)
            ).first()
            if old_defn:
                new_defn = session.exec(
                    select(ProviderDefinitionModel)
                    .where(ProviderDefinitionModel.provider_type == ptype)
                    .where(ProviderDefinitionModel.provider_key == new_key)
                ).first()
                if new_defn:
                    session.delete(old_defn)
                else:
                    old_defn.provider_key = new_key
                    session.add(old_defn)
                migrated += 1

        if migrated:
            session.commit()
            print(f"[DB] 已迁移 {migrated} 条旧版 provider key")

        # 2. 修正 auth_mode 值
        fixed = 0
        all_settings = session.exec(select(ProviderSettingModel)).all()
        for item in all_settings:
            old_mode = item.auth_mode or ""
            if not old_mode:
                continue
            # 查找对应的 definition
            defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == item.provider_type)
                .where(ProviderDefinitionModel.provider_key == item.provider_key)
            ).first()
            if not defn:
                continue
            valid_modes = {m.get("value") for m in defn.get_auth_modes()}
            if not valid_modes or old_mode in valid_modes:
                # 当前值已经有效，跳过
                continue
            # 尝试映射
            new_mode = _LEGACY_AUTH_MODE_MAP.get(old_mode)
            if new_mode and new_mode in valid_modes:
                item.auth_mode = new_mode
            elif defn.default_auth_mode:
                item.auth_mode = defn.default_auth_mode
            else:
                continue
            session.add(item)
            fixed += 1

        if fixed:
            session.commit()
            print(f"[DB] 已修正 {fixed} 条旧版 auth_mode")


def _cleanup_non_real_providers():
    """generic_http 不是真实邮箱，从 DB 中清除其 definition 和空 setting。"""
    remove_keys = [("mailbox", "generic_http")]
    with Session(engine) as session:
        for pt, pk in remove_keys:
            setting = session.exec(
                select(ProviderSettingModel)
                .where(ProviderSettingModel.provider_type == pt)
                .where(ProviderSettingModel.provider_key == pk)
            ).first()
            if setting:
                config = setting.get_config() or {}
                auth = setting.get_auth() or {}
                if not config and not auth:
                    session.delete(setting)
            defn = session.exec(
                select(ProviderDefinitionModel)
                .where(ProviderDefinitionModel.provider_type == pt)
                .where(ProviderDefinitionModel.provider_key == pk)
            ).first()
            if defn:
                remaining = session.exec(
                    select(ProviderSettingModel)
                    .where(ProviderSettingModel.provider_type == pt)
                    .where(ProviderSettingModel.provider_key == pk)
                ).first()
                if not remaining:
                    session.delete(defn)
        session.commit()


def get_session():
    with Session(engine) as session:
        yield session
