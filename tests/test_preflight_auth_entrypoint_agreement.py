"""The entrypoint gate and the preflight report must describe the same rule.

``check_auth`` calls itself a mirror of what ``docker-entrypoint.sh`` enforces, and the
entrypoint runs the preflight precisely so an operator can read why it refused to start.
That agreement is not cosmetic: ``AuthMiddleware`` reads ``APP_PASSWORD`` through
``.strip()`` (``core/auth.py``), so a whitespace-only value means *authentication is
off*, while a bare ``[ -z "${APP_PASSWORD:-}" ]`` in the shell sees it as set and starts
the container on the default public bind.  The gate passed a configuration it should
have killed, and the report said nothing.

These tests run the *shipped* guard - the real ``strip``/``log``/``fatal`` definitions,
the real ``APP_HOST`` default and the real auth section, sliced out of
``docker-entrypoint.sh`` at runtime - rather than a copy kept here.  A copy would drift
in exactly the way the guard drifted from the application, so testing one would prove
nothing about the other.
"""
from __future__ import annotations

import itertools
import os
import shutil
import subprocess
from unittest import mock

import pytest

from scripts import cloud_preflight as preflight


ENTRYPOINT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker-entrypoint.sh"
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="the entrypoint is a bash script"
)


def _lines():
    with open(ENTRYPOINT, encoding="utf-8") as handle:
        return handle.read().split("\n")


def _find(lines, predicate, what):
    for index, line in enumerate(lines):
        if predicate(line):
            return index
    raise AssertionError("marker not found in the entrypoint: " + what)


def _fn(lines, start, what):
    """The whole definition, whether it is written on one line or several."""
    index = _find(lines, lambda line: line.startswith(start), what)
    if lines[index].rstrip().endswith("}"):
        return [lines[index]]
    block = [lines[index]]
    cursor = index + 1
    while cursor < len(lines) and lines[cursor].strip() != "}":
        block.append(lines[cursor])
        cursor += 1
    assert cursor < len(lines), "unterminated definition: " + what
    block.append(lines[cursor])
    return block


def _auth_guard():
    """The shipped guard, assembled from the file itself."""
    lines = _lines()
    auth = _find(lines, lambda l: l.startswith("# --- 鉴权"), "auth section")
    xvfb = _find(lines, lambda l: l.startswith("# --- 虚拟显示"), "display section")
    host = _find(lines, lambda l: l.startswith("APP_HOST="), "APP_HOST default")
    return "\n".join(
        ["set -euo pipefail"]
        + _fn(lines, "strip() {", "strip")
        + _fn(lines, "log() {", "log")
        + _fn(lines, "fatal() {", "fatal")
        + [lines[host]]
        + lines[auth:xvfb]
    )


HARNESS = _auth_guard()

PASSWORDS = (None, "", "   ", "\t", "secret")
INSECURE = (None, "1", " 1")
HOSTS = (None, "0.0.0.0", "127.0.0.1", "localhost", "::1", " 127.0.0.1 ", "LOCALHOST")


def _entrypoint_verdict(password, insecure, host):
    """Run the real guard and report what the container would do."""
    environment = dict(os.environ)
    for key in ("APP_PASSWORD", "ZCJ_ALLOW_INSECURE", "APP_HOST"):
        environment.pop(key, None)
    if password is not None:
        environment["APP_PASSWORD"] = password
    if insecure is not None:
        environment["ZCJ_ALLOW_INSECURE"] = insecure
    if host is not None:
        environment["APP_HOST"] = host
    completed = subprocess.run(
        ["bash", "-c", HARNESS], capture_output=True, text=True, env=environment,
    )
    if completed.returncode != 0 or "FATAL" in completed.stderr:
        return "FATAL"
    if "[WARN]" in completed.stdout:
        return "WARN"
    return "PASS"


def _preflight_verdict(password, insecure, host):
    """check_auth's verdict, with the process environment restored afterwards.

    These three names are process-global.  Setting APP_PASSWORD and walking away turns
    AuthMiddleware on for every test that runs later, which surfaces as unrelated 401s
    in whatever file happens to be collected next.  patch.dict puts the environment
    back on the way out, so this helper cannot poison its neighbours.
    """
    wanted = {}
    if password is not None:
        wanted["APP_PASSWORD"] = password
    if insecure is not None:
        wanted["ZCJ_ALLOW_INSECURE"] = insecure
    if host is not None:
        wanted["APP_HOST"] = host
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in ("APP_PASSWORD", "ZCJ_ALLOW_INSECURE", "APP_HOST"):
            os.environ.pop(key, None)
        os.environ.update(wanted)
        report = preflight.Report()
        preflight.check_auth(report)
        return report.rows[0][0]


MAP = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FATAL"}


def test_the_guard_slices_out_of_the_real_file():
    """Guards the extraction itself: a moved marker must not silently empty it."""
    assert "APP_PASSWORD" in HARNESS
    assert HARNESS.count("strip") >= 3, HARNESS
    assert "::1" in HARNESS


def test_a_whitespace_only_password_is_not_a_password():
    """The defect this pins: the app strips, so a blank value means auth is off."""
    assert _entrypoint_verdict("   ", None, None) == "FATAL"
    assert _preflight_verdict("   ", None, None) == "FAIL"


@pytest.mark.parametrize("password,insecure,host", [
    combo
    for combo in itertools.product(PASSWORDS, INSECURE, HOSTS)
    if not (combo[0] is None or (isinstance(combo[0], str) and combo[0].strip()))
        and not (combo[1] if combo[1] is not None else "").strip() == "1"
        and (combo[2] or "0.0.0.0").strip() not in ("127.0.0.1", "localhost", "::1")
])
def test_the_two_rules_agree_on_dangerous_configurations(password, insecure, host):
    """Every way of getting it wrong must be described the same way by both."""
    assert _entrypoint_verdict(password, insecure, host) == "FATAL"
    assert _preflight_verdict(password, insecure, host) == "FAIL"


def test_the_two_rules_agree_everywhere():
    """The full matrix, so drift in either direction fails here rather than in prod."""
    disagreements = []
    for password, insecure, host in itertools.product(PASSWORDS, INSECURE, HOSTS):
        entry = _entrypoint_verdict(password, insecure, host)
        report = MAP[_preflight_verdict(password, insecure, host)]
        if entry != report:
            disagreements.append(
                "APP_PASSWORD=%r ZCJ_ALLOW_INSECURE=%r APP_HOST=%r: entrypoint=%s preflight=%s"
                % (password, insecure, host, entry, report)
            )
    assert disagreements == [], disagreements
