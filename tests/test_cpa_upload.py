from types import SimpleNamespace


def test_normalize_cpa_api_url_removes_management_suffix_and_slashes():
    from platforms.chatgpt.cpa_upload import normalize_cpa_api_url

    assert normalize_cpa_api_url(" https://cpa.example/ " ) == "https://cpa.example"
    assert normalize_cpa_api_url("https://cpa.example/v0/management/auth-files") == "https://cpa.example"
    assert normalize_cpa_api_url("https://cpa.example/v0/management") == "https://cpa.example"
    assert normalize_cpa_api_url("https://cpa.example/v0") == "https://cpa.example"


def test_upload_to_cpa_uses_single_slash_and_management_headers(monkeypatch):
    from platforms.chatgpt import cpa_upload

    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["headers"] = kwargs["headers"]
        return SimpleNamespace(status_code=201, text="", json=lambda: {})

    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)
    ok, message = cpa_upload.upload_to_cpa(
        {
            "email": "probe@example.com",
            "account_id": "acct-probe",
            "access_token": "access",
        },
        api_url="https://cpa.example/",
        api_key="management-key",
    )

    assert ok is True
    assert message == "上传成功"
    assert calls["url"].startswith("https://cpa.example/v0/management/auth-files?name=")
    assert "//v0/" not in calls["url"]
    assert calls["headers"]["Authorization"] == "Bearer management-key"
    assert calls["headers"]["X-API-Key"] == "management-key"
    assert calls["headers"]["X-Management-Key"] == "management-key"


def test_cpa_connection_uses_read_only_authenticated_get(monkeypatch):
    from platforms.chatgpt import cpa_upload

    calls = {}

    def fake_get(url, **kwargs):
        calls["url"] = url
        calls["headers"] = kwargs["headers"]
        return SimpleNamespace(status_code=200, text="", json=lambda: {"files": []})

    monkeypatch.setattr(cpa_upload.cffi_requests, "get", fake_get)
    ok, message = cpa_upload.test_cpa_connection(
        "https://cpa.example/v0/management",
        "management-key",
    )

    assert ok is True
    assert "Management Key 已验证" in message
    assert calls["url"] == "https://cpa.example/v0/management/auth-files"
    assert calls["headers"]["Authorization"] == "Bearer management-key"
    assert calls["headers"]["X-Management-Key"] == "management-key"


def test_cpa_connection_does_not_report_forbidden_as_success(monkeypatch):
    from platforms.chatgpt import cpa_upload

    monkeypatch.setattr(
        cpa_upload.cffi_requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(status_code=403, text=""),
    )

    ok, message = cpa_upload.test_cpa_connection(
        "https://cpa.example",
        "wrong-management-key",
    )

    assert ok is False
    assert "403" in message
    assert "remote-management.allow-remote" in message


def test_cpa_connection_reports_invalid_management_key(monkeypatch):
    from platforms.chatgpt import cpa_upload

    monkeypatch.setattr(
        cpa_upload.cffi_requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(status_code=401, text=""),
    )

    ok, message = cpa_upload.test_cpa_connection(
        "https://cpa.example",
        "wrong-management-key",
    )

    assert ok is False
    assert "401" in message
    assert "Management Key" in message
