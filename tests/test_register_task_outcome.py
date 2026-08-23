from application.tasks import (
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCEEDED,
    _registration_failure_summary,
    _register_task_outcome,
)


def test_retry_failures_are_not_reported_as_multiple_failed_accounts():
    outcome = _register_task_outcome(
        target_count=1,
        success_count=0,
        submitted_attempts=3,
        attempt_errors=["mailbox reset", "proxy tls", "phone exhausted"],
    )

    assert outcome["failure_count"] == 1
    assert outcome["attempt_failure_count"] == 3
    assert outcome["summary"] == "完成: 成功 0 个, 失败 1 个（共尝试 3 次，尝试失败 3 次）"
    assert outcome["status"] == TASK_STATUS_FAILED
    assert outcome["final_error"] == "phone exhausted"


def test_success_after_retries_meets_the_single_target():
    outcome = _register_task_outcome(
        target_count=1,
        success_count=1,
        submitted_attempts=3,
        attempt_errors=["mailbox reset", "proxy tls"],
    )

    assert outcome["failure_count"] == 0
    assert outcome["status"] == TASK_STATUS_SUCCEEDED
    assert outcome["final_error"] == ""


def test_partial_multi_account_result_remains_failed():
    outcome = _register_task_outcome(
        target_count=5,
        success_count=3,
        submitted_attempts=5,
        attempt_errors=["first", "second"],
    )

    assert outcome["failure_count"] == 2
    assert outcome["status"] == TASK_STATUS_FAILED
    assert outcome["final_error"] == "second"


def test_registration_failure_summary_groups_actionable_reasons():
    summary = _registration_failure_summary(
        [
            "创建用户账户失败: user_already_exists",
            "An account already exists for this email address",
            "获取验证码失败: Microsoft refresh_token invalid_grant",
            "获取 Device ID 失败: CONNECT tunnel failed 502",
        ]
    )

    assert summary == [
        {
            "code": "email_already_registered",
            "label": "邮箱已注册",
            "count": 2,
            "sample": "创建用户账户失败: user_already_exists",
        },
        {
            "code": "mailbox_auth",
            "label": "邮箱授权失效",
            "count": 1,
            "sample": "获取验证码失败: Microsoft refresh_token invalid_grant",
        },
        {
            "code": "proxy_network",
            "label": "代理或网络异常",
            "count": 1,
            "sample": "获取 Device ID 失败: CONNECT tunnel failed 502",
        },
    ]


def test_registration_failure_summary_prioritizes_phone_errors_over_oauth_marker():
    summary = _registration_failure_summary(
        ["手机号验证失败: code=invalid_auth_step Invalid authorization step."]
    )

    assert summary == [
        {
            "code": "phone_verification",
            "label": "手机号验证失败",
            "count": 1,
            "sample": "手机号验证失败: code=invalid_auth_step Invalid authorization step.",
        }
    ]
