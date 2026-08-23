from __future__ import annotations

from application.tasks import _post_registration_chatgpt_liveness_error
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration.models import RegistrationResult


class _TestPlatform(BasePlatform):
    name = ""
    display_name = "Test"
    supported_executors = ["protocol"]

    def check_valid(self, account: Account) -> bool:
        return True


def _platform() -> _TestPlatform:
    return _TestPlatform(RegisterConfig(executor_type="protocol"))


def test_no_token_is_pending():
    result = RegistrationResult(email="test@example.com", password="pass123")

    account = _platform()._account_from_registration_result(result)

    assert account.status == AccountStatus.PENDING_VERIFICATION


def test_has_token_is_registered():
    result = RegistrationResult(
        email="test@example.com",
        password="pass123",
        token="valid_token",
    )

    account = _platform()._account_from_registration_result(result)

    assert account.status == AccountStatus.REGISTERED


def test_has_token_but_add_phone_is_pending():
    result = RegistrationResult(
        email="test@example.com",
        password="pass123",
        token="some_token",
        extra={"page_type": "add_phone"},
    )

    account = _platform()._account_from_registration_result(result)

    assert account.status == AccountStatus.PENDING_VERIFICATION


def test_has_extra_token_but_email_otp_is_pending():
    result = RegistrationResult(
        email="test@example.com",
        password="pass123",
        extra={"access_token": "token", "page_type": "email_otp"},
    )

    account = _platform()._account_from_registration_result(result)

    assert account.status == AccountStatus.PENDING_VERIFICATION


def test_has_token_completed_is_registered():
    result = RegistrationResult(
        email="test@example.com",
        password="pass123",
        token="valid",
        extra={"page_type": "chat"},
    )

    account = _platform()._account_from_registration_result(result)

    assert account.status == AccountStatus.REGISTERED


def test_manual_phone_required_explicit_status():
    result = RegistrationResult(
        email="test@example.com",
        password="pass123",
        status=AccountStatus.MANUAL_PHONE_REQUIRED,
    )

    account = _platform()._account_from_registration_result(result)

    assert account.status == AccountStatus.MANUAL_PHONE_REQUIRED


class _LivenessLogger:
    def __init__(self):
        self.lines = []

    def log(self, message, level="info"):
        self.lines.append((level, message))

    def is_cancel_requested(self):
        return False


def test_sms_registration_liveness_rejects_remote_deactivation():
    class Platform:
        def check_valid(self, account):
            return False

        def get_last_check_overview(self):
            return {
                "validity_status": "invalid",
                "validity_reason": "远端认证返回 account_deactivated",
            }

    account = Account(
        platform="chatgpt",
        email="new@example.com",
        password="Secret123!",
        extra={"register_mode": "phone_with_email"},
    )
    error = _post_registration_chatgpt_liveness_error(
        platform_name="chatgpt",
        platform=Platform(),
        account=account,
        extra={"post_registration_liveness_delay_seconds": 0},
        logger=_LivenessLogger(),
    )

    assert "远端停用" in error
    assert account.status == AccountStatus.INVALID
    assert account.extra["account_overview"]["validity_status"] == "invalid"


def test_sms_registration_liveness_keeps_unknown_network_state():
    class Platform:
        def check_valid(self, account):
            return False

        def get_last_check_overview(self):
            return {"validity_status": "unknown", "validity_reason": "network timeout"}

    account = Account(
        platform="chatgpt",
        email="new@example.com",
        password="Secret123!",
        extra={"register_mode": "email_then_phone"},
    )
    error = _post_registration_chatgpt_liveness_error(
        platform_name="chatgpt",
        platform=Platform(),
        account=account,
        extra={"post_registration_liveness_delay_seconds": 0},
        logger=_LivenessLogger(),
    )

    assert error == ""
    assert account.status == AccountStatus.REGISTERED
