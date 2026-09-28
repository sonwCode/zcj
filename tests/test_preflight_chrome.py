"""The browser check must find the Chromium the deployment image actually installs.

The Dockerfile pins ``PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`` and runs
``playwright install --with-deps chromium``.  Playwright lays its browsers out under a
*versioned* directory - ``chromium-<build>/chrome-linux/chrome`` - so the candidate path
``/ms-playwright/chromium/chrome-linux/chrome`` (no build number) can never exist.

On the project's own image that made ``check_chrome`` report

    浏览器  未找到 Chrome/Chromium

while the browser it installed was sitting right there, telling the operator to fall back
to "playwright's bundled Chromium" - which is exactly what had been installed.
"""
from __future__ import annotations

import os

import pytest

from scripts import cloud_preflight as preflight


def _exec_capable_dir(tmp_path):
    """Return a directory whose files can be executable, preferring one that is.

    ``tmp_path`` is not guaranteed to be executable - the harness mounts ``/tmp``
    ``noexec`` - so fall back to the first standard candidate that works.
    """
    candidates = [tmp_path / "bin", "/var/tmp"]
    for candidate in candidates:
        directory = candidate if candidate == "/var/tmp" else candidate
        try:
            directory = __import__("pathlib").Path(directory)
            probe_dir = directory / ("zcj-exec-probe-%d" % os.getpid())
            probe_dir.mkdir(parents=True, exist_ok=True)
            probe = probe_dir / "probe"
            probe.write_text("")
            probe.chmod(0o755)
            if os.access(str(probe), os.X_OK):
                return probe_dir
        except OSError:
            continue
    return tmp_path / "bin"


def _layout(root, *parts):
    path = os.path.join(str(root), *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("")
    os.chmod(path, 0o755)
    return path


@pytest.fixture(autouse=True)
def _no_system_chrome(monkeypatch):
    """Isolate the playwright branch from whatever the host has installed."""
    monkeypatch.setenv("PATH", "")


@pytest.fixture
def browsers_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    return tmp_path


def test_a_versioned_playwright_chromium_is_found(browsers_dir):
    """This is the layout the shipped Dockerfile produces."""
    built = _layout(browsers_dir, "chromium-1148", "chrome-linux", "chrome")
    report = preflight.Report()

    preflight.check_chrome(report)

    assert report.rows[0][0] == preflight.PASS, report.rows
    assert report.rows[0][2] == built, report.rows


def test_a_headless_shell_install_is_found_too(browsers_dir):
    """Playwright also ships a separate headless shell build."""
    _layout(browsers_dir, "chromium_headless_shell-1148", "chrome-linux", "headless_shell")
    report = preflight.Report()

    preflight.check_chrome(report)

    assert report.rows[0][0] == preflight.PASS, report.rows


def test_the_unversioned_layout_is_still_accepted(browsers_dir):
    """Some images lay the browser out without a build number."""
    _layout(browsers_dir, "chromium", "chrome-linux", "chrome")
    report = preflight.Report()

    preflight.check_chrome(report)

    assert report.rows[0][0] == preflight.PASS, report.rows


def test_no_browser_at_all_is_still_a_warning(browsers_dir):
    """Hardening must not turn a genuinely missing browser into a PASS."""
    report = preflight.Report()

    preflight.check_chrome(report)

    assert report.rows[0][0] == preflight.WARN, report.rows


def test_a_browser_on_path_is_preferred(browsers_dir, tmp_path, monkeypatch):
    """A system Chrome is the cheapest case and must keep working.

    The stub must live on a filesystem that honours the executable bit: pytest's
    ``tmp_path`` is under a ``noexec`` mount in some sandboxes, where ``shutil.which``
    can never find the stub no matter what the code does.
    """
    bindir = _exec_capable_dir(tmp_path)
    chrome = bindir / "google-chrome"
    chrome.write_text("")
    chrome.chmod(0o755)
    if not os.access(str(chrome), os.X_OK):
        pytest.skip("no mount that honours the executable bit is available")
    monkeypatch.setenv("PATH", str(bindir))
    report = preflight.Report()

    preflight.check_chrome(report)

    assert report.rows[0][0] == preflight.PASS, report.rows
    assert report.rows[0][2] == str(chrome), report.rows
