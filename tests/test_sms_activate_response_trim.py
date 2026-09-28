"""SMS-Activate responses are parsed without trimming the fields.

The API pads values, so ``ACCESS_NUMBER: 12345 : 7999 `` yields an activation id with
spaces around it.  Nothing strips it: the padded id is handed straight back to the
provider as a query parameter (``getStatus``/``setStatus``) and the padded code is
returned to the caller.  A space in a query value is encoded as ``+`` or ``%20``, so the
provider is asked about a different id than the one it issued.
"""
from __future__ import annotations

import pytest

from core.base_sms import SmsActivateProvider


class _Scripted(SmsActivateProvider):
    """A provider whose HTTP layer is replaced by a canned response list."""

    def __init__(self, *, number="", statuses=()):
        super().__init__("dummy-key", default_country="ru")
        self._number = number
        self._statuses = list(statuses)
        self.calls = []

    def _request(self, action, **params):
        self.calls.append((action, dict(params)))
        if action == "getNumber":
            return self._number
        if action == "getStatus":
            return self._statuses.pop(0) if self._statuses else "STATUS_CANCEL"
        return "ACCESS_CANCEL"


def test_a_padded_number_response_keeps_the_padding():
    """Documents the current behaviour so a fix cannot silently regress."""
    provider = _Scripted(number="ACCESS_NUMBER: 12345 : 7999 ")

    activation = provider.get_number(service="default")

    assert activation.activation_id == "12345"
    assert activation.phone_number == "7999"


def test_the_padded_id_is_not_sent_back_to_the_provider():
    provider = _Scripted(number="ACCESS_NUMBER: 12345 : 7999 ", statuses=["STATUS_OK: 987654 "])
    activation = provider.get_number(service="default")

    provider.get_code(activation.activation_id, timeout=5)

    status_calls = [params for action, params in provider.calls if action == "getStatus"]
    assert status_calls, provider.calls
    assert status_calls[0]["id"] == "12345"


def test_a_padded_code_is_trimmed():
    provider = _Scripted(number="ACCESS_NUMBER:1:2", statuses=["STATUS_OK: 987654 "])

    code = provider.get_code("1", timeout=5)

    assert code == "987654"
