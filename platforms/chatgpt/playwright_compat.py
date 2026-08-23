"""Playwright/Camoufox compatibility patches.

Camoufox runs through Playwright's Firefox driver.  Some Camoufox/Firefox
events are valid at the browser boundary but older bundled Playwright driver
code assumes fields that may be absent.  Keep these patches at the browser
backend boundary so registration/payment state machines do not need defensive
branches for driver internals.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


_PAGEERROR_LOCATION_REPLACEMENTS = (
    ('url: pageError.location.url,', 'url: pageError.location?.url || "",'),
    ('line: pageError.location.lineNumber,', 'line: pageError.location?.lineNumber || 0,'),
    ('column: pageError.location.columnNumber', 'column: pageError.location?.columnNumber || 0'),
)


def _playwright_core_bundle_path() -> Path:
    import playwright

    return Path(playwright.__file__).resolve().parent / "driver" / "package" / "lib" / "coreBundle.js"


def patch_playwright_firefox_pageerror_location_bug(
    *,
    bundle_path: str | Path | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """Guard Firefox pageerror dispatch when Camoufox omits ``location``.

    Returns ``True`` only when the local Playwright driver bundle was changed.
    Returning ``False`` means the patch was already present, the target pattern
    was not found, or the bundle could not be updated.  Callers intentionally do
    not fail registration on ``False`` because this is a best-effort driver
    compatibility patch.
    """

    log = log_fn or (lambda message: logger.info(message))
    path = Path(bundle_path) if bundle_path is not None else _playwright_core_bundle_path()
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - best-effort compatibility patch
        log(f"Playwright pageerror 热补丁读取失败: {exc}")
        return False

    patched = text
    for old, new in _PAGEERROR_LOCATION_REPLACEMENTS:
        patched = patched.replace(old, new)
    if patched == text:
        return False

    try:
        path.write_text(patched, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - best-effort compatibility patch
        log(f"Playwright pageerror 热补丁写入失败: {exc}")
        return False

    log("已应用 Playwright Firefox pageerror 热补丁")
    return True
