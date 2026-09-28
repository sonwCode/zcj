"""Guard against bare ``except:`` handlers.

A bare ``except:`` catches ``BaseException``, which includes ``KeyboardInterrupt``
and ``SystemExit``. On a long-running worker that means Ctrl-C can be swallowed
mid-block and ``sys.exit()`` inside the ``try`` silently does nothing - both of
which matter for the graceful-shutdown path. There were 25 of them across eight
files; this keeps them from coming back.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_PARTS = {"vendor", "node_modules", ".git", "__pycache__", "customer_portal_web", ".venv"}


def _bare_except_sites() -> list[str]:
    sites: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                sites.append("%s:%d" % (path.relative_to(ROOT), node.lineno))
    return sites


def test_the_scan_actually_covers_the_project():
    """A broken glob would make the real test pass vacuously."""
    files = [
        p
        for p in ROOT.rglob("*.py")
        if not any(part in SKIP_PARTS for part in p.parts)
    ]
    assert len(files) > 200, "expected to scan the whole project, found %d files" % len(files)


def test_no_bare_except_handlers():
    sites = _bare_except_sites()
    assert not sites, (
        "bare `except:` catches KeyboardInterrupt and SystemExit; "
        "use `except Exception:` instead. Found:\n  " + "\n  ".join(sites)
    )
