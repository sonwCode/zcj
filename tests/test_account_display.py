from core.account_display import build_account_display_summary


def _summary(chatgpt_usage):
    return build_account_display_summary(
        platform="chatgpt",
        email="quota@example.com",
        lifecycle_status="registered",
        validity_status="valid",
        plan_state="unknown",
        plan_name="",
        display_status="registered",
        overview={"chatgpt_usage": chatgpt_usage},
        provider_resources=[],
    )


def test_free_quota_uses_monthly_label_and_omits_missing_code_review_limit():
    summary = _summary(
        {
            "plan_type": "free",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 30 * 24 * 60 * 60,
                }
            },
            "code_review_rate_limit": None,
        }
    )

    assert [(metric["label"], metric["value"]) for metric in summary["primary_metrics"]] == [
        ("月限额", "剩余 100%")
    ]


def test_paid_quota_keeps_weekly_labels_when_weekly_windows_are_returned():
    weekly_window = {
        "primary_window": {
            "used_percent": 25,
            "limit_window_seconds": 7 * 24 * 60 * 60,
        }
    }
    summary = _summary(
        {
            "plan_type": "plus",
            "rate_limit": weekly_window,
            "code_review_rate_limit": weekly_window,
        }
    )

    assert [(metric["label"], metric["value"]) for metric in summary["primary_metrics"]] == [
        ("周限额", "剩余 75%"),
        ("代码审查周限额", "剩余 75%"),
    ]


def test_registration_pipeline_exposes_specific_failed_stage_in_display_summary():
    summary = build_account_display_summary(
        platform="chatgpt",
        email="pending@example.com",
        lifecycle_status="pending_verification",
        validity_status="valid",
        plan_state="free",
        plan_name="free",
        display_status="pending_verification",
        overview={
            "registration_pipeline": {
                "registration_status": "failed",
                "current_stage": "credentials_ready",
                "stages": {
                    "account_created": {"status": "passed"},
                    "phone_verified": {"status": "passed"},
                    "credentials_ready": {
                        "status": "failed",
                        "error": "CODEX_RT_MISSING",
                    },
                },
            }
        },
        provider_resources=[],
    )

    assert summary["registration"]["state"] == "failed"
    assert summary["registration"]["label"] == "Codex RT 获取失败"
    assert summary["registration"]["completed_stages"] == 2
    assert summary["registration"]["error"] == "CODEX_RT_MISSING"
    assert any(item["key"] == "registration_pipeline" for item in summary["warnings"])
