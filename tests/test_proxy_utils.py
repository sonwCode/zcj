from core.proxy_utils import (
    infer_proxy_region,
    mask_proxy_url,
    normalize_proxy_url,
    pin_711proxy_session,
    redact_proxy_credentials,
)
from application.tasks import _pin_chatgpt_registration_proxy


def test_normalizes_vendor_host_port_at_user_pass_format():
    assert (
        normalize_proxy_url("gate.example.com:8080@user:pass")
        == "http://user:pass@gate.example.com:8080"
    )


def test_repairs_saved_scheme_host_port_at_user_pass_format():
    assert (
        normalize_proxy_url("http://gate.example.com:8080@user:pass")
        == "http://user:pass@gate.example.com:8080"
    )


def test_keeps_standard_authenticated_proxy_format():
    assert (
        normalize_proxy_url("http://user:pass@gate.example.com:8080")
        == "http://user:pass@gate.example.com:8080"
    )


def test_rejects_non_numeric_proxy_port():
    assert normalize_proxy_url("http://user:pass@gate.example.com:not-a-port") == ""


def test_redacts_password_from_proxy_errors():
    proxy = "http://user:secret@gate.example.com:8080"
    message = f"connection failed via {proxy}"
    assert redact_proxy_credentials(message, proxy) == (
        "connection failed via http://user:****@gate.example.com:8080"
    )


def test_masks_proxy_credentials_in_user_facing_logs():
    assert mask_proxy_url("http://user:secret@gate.example.com:8080") == (
        "http://***@gate.example.com:8080"
    )


def test_infers_region_from_vendor_username():
    proxy = "http://subuser-zone-custom-region-JP:secret@gate.example.com:8080"
    assert infer_proxy_region(proxy) == "JP"


def test_pins_711proxy_to_phone_country_and_sticky_session():
    proxy = "http://USER-zone-custom:secret@global.rotgb.711proxy.com:10000"

    pinned = pin_711proxy_session(
        proxy,
        region="CL",
        session_id="task-123456789",
        session_minutes=180,
    )

    assert pinned == (
        "http://USER-zone-custom-region-CL-session-task1234567-sessTime-180:secret"
        "@global.rotgb.711proxy.com:10000"
    )
    assert infer_proxy_region(pinned) == "CL"


def test_replaces_existing_711proxy_route_modifiers():
    proxy = (
        "http://USER-zone-custom-region-US-session-old123-sessTime-5:secret"
        "@global.rotgb.711proxy.com:10000"
    )

    pinned = pin_711proxy_session(proxy, region="ID", session_id="new123")

    assert "region-ID" in pinned
    assert "session-new123" in pinned
    assert "region-US" not in pinned
    assert "session-old123" not in pinned


def test_chatgpt_registration_upgrades_short_shared_711_session():
    proxy = (
        "http://USER-zone-custom-region-US-session-shared-sessTime-5:secret"
        "@global.rotgb.711proxy.com:10000"
    )

    pinned = _pin_chatgpt_registration_proxy(
        proxy,
        region="US",
        session_id="regworker01",
    )

    assert "region-US" in pinned
    assert "session-regworker01" in pinned
    assert "sessTime-180" in pinned
    assert "session-shared" not in pinned
    assert "sessTime-5" not in pinned
