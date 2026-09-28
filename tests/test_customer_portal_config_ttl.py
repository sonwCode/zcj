r"""Portal integer settings must survive a malformed value.

``Settings`` parses PostgreSQL-style TTL settings from the environment in its *class
body*, and the module ends with ``settings = Settings()``. That body therefore runs at
import time, before the application exists. A bare ``int(os.getenv(...))`` there meant a
single typo in a documented operator setting crashed the whole portal while uvicorn was
still importing it:

    PORTAL_ACCESS_TOKEN_TTL_SECONDS=2h

gave ``ValueError: invalid literal for int() with base 10: '2h'`` - a traceback that
named the symptom but never the variable, and took down every route with it.

The bar is therefore higher than "the helper works": reverting the class body to an
inline ``int(os.getenv(...))`` while leaving ``_env_int`` intact would pass a test that
only exercises the helper. The subprocess cases below import the real module under a bad
environment, which is the only way to pin the property that actually broke.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from customer_portal_api.app.config import _env_int


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCESS_VAR = "PORTAL_ACCESS_TOKEN_TTL_SECONDS"
REFRESH_VAR = "PORTAL_REFRESH_TOKEN_TTL_SECONDS"
ACCESS_DEFAULT = 7200
REFRESH_DEFAULT = 30 * 24 * 3600


# Every one of these aborted the import before the fix.
MALFORMED = ["2h", "7200s", "1.5", "1e5", "0x10", "12 34", "", "  ", "abc", "7200.0"]


@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_value_falls_back_to_the_default(monkeypatch, capsys, value):
    monkeypatch.setenv(ACCESS_VAR, value)

    assert _env_int(ACCESS_VAR, ACCESS_DEFAULT) == ACCESS_DEFAULT


@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_value_is_reported_with_its_variable_name(monkeypatch, capsys, value):
    """The old traceback never said which setting was wrong; the warning must."""
    monkeypatch.setenv(ACCESS_VAR, value)

    _env_int(ACCESS_VAR, ACCESS_DEFAULT)

    warning = capsys.readouterr().out
    assert ACCESS_VAR in warning
    assert "[portal][WARN]" in warning


def test_an_unset_variable_returns_the_default_silently(monkeypatch, capsys):
    """Absent is normal, not a misconfiguration - no warning should be emitted."""
    monkeypatch.delenv(ACCESS_VAR, raising=False)

    assert _env_int(ACCESS_VAR, ACCESS_DEFAULT) == ACCESS_DEFAULT
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "value,expected",
    [
        ("7200", 7200),
        ("  7200  ", 7200),   # surrounding whitespace is tolerated
        ("0", 0),
        ("2592000", 2592000),
        ("-1", -1),           # a negative TTL is nonsense but not a crash
    ],
)
def test_a_valid_value_is_parsed(monkeypatch, value, expected):
    monkeypatch.setenv(ACCESS_VAR, value)

    assert _env_int(ACCESS_VAR, ACCESS_DEFAULT) == expected


def _import_settings(environ_extra: dict, tmp_path) -> subprocess.CompletedProcess:
    """Import the real module in a fresh interpreter with a custom environment.

    ``cwd`` is a temp directory rather than the repo: importing the module also runs
    ``_load_or_create_jwt_secret()``, which persists a ``.portal_jwt_secret`` next to
    the working directory. Running in the repo would leave that file behind, and it is
    not gitignored.
    """
    env = dict(os.environ)
    env.pop(ACCESS_VAR, None)
    env.pop(REFRESH_VAR, None)
    env.update(environ_extra)
    env["PYTHONPATH"] = REPO
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from customer_portal_api.app.config import settings"
            " as s; print(s.access_token_ttl_seconds, s.refresh_token_ttl_seconds)",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _parsed(result: subprocess.CompletedProcess) -> list[str]:
    """Return the two TTLs from the last stdout line.

    Importing the module also emits unrelated notices (the JWT secret banner), so the
    line printed by the probe is the last non-empty one.
    """
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"no stdout: {result.stdout!r} / stderr: {result.stderr!r}"
    return lines[-1].split()


@pytest.mark.parametrize("value", MALFORMED)
def test_the_module_still_imports_with_a_malformed_value(value, tmp_path):
    """The property that actually broke: importing must not raise.

    This drives the real ``Settings`` class body, so reverting it to an inline
    ``int(os.getenv(...))`` fails here even though ``_env_int`` is untouched.
    """
    result = _import_settings({ACCESS_VAR: value}, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _parsed(result) == [str(ACCESS_DEFAULT), str(REFRESH_DEFAULT)]


def test_the_module_imports_with_a_malformed_refresh_ttl_too(tmp_path):
    result = _import_settings({REFRESH_VAR: "30d"}, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _parsed(result) == [str(ACCESS_DEFAULT), str(REFRESH_DEFAULT)]


def test_the_module_still_honours_a_valid_value(tmp_path):
    """Hardening must not stop a correctly configured deployment from applying it."""
    result = _import_settings({ACCESS_VAR: "600", REFRESH_VAR: "1200"}, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _parsed(result) == ["600", "1200"]


# --- where the signing key is kept -------------------------------------------


def test_the_secret_is_kept_next_to_the_database_even_from_another_cwd(tmp_path):
    """The key must follow the database, not the process working directory.

    ``db.py`` anchors the portal database to the source tree, but ``config._database_dir``
    used to return ``Path.cwd()`` when ``PORTAL_DATABASE_URL`` was unset. Under a cloud
    deployment (systemd/docker start the process somewhere else) the database stayed in
    the source tree while the signing key was created in the working directory - and on
    a read-only or ephemeral cwd the key silently became a fresh random value on every
    restart, invalidating every issued token.
    """
    env = dict(os.environ)
    env.pop("PORTAL_DATABASE_URL", None)
    env.pop("PORTAL_JWT_SECRET", None)
    env["PYTHONPATH"] = REPO
    probe = ";".join(
        [
            "import os, pathlib",
            "from customer_portal_api.app import db as d",
            "from customer_portal_api.app import config as c",
            "db_dir = pathlib.Path(d.DATABASE_URL[len('sqlite:///'):]).resolve().parent",
            "print(str(db_dir), str(c._database_dir().resolve()))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    db_dir, secret_dir = lines[-1].split()
    assert secret_dir == db_dir


def test_the_database_path_is_anchored_to_the_package_not_the_cwd(tmp_path):
    """The premise the key placement depends on.

    ``config._database_dir`` follows the database, so the database itself must not move
    with the working directory. If ``default_database_path`` ever became cwd-relative the
    key would follow it straight back to the ephemeral directory.
    """
    env = dict(os.environ)
    env.pop("PORTAL_DATABASE_URL", None)
    env.pop("PORTAL_JWT_SECRET", None)
    env["PYTHONPATH"] = REPO
    probe = ";".join(
        [
            "import os, pathlib",
            "from customer_portal_api.app import db as d",
            "print(str(d.default_database_path().resolve().parent))",
        ]
    )
    seen = []
    for cwd in (tmp_path, REPO):
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        seen.append(lines[-1].strip())

    assert seen[0] == seen[1], f"database path moved with cwd: {seen}"
