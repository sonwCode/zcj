"""Offline resource-capability checks for registration preflight."""
from __future__ import annotations

from core.registration import preflight


def test_fixed_mailbox_is_reported_as_existing_mailbox_consumer():
    check = preflight.check_mailbox_source(
        email="existing@example.test",
        required=True,
    )

    assert check.ok is True
    assert "mode=consume_existing" in check.detail
    assert "mainstream_signup=absent" in check.detail
    assert "provisioning=unsupported" in check.detail


def test_inline_mailbox_pool_counts_only_well_shaped_rows():
    check = preflight.check_mailbox_source(
        config={
            "api_mailbox_lines": "first@example.test----https://mailbox.test/1\nnot-a-row",
        },
        required=True,
    )

    assert check.ok is True
    assert "rows=1/2" in check.detail


def test_provider_setting_is_not_mistaken_for_mainstream_signup():
    check = preflight.check_mailbox_source(
        provider="generic_http",
        required=True,
    )

    assert check.ok is True
    assert "mode=temporary_mailbox_service" in check.detail
    assert "provisioning=temporary_only" in check.detail
    assert "mainstream_signup=absent" in check.detail


def test_missing_mailbox_source_fails_when_required():
    check = preflight.check_mailbox_source(required=True)

    assert check.ok is False
    assert check.required is True
    assert "error=no_mailbox_source" in check.detail


def test_sms_provider_requires_the_runtime_api_key():
    missing = preflight.check_sms_source(
        provider="herosms",
        config={},
        required=True,
    )
    configured = preflight.check_sms_source(
        provider="herosms",
        config={"herosms_api_key": "test-key"},
        required=True,
    )

    assert missing.ok is False
    assert "api_key=missing" in missing.detail
    assert configured.ok is True
    assert "api_key=present" in configured.detail
    assert "network_probe=skipped" in configured.detail


def test_run_preflight_exposes_resource_checks_without_network():
    report = preflight.run_preflight(
        mailbox_provider="local_ms_pool",
        mailbox_config={"local_ms_pool_text": "existing@example.test----token"},
        require_mailbox=True,
        sms_provider="smsbower",
        sms_config={"smsbower_api_key": "test-key"},
        require_sms=True,
    )
    checks = {check.name: check for check in report.checks}

    assert checks["mailbox_resource"].ok is True
    assert checks["sms_resource"].ok is True
    assert report.ok is True
