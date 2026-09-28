"""The dependency check must cover what the application needs to start.

``check_dependencies`` required fastapi / sqlmodel / sqlalchemy / curl_cffi.  Two of those
are right, but the list did not match the startup graph: ``services/solver_manager.py``
imports ``requests`` at module scope and ``main.py`` pulls it in while starting up, so an
image without ``requests`` would fail to boot while the preflight reported PASS.

``curl_cffi`` is on the same startup path, by a longer route: ``main.py`` imports ``api.accounts``,
which instantiates ``CtfPlusAccountsService`` at module scope, which imports ``platforms.chatgpt.oauth``,
which does ``from curl_cffi import requests``.  ``core.http_client`` imports it at module scope too.
So it is a boot dependency just like ``requests``, not a lazy one - leaving it out of the list
would let an image that cannot boot report PASS.  (The earlier wording here called it lazy; that was
wrong, and it would have invited someone to trim it from the list.)
"""
from __future__ import annotations

import builtins
import importlib.util

import pytest

from scripts import cloud_preflight as preflight


# Every one of these is imported at module scope somewhere on the startup path.
STARTUP_PACKAGES = ("fastapi", "sqlmodel", "sqlalchemy", "requests", "pydantic")


def _row(report):
    rows = [row for row in report.rows if row[1] == "核心依赖"]
    assert rows, report.rows
    return rows[0]


def test_the_shipped_environment_passes() -> None:
    report = preflight.Report()

    preflight.check_dependencies(report)

    assert _row(report)[0] == preflight.PASS, _row(report)


@pytest.mark.parametrize("missing", STARTUP_PACKAGES)
def test_a_missing_startup_dependency_is_a_failure(monkeypatch, missing) -> None:
    """Blocking the import is exactly what a stripped image looks like."""
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == missing or name.startswith(missing + "."):
            raise ImportError("simulated missing " + missing)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    report = preflight.Report()

    preflight.check_dependencies(report)

    row = _row(report)
    assert row[0] == preflight.FAIL, row
    assert missing in row[2], row


def test_the_required_list_names_the_startup_dependencies() -> None:
    """A regression guard on the list itself, independent of the import probe."""
    required = set(preflight.REQUIRED_IMPORTS)

    missing = sorted(set(STARTUP_PACKAGES) - required)

    assert not missing, "startup dependencies not checked: %s" % missing


def test_curl_cffi_stays_on_the_required_list() -> None:
    """It is reachable from main.py in five hops, so it is a boot dependency.

    ``STARTUP_PACKAGES`` cannot guard this one: the check above is a subset test, and
    ``curl_cffi`` is deliberately not in that tuple, so dropping it from
    ``REQUIRED_IMPORTS`` would pass every other test in this file.  Pin it here so the
    list cannot be trimmed back to the fastapi / sqlmodel / sqlalchemy trio.
    """
    assert "curl_cffi" in preflight.REQUIRED_IMPORTS


def _playwright_row(report):
    rows = [row for row in report.rows if row[1] == "playwright"]
    assert rows, report.rows
    return rows[0]


def test_a_missing_playwright_warns_instead_of_failing(monkeypatch) -> None:
    """Playwright is browser-path only, so its absence must not block a protocol deploy.

    The two outcomes are deliberately different: a missing startup package is a FAIL, a
    missing playwright is a WARN.  Collapsing that distinction either way would be wrong -
    failing a protocol-only host, or waving through a browser host that cannot start.
    """
    real_find_spec = importlib.util.find_spec

    def _no_playwright(name, *args, **kwargs):
        if name == "playwright":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _no_playwright)
    report = preflight.Report()

    preflight.check_dependencies(report)

    assert _row(report)[0] == preflight.PASS, "startup deps are present"
    row = _playwright_row(report)
    assert row[0] == preflight.WARN, row
    assert row[3], "a warning has to explain when it can be ignored"


def test_playwright_present_passes(monkeypatch) -> None:
    """When it is installed the row is a PASS, not a warning to be ignored."""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    report = preflight.Report()

    preflight.check_dependencies(report)

    assert _playwright_row(report)[0] == preflight.PASS
