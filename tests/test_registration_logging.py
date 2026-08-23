from core.registration_logging import classify_registration_log


def test_successful_transport_details_are_diagnostic():
    assert classify_registration_log("NextAuth providers 状态: 200") == {
        "log_view": "diagnostic",
        "log_stage": "authorization",
    }


def test_failures_remain_in_summary_even_when_they_contain_status():
    result = classify_registration_log("验证码校验失败: HTTP 403")

    assert result["log_view"] == "summary"
    assert result["log_stage"] == "verification"


def test_expected_request_shape_probes_are_diagnostic():
    result = classify_registration_log(
        "手机号验证码发送状态: 400 body_fields=phone_number,channel"
    )

    assert result["log_view"] == "diagnostic"
    assert result["log_stage"] == "verification"


def test_warnings_are_always_visible_in_summary():
    result = classify_registration_log("Sentinel VM failed", level="warning")

    assert result["log_view"] == "summary"
