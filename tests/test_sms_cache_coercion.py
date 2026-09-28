r"""A persisted SMS phone cache must not be able to raise.

``HeroSmsProvider._load_cache`` reads ``.herosms_phone_cache.json`` and wraps the
``json.loads`` in ``except Exception`` - the author knew the file can be malformed -
but then read ``acquired_at`` / ``use_count`` out of the parsed dict with a bare
``float()`` / ``int()``. A cache written by an older version (ISO-string timestamp) or a
hand-edited file therefore raised ``ValueError`` / ``OverflowError`` out of
``get_reuse_info``, which ``is_herosms_phone_cache_alive`` calls when deciding whether a
number may be reused for scheduling.

Two things are asserted for every shape: the call does not raise, and a cache whose age
is unusable is *dropped* rather than reused.
"""
from __future__ import annotations

import json
import time

import pytest

from core import base_sms as sms_module
from core.base_sms import HeroSmsProvider, is_herosms_phone_cache_alive


API_KEY = "hero123"


def _payload(provider: HeroSmsProvider, **overrides):
    payload = {
        **provider._cache_identity(provider.default_service, provider.default_country),
        "activation_id": "act_1",
        "phone_number": "+15551234",
        "acquired_at": time.time(),
        "use_count": 0,
    }
    payload.update(overrides)
    return payload


def _write_cache(tmp_path, monkeypatch, **overrides):
    """Write a cache file and prove the identity gate would accept it.

    Without the identity fields ``_load_cache`` returns ``None`` before reading any
    field, which would make every assertion below pass without reaching the coercion.
    """
    provider = HeroSmsProvider(API_KEY)
    payload = _payload(provider, **overrides)
    expected = provider._cache_identity(provider.default_service, provider.default_country)
    assert all(str(payload.get(k) or "") == str(v) for k, v in expected.items()), \
        "test fixture must pass the identity gate or it reads nothing"

    path = tmp_path / ".herosms_phone_cache.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: path)
    monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
    return path


MALFORMED_ACQUIRED_AT = [
    "2025-12-01T00:00:00Z",
    "not-a-number",
    None,
    float("inf"),
    float("-inf"),
    float("nan"),
    # A JSON integer literal larger than a float can hold parses to a Python int, and
    # float() of it raises OverflowError rather than returning inf.
    10 ** 400,
]


@pytest.mark.parametrize("bad", MALFORMED_ACQUIRED_AT)
def test_a_malformed_acquired_at_does_not_raise(tmp_path, monkeypatch, bad):
    _write_cache(tmp_path, monkeypatch, acquired_at=bad)

    info = HeroSmsProvider(API_KEY).get_reuse_info()

    assert isinstance(info, dict)
    # An age that cannot be established must not be reused.
    assert info.get("alive") is False


@pytest.mark.parametrize("bad", ["many", float("inf"), float("nan"), None])
def test_a_malformed_use_count_does_not_raise(tmp_path, monkeypatch, bad):
    _write_cache(tmp_path, monkeypatch, use_count=bad)

    info = HeroSmsProvider(API_KEY).get_reuse_info()

    assert info["use_count"] == 0


@pytest.mark.parametrize("bad", MALFORMED_ACQUIRED_AT)
def test_is_herosms_phone_cache_alive_survives_a_malformed_file(tmp_path, monkeypatch, bad):
    _write_cache(tmp_path, monkeypatch, acquired_at=bad)

    alive, info = is_herosms_phone_cache_alive({"herosms_api_key": API_KEY})

    assert alive is False
    assert isinstance(info, dict)


def test_a_healthy_cache_is_still_reused(tmp_path, monkeypatch):
    _write_cache(tmp_path, monkeypatch, use_count=1)

    info = HeroSmsProvider(API_KEY).get_reuse_info()

    assert info["alive"] is True
    assert info["use_count"] == 1


def test_a_healthy_cache_reports_alive_through_the_scheduling_helper(tmp_path, monkeypatch):
    _write_cache(tmp_path, monkeypatch, use_count=1)

    alive, info = is_herosms_phone_cache_alive({"herosms_api_key": API_KEY})

    assert alive is True
    assert info["use_count"] == 1
