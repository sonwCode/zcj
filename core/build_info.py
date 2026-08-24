"""Public-safe runtime build identity and process-start metadata."""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from core import version as version_module


_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _public_text(value: object, *, max_length: int = 100) -> str:
    """Keep build metadata one-line and bounded before exposing it via the API."""
    return re.sub(r"[\r\n\x00-\x1f]+", "", str(value or "")).strip()[:max_length]


def _git_sha_from_checkout() -> str:
    """Resolve a commit without returning paths, command errors, or repository data."""
    try:
        project_root = Path(__file__).resolve().parent.parent
        value = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(project_root),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).strip()
    except Exception:
        return ""
    return value if _SHA_PATTERN.fullmatch(value) else ""


def get_build_info() -> dict[str, str]:
    """Return only non-secret identity fields suitable for logs and public UI."""
    embedded_sha = _public_text(getattr(version_module, "__git_sha__", ""), max_length=40)
    environment_sha = _public_text(os.environ.get("APP_GIT_SHA", ""), max_length=40)
    git_sha = environment_sha or embedded_sha
    if not _SHA_PATTERN.fullmatch(git_sha):
        git_sha = _git_sha_from_checkout() or "unknown"

    build_time = _public_text(
        os.environ.get("APP_BUILD_TIME")
        or getattr(version_module, "__build_time__", ""),
        max_length=80,
    )
    return {
        "version": _public_text(getattr(version_module, "__version__", "dev")) or "dev",
        "git_sha": git_sha,
        "build_time": build_time or "unknown",
        "started_at": _PROCESS_STARTED_AT,
    }


def build_identity() -> str:
    info = get_build_info()
    return f"{info['version']}@{info['git_sha']}"
