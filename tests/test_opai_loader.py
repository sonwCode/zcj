"""The GoPay protocol SDK is not shipped in this repository.

``.gitignore`` excludes ``platforms/gopay-deploy/``, so a fresh clone has no
``opai`` package. The loader used to swallow that and let the caller fail a few
frames later with a bare ``No module named 'opai'``, which told the reader
nothing about where the SDK was supposed to be. These tests pin the clearer
failure and the two legitimate ways the SDK can be present.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from platforms.gopay import _opai_loader


@pytest.fixture(autouse=True)
def _reset_loader_state():
    """The loader caches success in a module global; undo that between tests."""
    saved_path = list(sys.path)
    _opai_loader._LOADED = False
    yield
    _opai_loader._LOADED = False
    sys.path[:] = saved_path


def test_missing_sdk_raises_with_the_expected_path(monkeypatch, tmp_path):
    missing = tmp_path / "gopay-deploy" / "app" / "src"
    monkeypatch.setattr(_opai_loader, "_opai_src_dir", lambda: str(missing))
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    with pytest.raises(RuntimeError) as excinfo:
        _opai_loader.ensure_opai_on_path()

    message = str(excinfo.value)
    assert str(missing) in message, "the error must name where the SDK is expected"
    assert "gopay-deploy" in message
    assert "忽略" in message, "and say it is safe to ignore when not using GoPay"


def test_bundled_sdk_is_added_to_sys_path(monkeypatch, tmp_path):
    src = tmp_path / "gopay-deploy" / "app" / "src"
    src.mkdir(parents=True)
    monkeypatch.setattr(_opai_loader, "_opai_src_dir", lambda: str(src))

    _opai_loader.ensure_opai_on_path()

    assert str(src) in sys.path


def test_pip_installed_sdk_does_not_raise(monkeypatch, tmp_path):
    """``pip install -e .`` inside the SDK leaves it importable without the bundle."""
    monkeypatch.setattr(
        _opai_loader, "_opai_src_dir", lambda: str(tmp_path / "absent")
    )
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())

    _opai_loader.ensure_opai_on_path()  # must not raise


def test_success_is_cached(monkeypatch, tmp_path):
    src = tmp_path / "gopay-deploy" / "app" / "src"
    src.mkdir(parents=True)
    monkeypatch.setattr(_opai_loader, "_opai_src_dir", lambda: str(src))
    _opai_loader.ensure_opai_on_path()

    sys.path.remove(str(src))
    _opai_loader.ensure_opai_on_path()

    assert str(src) not in sys.path, "second call should short-circuit on the cache"


def test_failure_is_not_cached(monkeypatch, tmp_path):
    """A missing SDK must be re-checked, so dropping it in takes effect at once."""
    src = tmp_path / "gopay-deploy" / "app" / "src"
    monkeypatch.setattr(_opai_loader, "_opai_src_dir", lambda: str(src))
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    with pytest.raises(RuntimeError):
        _opai_loader.ensure_opai_on_path()

    src.mkdir(parents=True)
    _opai_loader.ensure_opai_on_path()  # now succeeds without restarting

    assert str(src) in sys.path


def test_opai_available_false_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        _opai_loader, "_opai_src_dir", lambda: str(tmp_path / "absent")
    )
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert _opai_loader.opai_available() is False


def test_opai_available_true_when_bundled(monkeypatch, tmp_path):
    src = tmp_path / "gopay-deploy" / "app" / "src"
    src.mkdir(parents=True)
    monkeypatch.setattr(_opai_loader, "_opai_src_dir", lambda: str(src))
    assert _opai_loader.opai_available() is True


def test_opai_src_dir_points_at_the_bundled_location():
    path = _opai_loader.opai_src_dir()
    assert path.endswith(os.path.join("gopay-deploy", "app", "src"))
