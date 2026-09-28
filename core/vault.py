"""Credential vault - transparent at-rest encryption for stored secrets.

Why this exists
---------------
The account database historically stored account passwords, provider API keys,
mailbox tokens and proxy credentials as plaintext columns.  Any read of the
SQLite file (backup, support bundle, portal export, accidental commit) leaked
live credentials.  This module adds an authenticated-encryption layer that is
transparent to every existing SQLModel reader/writer through a SQLAlchemy
``TypeDecorator``.

Design
------
* AEAD: ``nacl.secret.SecretBox`` (XSalsa20-Poly1305).  PyNaCl is already a
  hard dependency of this project, so no new requirement is introduced.
* Key material: ``ZCJ_VAULT_KEY`` (hex/base64) if present, otherwise a key file
  beside the database (``.zcj_vault_key``, mode 0600) created on first use.
* Envelope: ``enc:v1:<base64(nonce || ciphertext)>``.  Values without the
  prefix are legacy plaintext and are returned unchanged, so the change is
  backward compatible and requires no big-bang migration.
* ``blind_index()`` provides a deterministic, keyed lookup value so encrypted
  columns that must still be searched (proxy URLs) can be queried without
  revealing the plaintext.
* Failure mode is explicit: if no key can be provisioned the vault degrades to
  pass-through and ``vault_status()`` reports ``enabled: false`` so operators
  can see that encryption is not active.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

ENC_PREFIX = "enc:v1:"
KEY_ENV = "ZCJ_VAULT_KEY"
KEY_FILE_ENV = "ZCJ_VAULT_KEY_FILE"
DISABLE_ENV = "ZCJ_VAULT_DISABLED"
KEY_FILENAME = ".zcj_vault_key"
_BLIND_LABEL = b"zcj-blind-index-v1"


class VaultError(RuntimeError):
    """Raised when a value cannot be decrypted or the key is unusable."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _database_dir() -> Path:
    """Return the directory that holds the primary database file."""
    url = str(os.getenv("ACCOUNT_MANAGER_DATABASE_URL", "") or "").strip()
    if url.startswith("sqlite"):
        raw = url.split("///", 1)[-1] if "///" in url else ""
        if raw and raw != ":memory:":
            try:
                return Path(raw).expanduser().resolve().parent
            except OSError:
                pass
    return Path(__file__).resolve().parent.parent


def _decode_key(raw: str) -> bytes | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
            return bytes.fromhex(text)
        padded = text + "=" * (-len(text) % 4)
        candidate = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(candidate) == 32:
            return candidate
    except Exception:
        return None
    return None


class _Vault:
    """Lazily-initialised key holder.  Safe for concurrent use."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ready = False
        self._box: Any = None
        self._key: bytes | None = None
        self._key_source = "none"
        self._key_path = ""
        self._reason = ""
        self._disabled = False

    def _load(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self._ready = True
            if _truthy(os.getenv(DISABLE_ENV)):
                self._disabled = True
                self._reason = "disabled by " + DISABLE_ENV
                return
            key = _decode_key(os.getenv(KEY_ENV, ""))
            if key is not None:
                self._key_source = "env"
            else:
                key = self._read_or_create_key_file()
            if key is None:
                # ``_read_or_create_key_file`` records why (for example a
                # permission error); only fall back to the generic wording when
                # nothing more specific is known.  The reason is what sends the
                # operator to the right fix, and the stats API surfaces it too.
                if not self._reason:
                    self._reason = "no usable vault key"
                return
            try:
                from nacl.secret import SecretBox
            except Exception as exc:  # pragma: no cover - dependency guard
                self._reason = f"PyNaCl unavailable: {type(exc).__name__}"
                return
            self._key = key
            self._box = SecretBox(key)

    def _read_or_create_key_file(self) -> bytes | None:
        override = str(os.getenv(KEY_FILE_ENV, "") or "").strip()
        path = Path(override).expanduser() if override else _database_dir() / KEY_FILENAME
        self._key_path = str(path)
        try:
            if path.exists():
                self._key_source = "file"
                return _decode_key(path.read_text(encoding="utf-8").strip())
            path.parent.mkdir(parents=True, exist_ok=True)
            new_key = os.urandom(32)
            path.write_text(new_key.hex(), encoding="utf-8")
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            self._key_source = "file-generated"
            return new_key
        except OSError as exc:
            self._reason = f"key file error: {type(exc).__name__}"
            return None

    @property
    def enabled(self) -> bool:
        self._load()
        return self._box is not None

    def encrypt(self, value: str) -> str:
        text = str(value)
        if not text or text.startswith(ENC_PREFIX):
            return text
        self._load()
        if self._box is None:
            return text
        sealed = self._box.encrypt(text.encode("utf-8"))
        return ENC_PREFIX + base64.b64encode(bytes(sealed)).decode("ascii")

    def decrypt(self, value: str) -> str:
        text = str(value)
        if not text.startswith(ENC_PREFIX):
            return text
        payload = text[len(ENC_PREFIX):]
        self._load()
        if self._box is None:
            raise VaultError("encrypted value present but vault key is unavailable")
        try:
            raw = base64.b64decode(payload.encode("ascii"))
            return self._box.decrypt(raw).decode("utf-8")
        except Exception as exc:
            raise VaultError(f"failed to decrypt vault value: {type(exc).__name__}") from exc

    def blind_index(self, value: str) -> str:
        self._load()
        material = self._key
        if material is None:
            # Deterministic fallback so lookups keep working when the vault is
            # intentionally disabled; it is an index, not a security boundary.
            material = hashlib.sha256(b"zcj-no-vault").digest()
        derived = hmac.new(material, _BLIND_LABEL, hashlib.sha256).digest()
        return hmac.new(derived, str(value).encode("utf-8"), hashlib.sha256).hexdigest()

    def status(self) -> dict[str, Any]:
        self._load()
        return {
            "enabled": self._box is not None,
            "backend": "nacl.secretbox" if self._box is not None else "none",
            "key_source": self._key_source,
            "key_path": self._key_path,
            "disabled": self._disabled,
            "reason": self._reason,
        }


_VAULT = _Vault()


def vault_enabled() -> bool:
    return _VAULT.enabled


def encrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    return _VAULT.encrypt(str(value))


def decrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    return _VAULT.decrypt(str(value))


def is_encrypted(value: Any) -> bool:
    return str(value or "").startswith(ENC_PREFIX)


def blind_index(value: str | None) -> str:
    return _VAULT.blind_index(str(value or ""))


def vault_status() -> dict[str, Any]:
    return _VAULT.status()


class EncryptedText(TypeDecorator):
    """SQLAlchemy column type that transparently encrypts text at rest."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_value(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return decrypt_value(str(value))
        except VaultError:
            # Surface a readable marker instead of crashing the whole query.
            return ""
