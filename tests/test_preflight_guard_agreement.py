"""The entrypoint's worker and VNC gates must agree with the preflight report.

``check_single_process`` and ``check_vnc`` describe what the entrypoint will do; an
operator reads the report to find out why a container refused to start, or whether it
went up in a state worth worrying about.  Both read their values with ``.strip()`` on
the Python side, so the shell side has to strip too - a bare ``[ -n ]`` sees ``" 1"``
as set (one worker, rejected), and a bare ``[ -z ]`` sees ``"   "`` as a password.

As in the auth agreement test, the harness is sliced out of the *shipped*
``docker-entrypoint.sh`` at runtime rather than copied here: a copy would drift in
exactly the way these gates drifted from the application.
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


def _prelude(lines):
    return (
        ["set -euo pipefail"]
        + _fn(lines, "strip() {", "strip")
        + _fn(lines, "log() {", "log")
        + _fn(lines, "fatal() {", "fatal")
    )


def _slice(lines, start_marker, end_marker):
    start = _find(lines, lambda line: line.startswith(start_marker), start_marker)
    end = _find(lines, lambda line: line.startswith(end_marker), end_marker)
    assert end > start, start_marker
    return lines[start:end]


def _workers_harness():
    lines = _lines()
    return "\n".join(_prelude(lines) + _slice(lines, "# --- 单进程约束", "# --- 鉴权"))


def _vnc_harness():
    lines = _lines()
    stubs = [
        "x11vnc() { :; }",
        "websockify() { :; }",
        "mktemp() { echo /tmp/stub; }",
        "chmod() { :; }",
        "XVFB_DISPLAY=:99",
    ]
    return "\n".join(
        _prelude(lines) + stubs + _slice(lines, "# --- VNC", "# --- 部署前预检")
    )


WORKERS_HARNESS = _workers_harness()
VNC_HARNESS = _vnc_harness()

WORKER_VALUES = (None, "", "1", "2", " 1", "  ", "\t1", "01")
VNC_ENABLED = (None, "0", "1", " 1")
VNC_PASSWORDS = (None, "", "   ", "pw")
VNC_INSECURE = (None, "1", " 1")
VNC_BINDS = (None, "127.0.0.1", "localhost", "0.0.0.0", " 127.0.0.1 ")
VNC_KEYS = ("VNC_ENABLED", "VNC_PASSWORD", "ZCJ_ALLOW_INSECURE_VNC", "VNC_BIND")


def _run(harness, wanted, keys):
    environment = dict(os.environ)
    for key in keys:
        environment.pop(key, None)
    environment.update(wanted)
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, env=environment
    )


def _entrypoint_workers(value):
    wanted = {} if value is None else {"UVICORN_WORKERS": value}
    completed = _run(WORKERS_HARNESS, wanted, ("UVICORN_WORKERS",))
    if completed.returncode != 0 or "FATAL" in completed.stderr:
        return "FAIL"
    return "PASS"


def _preflight_workers(value):
    wanted = {} if value is None else {"UVICORN_WORKERS": value}
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("UVICORN_WORKERS", None)
        os.environ.update(wanted)
        report = preflight.Report()
        preflight.check_single_process(report)
        return report.rows[0][0]


def _entrypoint_vnc(enabled, password, insecure, bind):
    wanted = {}
    if enabled is not None:
        wanted["VNC_ENABLED"] = enabled
    if password is not None:
        wanted["VNC_PASSWORD"] = password
    if insecure is not None:
        wanted["ZCJ_ALLOW_INSECURE_VNC"] = insecure
    if bind is not None:
        wanted["VNC_BIND"] = bind
    completed = _run(VNC_HARNESS, wanted, VNC_KEYS)
    if (enabled or "0").strip() != "1":
        return "INFO"
    if completed.returncode != 0 or "FATAL" in completed.stderr:
        return "FAIL"
    if "[WARN]" in completed.stdout:
        return "WARN"
    return "PASS"


def _preflight_vnc(enabled, password, insecure, bind):
    wanted = {}
    if enabled is not None:
        wanted["VNC_ENABLED"] = enabled
    if password is not None:
        wanted["VNC_PASSWORD"] = password
    if insecure is not None:
        wanted["ZCJ_ALLOW_INSECURE_VNC"] = insecure
    if bind is not None:
        wanted["VNC_BIND"] = bind
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in VNC_KEYS:
            os.environ.pop(key, None)
        os.environ.update(wanted)
        report = preflight.Report()
        preflight.check_vnc(report)
        return report.rows[0][0]


def test_the_harnesses_slice_out_of_the_real_file():
    """Guards the extraction itself: moved markers must not yield an empty gate."""
    assert "UVICORN_WORKERS" in WORKERS_HARNESS
    assert "VNC_BIND" in VNC_HARNESS
    assert "websockify" in VNC_HARNESS
    assert "fatal" in WORKERS_HARNESS


@pytest.mark.parametrize("value", WORKER_VALUES)
def test_the_worker_gates_agree(value):
    """One worker means one worker, even with stray whitespace around it."""
    assert _entrypoint_workers(value) == _preflight_workers(value)


def test_a_whitespace_worker_count_is_still_one_worker():
    """The case that started this: " 1" is one worker, not a multi-worker error."""
    assert _entrypoint_workers(" 1") == "PASS"
    assert _preflight_workers(" 1") == "PASS"


@pytest.mark.parametrize(
    "enabled,password,insecure,bind",
    list(itertools.product(VNC_ENABLED, VNC_PASSWORDS, VNC_INSECURE, VNC_BINDS)),
)
def test_the_vnc_gates_agree(enabled, password, insecure, bind):
    """Every VNC configuration must be described the same way by both gates."""
    assert _entrypoint_vnc(enabled, password, insecure, bind) == _preflight_vnc(
        enabled, password, insecure, bind
    )


def test_vnc_without_a_password_is_never_a_clean_pass():
    """The escape hatch downgrades to a warning; it must not read as PASS."""
    assert _preflight_vnc("1", "", "1", None) == "WARN"
    assert _entrypoint_vnc("1", "", "1", None) == "WARN"


def test_a_public_vnc_bind_is_warned_about_on_both_sides():
    assert _preflight_vnc("1", "pw", None, "0.0.0.0") == "WARN"
    assert _entrypoint_vnc("1", "pw", None, "0.0.0.0") == "WARN"
