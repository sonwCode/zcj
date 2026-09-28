from __future__ import annotations

import os
import secrets
from pathlib import Path


# 出厂默认值曾随 docker-compose / .env.example 一起发布，任何人都能用它伪造管理员
# JWT 或直接登录，因此这些值一律按“未配置”处理。
_INSECURE_JWT_SECRETS = {"", "change-me-in-production", "changeme", "secret", "your-secret-key"}
_INSECURE_ADMIN_PASSWORDS = {"", "admin123456", "admin", "password", "123456"}


def _database_dir() -> Path:
    """Directory that holds the portal database, and the signing key beside it.

    The fallback is the package directory (the same place ``db.py`` puts
    ``customer_portal.db``), *not* ``Path.cwd()``: the database is anchored to the
    package, so a key that followed the working directory would be written somewhere
    else entirely - and on a read-only or ephemeral cwd it would silently be
    regenerated on every restart, invalidating every token that had been issued.

    The expression is repeated from ``db.default_database_path`` rather than imported:
    importing ``db`` constructs the database engine as a side effect, which would bind
    it to this default before a caller has had a chance to set
    ``PORTAL_DATABASE_URL``. ``test_customer_portal_config_ttl`` asserts the two agree.
    """
    url = os.getenv("PORTAL_DATABASE_URL", "").strip()
    prefix = "sqlite:///"
    if url.startswith(prefix):
        raw = url[len(prefix):]
        if raw and raw != ":memory:":
            return Path(raw).resolve().parent
    return Path(__file__).resolve().parent.parent


def _jwt_secret_file() -> Path:
    return _database_dir() / ".portal_jwt_secret"


def _load_or_create_jwt_secret() -> str:
    """Return a usable signing key that is never the published default.

    Priority: an explicitly configured non-default value, then a random key
    persisted next to the database, then an ephemeral random key.
    """
    configured = os.getenv("PORTAL_JWT_SECRET", "").strip()
    if configured and configured not in _INSECURE_JWT_SECRETS:
        return configured

    path = _jwt_secret_file()
    try:
        if path.is_file():
            persisted = path.read_text(encoding="utf-8").strip()
            if persisted and persisted not in _INSECURE_JWT_SECRETS:
                print("[portal] PORTAL_JWT_SECRET 未配置，使用持久密钥 " + str(path))
                return persisted
        generated = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print(
            "[portal][WARN] PORTAL_JWT_SECRET 未配置或仍是出厂默认值，"
            "已生成随机签名密钥并保存到 " + str(path)
        )
        return generated
    except OSError as exc:
        print(
            "[portal][WARN] 无法持久化 JWT 签名密钥（" + str(exc) + "），"
            "本次进程使用临时随机密钥，重启后既有 token 会失效"
        )
        return secrets.token_urlsafe(48)


def _env_int(name: str, default: int) -> int:
    """Read an integer setting, falling back to ``default`` when it is malformed.

    These are documented operator settings (``docs/configuration.md``, the compose
    file, ``.env.example``), and this module is imported once when the process starts
    - the class body below evaluates at import time. Parsing them with a bare
    ``int(os.getenv(...))`` therefore meant a single typo took the whole portal down:
    ``PORTAL_ACCESS_TOKEN_TTL_SECONDS=2h`` (or ``7200s``, ``1.5``, or an empty
    substitution) raised ``ValueError: invalid literal for int()`` while uvicorn was
    still importing, and the traceback named the symptom but never the variable.

    A malformed value is reported loudly and the documented default is used, matching
    the existing ``[portal][WARN]`` handling for the JWT secret. None of these
    settings is security-sensitive, so the fallback is simply the published default.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    text = raw.strip()
    if not text:
        print(f"[portal][WARN] {name}={raw!r} 不是有效整数，使用默认值 {default}")
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        print(f"[portal][WARN] {name}={raw!r} 不是有效整数，使用默认值 {default}")
        return default


def resolve_seed_admin_password() -> tuple[str, bool]:
    """Return (password, generated) for the bootstrap admin account.

    A published default password counts as "not configured", so a fresh
    deployment never comes up as admin/admin123456.
    """
    configured = os.getenv("PORTAL_ADMIN_PASSWORD", "").strip()
    if configured and configured not in _INSECURE_ADMIN_PASSWORDS:
        return configured, False
    return secrets.token_urlsafe(18), True


class Settings:
    app_name: str = os.getenv("PORTAL_APP_NAME", "Customer Portal API")
    app_version: str = "0.2.0"
    jwt_secret: str = _load_or_create_jwt_secret()
    access_token_ttl_seconds: int = _env_int("PORTAL_ACCESS_TOKEN_TTL_SECONDS", 7200)
    refresh_token_ttl_seconds: int = _env_int(
        "PORTAL_REFRESH_TOKEN_TTL_SECONDS", 30 * 24 * 3600
    )
    seed_admin_username: str = os.getenv("PORTAL_ADMIN_USERNAME", "admin")
    seed_admin_email: str = os.getenv("PORTAL_ADMIN_EMAIL", "admin@example.com")
    cors_origins: list[str] = [item.strip() for item in os.getenv("PORTAL_CORS_ORIGINS", "").split(",") if item.strip()]


settings = Settings()
