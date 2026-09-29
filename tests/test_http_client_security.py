from __future__ import annotations

import pytest

from core.http_client import (
    HTTPClient,
    HTTPClientError,
    RequestConfig,
    _safe_text,
    _safe_url,
)


def test_safe_url_removes_credentials_and_secret_query_values():
    value = _safe_url(
        "https://proxy-user:proxy-pass@example.test/v1?token=secret-token&api-key=secret-key&page=2#fragment"
    )

    assert "proxy-user" not in value
    assert "proxy-pass" not in value
    assert "secret-token" not in value
    assert "secret-key" not in value
    assert "token=%2A%2A%2A" in value
    assert "api-key=%2A%2A%2A" in value
    assert "page=2" in value
    assert "fragment" not in value


def test_safe_text_removes_sensitive_fields_and_embedded_urls():
    value = _safe_text(
        "request failed https://user:pass@example.test/?password=secret&ok=1 token=raw-token"
    )

    assert "user" not in value
    assert "user:pass@" not in value
    assert "proxy-pass" not in value
    assert "secret" not in value
    assert "raw-token" not in value
    assert "ok=1" in value


def test_request_error_does_not_echo_raw_url_or_exception_secret():
    class BrokenSession:
        def request(self, method, url, **kwargs):
            raise ConnectionError(
                "upstream https://proxy-user:proxy-pass@example.test/?token=secret-token"
            )

    client = HTTPClient(
        session=BrokenSession(),
        config=RequestConfig(max_retries=1),
    )

    with pytest.raises(HTTPClientError) as caught:
        client.get("https://proxy-user:proxy-pass@example.test/?token=secret-token")

    message = str(caught.value)
    assert "proxy-pass" not in message
    assert "secret-token" not in message
    assert "example.test" in message
