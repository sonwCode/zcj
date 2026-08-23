from __future__ import annotations

import json
import re

import pytest

from application import tasks as tasks_module
from core.base_platform import Account
from domain.actions import ActionExecutionResult
from domain.actions import ActionExecutionCommand
from infrastructure import platform_runtime as runtime_module


class _FakeLogger:
    def __init__(self):
        self.events = []
        self.result_data = None
        self.finished = None
        self.cancel_requested = False

    def log(self, message, **kwargs):
        self.events.append(("log", message, kwargs))

    def record_error(self, error):
        self.events.append(("error", error, {}))

    def record_success(self):
        self.events.append(("success", "", {}))

    def set_result_data(self, data):
        self.result_data = data

    def set_progress(self, current, total):
        self.events.append(("progress", current, {"total": total}))

    def is_cancel_requested(self):
        return self.cancel_requested

    def set_subtask(self, subtask_id, label=""):
        self.events.append(("subtask", subtask_id, {"label": label}))

    def clear_subtask(self):
        self.events.append(("clear_subtask", "", {}))

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def test_platform_action_task_passes_task_logger_to_runtime(monkeypatch):
    seen = {}

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            seen["log_fn"] = log_fn
            seen["cancel_check"] = cancel_check
            if log_fn:
                log_fn("checkout step log")
            return ActionExecutionResult(ok=True, data={"message": "summary"})

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert getattr(seen["log_fn"], "__name__", "") == "log"
    assert getattr(seen["cancel_check"], "__self__", None) is logger
    assert getattr(seen["cancel_check"], "__name__", "") == "is_cancel_requested"
    assert seen["cancel_check"]() is False
    assert ("log", "checkout step log", {}) in logger.events
    assert logger.result_data == {"message": "summary"}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_register_task_succeeds_after_successful_registration(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "oauth_browser",
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert any(event[0] == "success" for event in logger.events)
    assert not any(
        "cannot access local variable 'extra'" in str(event)
        for event in logger.events
    )


def test_email_then_phone_keeps_parallel_attempt_window_when_target_is_one(monkeypatch):
    """Phone waits may overlap across independent accounts, even for count=1."""
    import threading
    import time

    seen = {"active": 0, "max_active": 0, "calls": 0}
    lock = threading.Lock()

    class FakePlatform:
        def register(self, email=None, password=None):
            with lock:
                seen["calls"] += 1
                seen["active"] += 1
                seen["max_active"] = max(seen["max_active"], seen["active"])
            time.sleep(0.03)
            with lock:
                seen["active"] -= 1
            return Account(
                platform="chatgpt",
                email=f"parallel-{seen['calls']}@example.com",
                password="Secret123!",
                user_id=f"acct_{seen['calls']}",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(tasks_module, "_build_platform_instance", lambda *args, **kwargs: FakePlatform())
    monkeypatch.setattr(tasks_module, "_complete_required_chatgpt_phone_verification", lambda **kwargs: None)
    monkeypatch.setattr(tasks_module, "_upgrade_protocol_codex_credentials", lambda **kwargs: None)
    monkeypatch.setattr(tasks_module, "_account_has_codex_rt", lambda account: True)
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: None)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 3,
            "extra": {
                "identity_provider": "oauth_browser",
                "require_phone_verification": True,
                "complete_started_attempts": True,
                "high_concurrency": {"mode": "custom", "concurrency": 3},
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert seen["max_active"] >= 2
    assert any(
        event[0] == "log" and "并发窗口=3" in str(event[1])
        for event in logger.events
    )
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_email_then_phone_target_five_keeps_all_five_sms_workers_in_flight(monkeypatch):
    """Target=5/concurrency=5 must reach five independent SMS stages together."""
    import threading

    worker_count = 5
    sms_barrier = threading.Barrier(worker_count)
    state = {
        "next_account": 0,
        "active_sms": 0,
        "max_active_sms": 0,
        "phone_calls": 0,
    }
    state_lock = threading.Lock()

    class FakePlatform:
        def register(self, email=None, password=None):
            with state_lock:
                state["next_account"] += 1
                account_number = state["next_account"]
            return Account(
                platform="chatgpt",
                email=f"parallel-{account_number}@example.com",
                password=password or "Secret123!",
                user_id=f"acct_{account_number}",
                extra={"access_token": "access-token"},
            )

    def fake_phone_verification(*, extra, **_kwargs):
        assert extra["register_reuse_phone_to_max"] is False
        with state_lock:
            state["phone_calls"] += 1
            state["active_sms"] += 1
            state["max_active_sms"] = max(
                state["max_active_sms"],
                state["active_sms"],
            )
        try:
            sms_barrier.wait(timeout=3)
        finally:
            with state_lock:
                state["active_sms"] -= 1

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_resolve_sms_provider_for_task",
        lambda _extra: (
            "herosms",
            {
                "herosms_api_key": "test-key",
                "register_reuse_phone_to_max": True,
            },
        ),
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(
        tasks_module,
        "_complete_required_chatgpt_phone_verification",
        fake_phone_verification,
    )
    monkeypatch.setattr(tasks_module, "_upgrade_protocol_codex_credentials", lambda **kwargs: None)
    monkeypatch.setattr(tasks_module, "_account_has_codex_rt", lambda account: True)
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: None)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_sub2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": worker_count,
            "concurrency": worker_count,
            "extra": {
                "identity_provider": "oauth_browser",
                "require_phone_verification": True,
                "post_registration_liveness_delay_seconds": 0,
                "high_concurrency": {
                    "mode": "custom",
                    "concurrency": worker_count,
                },
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert state["phone_calls"] == worker_count
    assert state["max_active_sms"] == worker_count
    assert logger.result_data["success"] == worker_count
    assert logger.result_data["attempts"] == worker_count
    assert logger.result_data["hero_sms_reuse"] is False
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert any(
        event[0] == "log" and "接码并发隔离已启用" in str(event[1])
        for event in logger.events
    )


def test_sub2_auto_sync_forces_mailbox_registration_through_phone_verification(monkeypatch):
    monkeypatch.setattr(
        "core.sub2api_sync.sub2api_auto_sync_enabled",
        lambda: True,
    )
    extra = {"identity_provider": "mailbox"}

    assert tasks_module._enforce_sub2_registration_requirements("chatgpt", extra) is True
    assert extra["sub2api_auto_sync"] is True
    assert extra["require_phone_verification"] is True
    assert extra["register_mode"] == "email_then_phone"


def test_sub2_auto_sync_does_not_override_explicit_task_disable(monkeypatch):
    monkeypatch.setattr(
        "core.sub2api_sync.sub2api_auto_sync_enabled",
        lambda: True,
    )
    extra = {"identity_provider": "mailbox", "sub2api_auto_sync": False}

    assert tasks_module._enforce_sub2_registration_requirements("chatgpt", extra) is False
    assert "require_phone_verification" not in extra


def test_chatgpt_register_task_keeps_protocol_for_separate_workspace_join(monkeypatch):
    seen = {}

    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={"access_token": "access-token"},
            )

    def fake_build(_platform_name, payload, *_args, **_kwargs):
        seen["executor_type"] = payload.get("executor_type")
        return FakePlatform()

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build)
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: None)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "executor_type": "protocol",
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "oauth_browser",
                "auto_chatgpt_workspace_join": True,
                "chatgpt_workspace_join": {
                    "enabled": True,
                    "workspace_ids": "workspace-1",
                },
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert seen["executor_type"] == "protocol"
    assert any(
        event[0] == "log"
        and "Workspace Join 将在注册成功后作为独立任务执行" in str(event[1])
        for event in logger.events
    )
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_register_task_uses_inline_api_mailbox_pool(monkeypatch):
    seen = {"calls": 0, "payloads": [], "mailboxes": []}

    class FakePlatform:
        def register(self, email=None, password=None):
            seen["calls"] += 1
            return Account(
                platform="chatgpt",
                email=f"registered-{seen['calls']}@example.com",
                password=password or "Secret123!",
                user_id=f"acct_{seen['calls']}",
                extra={"access_token": "access-token"},
            )

    def fake_build(_platform_name, payload, *_args, **kwargs):
        seen["payloads"].append(payload)
        seen["mailboxes"].append(kwargs.get("shared_mailbox"))
        return FakePlatform()

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build)
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: None)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 5,
            "executor_type": "headless",
            "extra": {
                "identity_provider": "mailbox",
                "chatgpt_api_mailbox_lines": "base@gmail.com----https://gapi.example/code",
                "gmail_alias_enabled": True,
                "gmail_alias_count": 2,
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert seen["calls"] == 1
    assert all(mailbox is seen["mailboxes"][0] for mailbox in seen["mailboxes"])
    assert seen["mailboxes"][0].pool_text.count("https://gapi.example/code") == 2
    assert seen["payloads"][0]["extra"]["mail_provider"] == "local_ms_pool"
    assert any("API mailbox pool loaded" in str(event[1]) for event in logger.events)


def test_chatgpt_register_task_counts_hotmail_plus_aliases(monkeypatch):
    seen = {"calls": 0, "mailboxes": []}

    class FakePlatform:
        def register(self, email=None, password=None):
            seen["calls"] += 1
            return Account(
                platform="chatgpt",
                email=f"registered-{seen['calls']}@example.com",
                password=password or "Secret123!",
                user_id=f"acct_{seen['calls']}",
                extra={"access_token": "access-token"},
            )

    def fake_build(_platform_name, _payload, *_args, **kwargs):
        seen["mailboxes"].append(kwargs.get("shared_mailbox"))
        return FakePlatform()

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(tasks_module, "_build_platform_instance", fake_build)
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: None)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 10,
            "concurrency": 10,
            "extra": {
                "identity_provider": "mailbox",
                "chatgpt_api_mailbox_lines": (
                    "base@hotmail.com----mail-pass----client-id----refresh-token"
                ),
                "gmail_alias_enabled": True,
                "gmail_alias_count": 10,
            },
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert seen["calls"] == 10
    mailbox = seen["mailboxes"][0]
    assert all(item is mailbox for item in seen["mailboxes"])
    aliases = [entry.email for entry in mailbox._entries()]
    assert len(aliases) == 10
    assert len(set(aliases)) == 10
    assert all(re.fullmatch(r"base\+[a-z0-9]{5,7}@hotmail\.com", email) for email in aliases)


def test_chatgpt_register_task_keeps_requested_count_with_expanded_saved_mailboxes(monkeypatch):
    from core import base_mailbox as base_mailbox_module

    seen = {"calls": 0}

    class FakeMailbox:
        def available_count(self):
            return 3

    class FakePlatform:
        def register(self, email=None, password=None):
            seen["calls"] += 1
            return Account(
                platform="chatgpt",
                email=f"saved-{seen['calls']}@example.com",
                password=password or "Secret123!",
                user_id=f"acct_{seen['calls']}",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(tasks_module, "_resolve_registration_proxy_for_platform", lambda *args, **kwargs: None)
    monkeypatch.setattr(base_mailbox_module, "create_mailbox", lambda **kwargs: FakeMailbox())
    monkeypatch.setattr(tasks_module, "_build_platform_instance", lambda *args, **kwargs: FakePlatform())
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: None)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "executor_type": "protocol",
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "local_ms_pool",
                "chatgpt_api_mailbox_use_all": True,
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert seen["calls"] == 1
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_chatgpt_registration_proxy_prefers_explicit_then_pool():
    assert tasks_module._resolve_registration_proxy_for_platform(
        "chatgpt",
        explicit_proxy=None,
        proxy_getter=lambda: "http://pool-proxy.example:8080",
    ) == "http://pool-proxy.example:8080"
    assert tasks_module._resolve_registration_proxy_for_platform(
        "chatgpt",
        explicit_proxy="http://manual-proxy.example:8080",
        proxy_getter=lambda: "http://pool-proxy.example:8080",
    ) == "http://manual-proxy.example:8080"
    assert tasks_module._resolve_registration_proxy_for_platform(
        "chatgpt",
        explicit_proxy=None,
        proxy_getter=lambda: "http://pool-proxy.example:8080",
        allow_pool=False,
    ) is None


def test_chatgpt_registration_proxy_policy_reads_workbench_and_preserves_manual_override():
    assert tasks_module._chatgpt_registration_proxy_policy(
        {
            "chatgpt_register_workbench": {
                "proxy_strategy": "direct",
                "proxy_country": "br",
            }
        },
        explicit_proxy=None,
    ) == ("direct", "BR")
    assert tasks_module._chatgpt_registration_proxy_policy(
        {"proxy_strategy": "polling", "proxy_country": "us"},
        explicit_proxy="http://manual-proxy.example:8080",
    ) == ("manual_template", "US")


def test_chatgpt_register_task_leases_distinct_pool_proxies_per_concurrent_attempt(monkeypatch):
    import threading

    worker_proxies = [
        "http://worker-1:secret@global.rotgb.711proxy.com:10000",
        "http://worker-2:secret@global.rotgb.711proxy.com:10000",
        "http://worker-3:secret@global.rotgb.711proxy.com:10000",
    ]

    class FakeProxyPool:
        def __init__(self):
            self._lock = threading.Lock()
            self.available = list(worker_proxies)
            self.acquired = []
            self.released = []

        def get_next(self, region=""):
            assert region == "US"
            return "http://mailbox-route.example:8080"

        def acquire_next(self, region=""):
            assert region == "US"
            with self._lock:
                value = self.available.pop(0)
                self.acquired.append(value)
                return value

        def release(self, url):
            with self._lock:
                self.released.append(url)

        def assignment_label(self, url):
            return f"#{worker_proxies.index(url) + 1}"

        def report_success(self, url):
            assert url in worker_proxies

        def report_fail(self, url):
            raise AssertionError(f"unexpected proxy failure: {url}")

    fake_pool = FakeProxyPool()
    seen = []
    seen_lock = threading.Lock()

    class FakePlatform:
        def __init__(self, proxy):
            self.proxy = proxy

        def register(self, email=None, password=None):
            with seen_lock:
                seen.append(self.proxy)
            worker_number = next(
                index
                for index in range(1, len(worker_proxies) + 1)
                if f"worker-{index}-" in self.proxy
            )
            return Account(
                platform="chatgpt",
                email=f"worker-{worker_number}@example.com",
                password=password or "Secret123!",
                user_id=f"acct_{worker_number}",
                extra={"access_token": "access-token"},
            )

    monkeypatch.setattr("core.proxy_pool.proxy_pool", fake_pool)
    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda _name, _payload, _logger, *, resolved_proxy=None, **_kwargs: FakePlatform(resolved_proxy),
    )
    monkeypatch.setattr(tasks_module, "save_account", lambda _account: None)
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_sub2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()
    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 3,
            "concurrency": 3,
            "executor_type": "headed",
            "extra": {
                "identity_provider": "oauth_browser",
                "proxy_strategy": "polling",
                "proxy_country": "US",
                "high_concurrency": {"mode": "custom", "concurrency": 3},
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert len(set(seen)) == len(worker_proxies)
    for worker_number in range(1, len(worker_proxies) + 1):
        assert any(
            f"worker-{worker_number}-region-US-session-" in proxy
            and "-sessTime-180:" in proxy
            for proxy in seen
        )
    assert fake_pool.acquired == worker_proxies
    assert set(fake_pool.released) == set(worker_proxies)
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert any(
        event[0] == "log" and "使用代理池条目 #" in str(event[1])
        for event in logger.events
    )
    assert not any("secret" in str(event[1]) for event in logger.events)


def test_chatgpt_proxy_preflight_runs_before_registration(monkeypatch):
    from platforms.chatgpt import http_client as http_client_module

    class FakeClient:
        def __init__(self, proxy_url=None):
            assert proxy_url == "http://proxy-success.example:8080"
            self.closed = False

        def check_ip_location(self):
            return True, "JP"

        def check_nextauth_access(self):
            return True, 200

        def close(self):
            self.closed = True

    monkeypatch.setattr(http_client_module, "OpenAIHTTPClient", FakeClient)

    assert tasks_module._preflight_chatgpt_registration_proxy("http://proxy-success.example:8080") == "JP"


def test_chatgpt_proxy_preflight_rejects_nextauth_403(monkeypatch):
    from platforms.chatgpt import http_client as http_client_module

    class FakeClient:
        def __init__(self, proxy_url=None):
            pass

        def check_ip_location(self):
            return True, "MY"

        def check_nextauth_access(self):
            return False, 403

        def close(self):
            pass

    monkeypatch.setattr(http_client_module, "OpenAIHTTPClient", FakeClient)

    with pytest.raises(tasks_module._RegistrationProxyPreflightError, match="NextAuth HTTP 403"):
        tasks_module._preflight_chatgpt_registration_proxy("http://proxy-denied.example:8080")


def test_chatgpt_proxy_preflight_retries_transient_nextauth_network_failure(monkeypatch):
    from platforms.chatgpt import http_client as http_client_module

    clients = []

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.index = len(clients)
            self.closed = False
            clients.append(self)

        def check_ip_location(self):
            return True, "CL"

        def check_nextauth_access(self):
            return (self.index >= 2, 200 if self.index >= 2 else 0)

        def close(self):
            self.closed = True

    logs = []
    monkeypatch.setattr(http_client_module, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(tasks_module.time, "sleep", lambda _seconds: None)

    location = tasks_module._preflight_chatgpt_registration_proxy(
        "http://proxy-transient.example:8080",
        log_fn=logs.append,
    )

    assert location == "CL"
    assert len(clients) == 3
    assert all(client.closed for client in clients)
    assert logs == [
        "ChatGPT 协议代理预检网络中断，重试 (1/3)",
        "ChatGPT 协议代理预检网络中断，重试 (2/3)",
    ]


def test_chatgpt_proxy_preflight_caches_success_for_same_proxy(monkeypatch):
    from platforms.chatgpt import http_client as http_client_module

    clients = []

    class FakeClient:
        def __init__(self, proxy_url=None):
            clients.append(self)

        def check_ip_location(self):
            return True, "CO"

        def check_nextauth_access(self):
            return True, 200

        def close(self):
            pass

    monkeypatch.setattr(http_client_module, "OpenAIHTTPClient", FakeClient)
    proxy = "http://proxy-cache.example:8080"

    assert tasks_module._preflight_chatgpt_registration_proxy(proxy) == "CO"
    assert tasks_module._preflight_chatgpt_registration_proxy(proxy) == "CO"
    assert len(clients) == 1


def test_chatgpt_proxy_preflight_refreshes_expired_cache(monkeypatch):
    from platforms.chatgpt import http_client as http_client_module

    clients = []
    clock = [100.0]

    class FakeClient:
        def __init__(self, proxy_url=None):
            clients.append(self)

        def check_ip_location(self):
            return True, "JP"

        def check_nextauth_access(self):
            return True, 200

        def close(self):
            pass

    monkeypatch.setattr(http_client_module, "OpenAIHTTPClient", FakeClient)
    monkeypatch.setattr(tasks_module.time, "monotonic", lambda: clock[0])
    proxy = "http://proxy-expiry.example:8080"

    assert tasks_module._preflight_chatgpt_registration_proxy(proxy) == "JP"
    clock[0] += tasks_module._CHATGPT_PROXY_PREFLIGHT_CACHE_TTL_SECONDS + 0.1
    assert tasks_module._preflight_chatgpt_registration_proxy(proxy) == "JP"
    assert len(clients) == 2


def test_chatgpt_proxy_preflight_cache_isolated_by_exact_proxy(monkeypatch):
    from platforms.chatgpt import http_client as http_client_module

    proxies = []

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.proxy_url = proxy_url
            proxies.append(proxy_url)

        def check_ip_location(self):
            return True, "CL" if "first" in self.proxy_url else "CO"

        def check_nextauth_access(self):
            return True, 200

        def close(self):
            pass

    monkeypatch.setattr(http_client_module, "OpenAIHTTPClient", FakeClient)

    assert tasks_module._preflight_chatgpt_registration_proxy("http://first.example:8080") == "CL"
    assert tasks_module._preflight_chatgpt_registration_proxy("http://second.example:8080") == "CO"
    assert proxies == ["http://first.example:8080", "http://second.example:8080"]


def test_chatgpt_proxy_preflight_does_not_cache_failure(monkeypatch):
    from platforms.chatgpt import http_client as http_client_module

    clients = []

    class FakeClient:
        def __init__(self, proxy_url=None):
            self.index = len(clients)
            clients.append(self)

        def check_ip_location(self):
            return True, "CO"

        def check_nextauth_access(self):
            return (self.index > 0, 200 if self.index > 0 else 403)

        def close(self):
            pass

    monkeypatch.setattr(http_client_module, "OpenAIHTTPClient", FakeClient)
    proxy = "http://proxy-recovery.example:8080"

    with pytest.raises(tasks_module._RegistrationProxyPreflightError, match="NextAuth HTTP 403"):
        tasks_module._preflight_chatgpt_registration_proxy(proxy)

    assert tasks_module._preflight_chatgpt_registration_proxy(proxy) == "CO"
    assert len(clients) == 2


def test_only_typed_proxy_errors_reduce_proxy_score():
    proxy_error = tasks_module._RegistrationProxyPreflightError("route failed")

    assert tasks_module._registration_error_counts_as_proxy_failure(proxy_error) is True
    assert tasks_module._registration_error_counts_as_proxy_failure(RuntimeError("wrong otp")) is False


def test_chatgpt_high_concurrency_profile_caps_workers():
    assert tasks_module._register_concurrency_cap("chatgpt", {"high_concurrency": {"mode": "high"}}) == 10
    assert tasks_module._register_concurrency_cap("chatgpt", {"high_concurrency": {"mode": "extreme"}}) == 15
    assert tasks_module._register_concurrency_cap(
        "chatgpt",
        {"high_concurrency": {"mode": "custom", "concurrency": 99}},
    ) == 20
    assert tasks_module._register_retry_multiplier(
        {"high_concurrency": {"retry_multiplier": 99}}
    ) == 8
    assert tasks_module._register_retry_multiplier(
        {"identity_provider": "phone", "high_concurrency": {"retry_multiplier": 8}}
    ) == 1


def test_inline_gmail_alias_pool_caps_concurrency_by_base():
    pool_text = "\n".join(
        [
            "base+a@gmail.com----https://example.test/a",
            "base+b@gmail.com----https://example.test/b",
            "base+c@gmail.com----https://example.test/c",
        ]
    )
    assert tasks_module._inline_mailbox_concurrency_cap(pool_text, {}) == (1, 1, 1)
    assert tasks_module._inline_mailbox_concurrency_cap(
        pool_text,
        {"high_concurrency": {"gmail_base_concurrency": 2}},
    ) == (2, 1, 2)


def test_inline_outlook_alias_pool_caps_concurrency_by_base():
    pool_text = "\n".join(
        [
            "base+a@outlook.com----base@outlook.com----pass----client----refresh",
            "base+b@outlook.com----base@outlook.com----pass----client----refresh",
        ]
    )

    assert tasks_module._inline_mailbox_concurrency_cap(pool_text, {}) == (1, 1, 1)


def test_chatgpt_register_task_fails_when_workspace_join_fails(monkeypatch):
    class FakePlatform:
        def register(self, email=None, password=None):
            return Account(
                platform="chatgpt",
                email=email or "registered@example.com",
                password=password or "Secret123!",
                user_id="acct_123",
                extra={
                    "access_token": "access-token",
                    "workspace_join": {
                        "ok": False,
                        "error": "invite button not clicked",
                        "accept_result": {"error": "invite button not clicked"},
                    },
                },
            )

    monkeypatch.setattr(tasks_module, "get", lambda platform_name: object)
    monkeypatch.setattr(
        tasks_module,
        "_resolve_registration_proxy_for_platform",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        tasks_module,
        "_build_platform_instance",
        lambda *args, **kwargs: FakePlatform(),
    )
    monkeypatch.setattr(tasks_module, "_auto_upload_cpa", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_auto_push_any2api", lambda *args, **kwargs: None)

    logger = _FakeLogger()

    tasks_module._execute_register_task(
        {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "registered@example.com",
            "password": "Secret123!",
            "extra": {
                "identity_provider": "oauth_browser",
                "auto_chatgpt_workspace_join": True,
                "auto_chatgpt_plus_payment": False,
            },
        },
        logger,
    )

    assert logger.finished == (
        tasks_module.TASK_STATUS_FAILED,
        "Workspace Join 失败: invite button not clicked",
    )
    assert not any(event[0] == "success" for event in logger.events)


def test_phone_bind_task_passes_logger_and_browser_mode(monkeypatch):
    seen = {}

    class FakePhoneBindingService:
        def bind(self, **kwargs):
            seen.update(kwargs)
            kwargs["log_fn"]("phone bind step")
            return {"success_count": 1, "failure_count": 0, "phones": []}

    monkeypatch.setattr(tasks_module, "PhoneBindingService", FakePhoneBindingService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_phone_bind_task(
        {
            "platform": "chatgpt",
            "ids": [123],
            "fallback_ids": [],
            "phone_lines": "2025550101----https://relay.example.com/api/sms/recordText?key=RELAY_KEY",
            "browser_mode": "camoufox_headed",
            "bit_profile_id": "profile-1",
            "concurrency": 7,
        },
        logger,
    )

    assert seen["ids"] == [123]
    assert seen["browser_mode"] == "camoufox_headed"
    assert seen["bit_profile_id"] == "profile-1"
    assert seen["concurrency"] == 7
    assert getattr(seen["log_fn"], "__self__", None) is logger
    assert ("log", "phone bind step", {}) in logger.events
    assert logger.result_data == {"success_count": 1, "failure_count": 0, "phones": []}
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_codex_oauth_task_passes_logger_and_browser_mode(monkeypatch):
    seen = []

    class FakeCtfPlusAccountsService:
        def run_codex_oauth_browser(self, **kwargs):
            seen.append(kwargs)
            kwargs["log_fn"]("oauth step")
            return {"ok": True, "account_id": kwargs["account_id"], "email": "oauth@test.com"}

    monkeypatch.setattr(tasks_module, "CtfPlusAccountsService", FakeCtfPlusAccountsService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_codex_oauth_task(
        {
            "ids": [456],
            "browser_mode": "bitbrowser_hidden",
            "bit_profile_id": "profile-2",
            "concurrency": 9,
        },
        logger,
    )

    assert seen[0]["account_id"] == 456
    assert seen[0]["browser_mode"] == "bitbrowser_hidden"
    assert seen[0]["bit_profile_id"] == "profile-2"
    assert getattr(seen[0]["log_fn"], "__self__", None) is logger
    assert any(event[0] == "log" and event[1] == "oauth step" for event in logger.events)
    assert logger.result_data["success_count"] == 1
    assert logger.result_data["concurrency"] == 1
    assert logger.result_data["results"][0]["account_id"] == 456
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_codex_oauth_task_runs_multiple_accounts_without_capping_concurrency(monkeypatch):
    seen = []

    class FakeCtfPlusAccountsService:
        def run_codex_oauth_browser(self, **kwargs):
            seen.append(kwargs["account_id"])
            return {"ok": True, "account_id": kwargs["account_id"], "email": f"{kwargs['account_id']}@test.com"}

    monkeypatch.setattr(tasks_module, "CtfPlusAccountsService", FakeCtfPlusAccountsService, raising=False)
    logger = _FakeLogger()

    tasks_module._execute_codex_oauth_task(
        {
            "ids": [1, 2, 3],
            "browser_mode": "camoufox_headed",
            "concurrency": 99,
        },
        logger,
    )

    assert sorted(seen) == [1, 2, 3]
    assert logger.result_data["total"] == 3
    assert logger.result_data["success_count"] == 3
    assert logger.result_data["failure_count"] == 0
    assert logger.result_data["concurrency"] == 3
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_platform_action_task_finishes_cancelled_without_starting_runtime(monkeypatch):
    class FakeRuntime:
        def execute_action(self, *args, **kwargs):
            raise AssertionError("runtime should not start after cancellation")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()
    logger.cancel_requested = True

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_platform_action_task_marks_cancelled_after_runtime_cancel(monkeypatch):
    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check):
            assert cancel_check() is False
            logger.cancel_requested = True
            return ActionExecutionResult(ok=False, error="任务已取消")

    monkeypatch.setattr(tasks_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_platform_action_task(
        {
            "platform": "chatgpt",
            "account_id": 123,
            "action_id": "payment_link",
            "params": {"auto_checkout": "true"},
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_CANCELLED, "任务已取消")


def test_chatgpt_auto_plus_followup_generates_payment_link(monkeypatch):
    saved_accounts = []

    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, params))
            return {
                "ok": True,
                "data": {
                    "cashier_url": "https://checkout.example/plus",
                    "checkout_url": "https://checkout.example/plus",
                    "message": "Payment link generated.",
                },
            }

    monkeypatch.setattr(tasks_module, "save_account", lambda account: saved_accounts.append(dict(account.extra)))
    logger = FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {
                    "country": "US",
                    "currency": "USD",
                    "headless": "true",
                    "checkout_hold_seconds": 0,
                },
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    assert error == ""
    assert platform.calls == [
        (
            "payment_link",
            {
                "plan": "plus",
                "country": "US",
                "currency": "USD",
                "auto_checkout": "true",
                "payment_method": "paypal",
                "headless": "true",
                "checkout_timeout": 180,
                "checkout_hold_seconds": 0,
            },
        )
    ]
    assert account.extra["cashier_url"] == "https://checkout.example/plus"
    assert saved_accounts[-1]["cashier_url"] == "https://checkout.example/plus"
    assert ("cashier_url", "https://checkout.example/plus", {}) in logger.events
    assert account.status == tasks_module.AccountStatus.SUBSCRIBED
    assert account.extra["account_overview"]["plan_state"] == "subscribed"
    assert account.extra["account_overview"]["plan_name"] == "Plus"
    assert "Plus" in account.extra["account_overview"]["chips"]


def test_chatgpt_auto_plus_followup_logs_paypal_authorize_url_when_available(monkeypatch):
    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakePlatform:
        def execute_action(self, action_id, account, params):
            return {
                "ok": True,
                "data": {
                    "cashier_url": "https://pay.openai.com/c/pay/cs_live_demo",
                    "checkout_url": "https://pm-redirects.stripe.com/authorize/acct_x/sa_nonce_y",
                    "paypal_authorize_url": "https://pm-redirects.stripe.com/authorize/acct_x/sa_nonce_y",
                    "paypal_protocol_extract": {"ok": True},
                },
            }

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = FakeLogger()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={"extra": {"auto_chatgpt_plus_payment": True}},
        platform=FakePlatform(),
        account=account,
        logger=logger,
    )

    assert error == ""
    assert (
        "cashier_url",
        "https://pm-redirects.stripe.com/authorize/acct_x/sa_nonce_y",
        {},
    ) in logger.events
    assert any(
        event[0] == "log" and "原始 cashier_url: https://pay.openai.com/c/pay/cs_live_demo" in event[1]
        for event in logger.events
    )
    assert account.extra["cashier_url"] == "https://pay.openai.com/c/pay/cs_live_demo"


def test_chatgpt_auto_plus_followup_forwards_checkout_mode_and_record_har(monkeypatch):
    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {"access_token": "access-token"}

    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, dict(params)))
            return {"ok": True, "data": {"cashier_url": "https://checkout.example/plus"}}

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {
                    "country": "US",
                    "currency": "USD",
                    "headless": "false",
                    "checkout_mode": "camoufox_headed",
                    "record_har": "true",
                },
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    assert len(platform.calls) == 1
    forwarded = platform.calls[0][1]
    assert forwarded["checkout_mode"] == "camoufox_headed"
    assert forwarded["record_har"] == "true"


def test_chatgpt_auto_plus_followup_omits_unset_checkout_mode_and_record_har(monkeypatch):
    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {}

    class FakePlatform:
        def __init__(self):
            self.calls = []

        def execute_action(self, action_id, account, params):
            self.calls.append((action_id, dict(params)))
            return {"ok": True, "data": {}}

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = _FakeLogger()
    platform = FakePlatform()
    account = FakeAccount()

    tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={
            "extra": {
                "auto_chatgpt_plus_payment": True,
                "chatgpt_payment": {"country": "US", "currency": "USD"},
            }
        },
        platform=platform,
        account=account,
        logger=logger,
    )

    forwarded = platform.calls[0][1]
    assert "checkout_mode" not in forwarded
    assert "record_har" not in forwarded


def test_auto_upload_cpa_prefers_workspace_json_exports(monkeypatch, tmp_path):
    from core import config_store as config_store_module
    from platforms.chatgpt import cpa_upload as cpa_upload_module

    uploaded = []
    workspace_json = tmp_path / "workspace.json"
    workspace_json.write_text(
        json.dumps(
            {
                "email": "workspace@example.com",
                "access_token": "workspace-token",
                "account_id": "workspace-1",
                "chatgpt_account_id": "workspace-1",
                "chatgpt_plan_type": "k12",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        config_store_module.config_store,
        "get",
        lambda key, default="": (
            "https://cpa.example"
            if key == "cpa_api_url"
            else ("test-api-key" if key == "cpa_api_key" else default)
        ),
    )
    monkeypatch.setattr(
        cpa_upload_module,
        "upload_to_cpa",
        lambda token_data, **kwargs: (uploaded.append(dict(token_data)) or (True, "uploaded")),
    )

    account = Account(
        platform="chatgpt",
        email="base@example.com",
        password="Secret123!",
        token="base-token",
        user_id="personal-account",
        extra={
            "access_token": "base-token",
            "workspace_statuses": {
                "workspace-1": {
                    "status": "export_ok",
                    "json_path": str(workspace_json),
                }
            },
        },
    )
    logger = _FakeLogger()

    tasks_module._auto_upload_cpa(logger, account)

    assert len(uploaded) == 1
    assert uploaded[0]["access_token"] == "workspace-token"
    assert uploaded[0]["account_id"] == "workspace-1"
    assert any("workspace JSON 自动上传完成" in str(event[1]) for event in logger.events)


def test_auto_upload_cpa_reports_missing_configuration(monkeypatch):
    from core import config_store as config_store_module

    monkeypatch.setattr(config_store_module.config_store, "get", lambda key, default="": default)
    account = Account(
        platform="chatgpt",
        email="missing-cpa@example.com",
        password="Secret123!",
        token="access-token",
        user_id="account-1",
    )
    logger = _FakeLogger()

    tasks_module._auto_upload_cpa(logger, account)

    assert any("未配置 API URL" in str(event[1]) for event in logger.events)
    assert not any("已启用自动上传" in str(event[1]) for event in logger.events)


def test_auto_upload_cpa_reports_missing_api_key(monkeypatch):
    from core import config_store as config_store_module

    monkeypatch.setattr(
        config_store_module.config_store,
        "get",
        lambda key, default="": "https://cpa.example" if key == "cpa_api_url" else default,
    )
    account = Account(
        platform="chatgpt",
        email="missing-key@example.com",
        password="Secret123!",
        token="access-token",
        user_id="account-1",
    )
    logger = _FakeLogger()

    tasks_module._auto_upload_cpa(logger, account)

    assert any("未配置 API Key" in str(event[1]) for event in logger.events)
    assert not any("已启用自动上传" in str(event[1]) for event in logger.events)


def test_get_rt_task_forwards_record_har_to_platform_action(monkeypatch):
    seen_params = []

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            seen_params.append(dict(command.params))
            return ActionExecutionResult(ok=True, data={"message": "ok"})

    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [123],
            "browser_mode": "camoufox_headed",
            "record_har": "true",
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert seen_params[0]["record_har"] == "true"


def test_get_rt_task_uses_shared_phone_reuse_pool(monkeypatch):
    from platforms.chatgpt import browser_get_rt as browser_get_rt_module

    built = []
    callbacks = []

    class FakePhonePool:
        def __init__(self):
            self.cleaned = False

        def make_callback(self, *, label=""):
            callback = lambda: f"phone-for-{label}"
            callbacks.append((label, callback))
            return callback

        def cleanup(self):
            self.cleaned = True

    fake_pool = FakePhonePool()

    def fake_build_pool(**kwargs):
        built.append(dict(kwargs))
        return fake_pool, ""

    class FakeRuntime:
        def execute_action(self, command, *, log_fn=None, cancel_check=None):
            assert callable(command.params["phone_callback"])
            assert command.params["phone_reuse_count"] == "3"
            return ActionExecutionResult(ok=True, data={"phone": command.params["phone_callback"]()})

    monkeypatch.setattr(browser_get_rt_module, "build_get_rt_phone_reuse_pool", fake_build_pool)
    monkeypatch.setattr(runtime_module, "PlatformRuntime", FakeRuntime)
    logger = _FakeLogger()

    tasks_module._execute_get_rt_task(
        {
            "ids": [101, 102, 103],
            "browser_mode": "camoufox_headed",
            "sms_provider": "smspool",
            "smspool_api_key": "KEY",
            "phone_reuse_count": 2,
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")
    assert len(built) == 1
    assert built[0]["reuse_count"] == 3
    assert len(callbacks) == 3
    assert callbacks[0][0] == "1/3"
    assert callbacks[-1][0] == "3/3"
    assert fake_pool.cleaned is True


def test_chatgpt_auto_plus_followup_returns_error_when_payment_link_fails(monkeypatch):
    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {}

    class FakePlatform:
        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "checkout failed",
                "data": {
                    "cashier_url": "https://checkout.example/partial",
                },
            }

    saved_accounts = []
    monkeypatch.setattr(tasks_module, "save_account", lambda account: saved_accounts.append(dict(account.extra)))
    logger = FakeLogger()
    account = FakeAccount()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={"extra": {"auto_chatgpt_plus_payment": True}},
        platform=FakePlatform(),
        account=account,
        logger=logger,
    )

    assert error == "ChatGPT Plus 支付链接生成失败: checkout failed"
    assert account.extra["cashier_url"] == "https://checkout.example/partial"
    assert saved_accounts[-1]["cashier_url"] == "https://checkout.example/partial"
    assert ("cashier_url", "https://checkout.example/partial", {}) in logger.events


def test_chatgpt_auto_plus_followup_does_not_output_pay_url_when_protocol_extract_fails(monkeypatch):
    class FakeLogger(_FakeLogger):
        def add_cashier_url(self, url):
            self.events.append(("cashier_url", url, {}))

    class FakeAccount:
        platform = "chatgpt"
        email = "ctf@example.com"
        password = "Secret123!"
        extra = {}

    class FakePlatform:
        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "Stripe /confirm 响应缺少 pm-redirects.stripe.com/authorize URL",
                "data": {
                    "cashier_url": "https://pay.openai.com/c/pay/cs_live_demo",
                    "checkout_url": "https://pay.openai.com/c/pay/cs_live_demo",
                    "paypal_authorize_url": "",
                    "paypal_protocol_extract": {"ok": False, "error": "missing authorize"},
                },
            }

    monkeypatch.setattr(tasks_module, "save_account", lambda account: None)
    logger = FakeLogger()

    error = tasks_module._auto_followup_chatgpt_plus_payment(
        platform_name="chatgpt",
        payload={"extra": {"auto_chatgpt_plus_payment": True}},
        platform=FakePlatform(),
        account=FakeAccount(),
        logger=logger,
    )

    assert error.startswith("ChatGPT Plus 支付链接生成失败:")
    assert not any(event[0] == "cashier_url" for event in logger.events)
    assert not any(
        event[0] == "log" and "ChatGPT Plus 测试支付链接已生成: https://pay.openai.com" in event[1]
        for event in logger.events
    )


def test_platform_runtime_wires_log_fn_to_platform(monkeypatch):
    logs = []
    seen = {}

    class FakeSession:
        def __init__(self, engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"platform": "chatgpt"})()

    class FakePlatform:
        def __init__(self, config=None):
            self._log_fn = print

        def set_logger(self, logger):
            self._log_fn = logger

        def set_cancel_checker(self, checker):
            seen["cancel_check"] = checker

        def execute_action(self, action_id, account, params):
            self._log_fn("runtime platform log")
            assert self.is_cancel_requested() is False
            return {"ok": True, "data": {"message": "ok"}}

        def is_cancel_requested(self):
            return seen["cancel_check"]()

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="payment_link",
            params={"auto_checkout": "true"},
        ),
        log_fn=logs.append,
        cancel_check=lambda: False,
    )

    assert result.ok is True
    assert logs == ["runtime platform log"]
    assert seen["cancel_check"]() is False


def test_platform_runtime_persists_cashier_url_even_when_action_fails_after_link(monkeypatch):
    patched = {}

    class FakeSession:
        def __init__(self, engine):
            self.added = []
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt", "updated_at": None})()

        def add(self, model):
            self.added.append(model)

        def commit(self):
            self.committed = True

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": False,
                "error": "checkout failed",
                "data": {
                    "cashier_url": "https://checkout.stripe.com/c/pay/cs_test_link",
                    "message": "Payment link generated, but checkout failed.",
                },
            }

    def fake_patch_account_graph(session, model, **kwargs):
        patched.update(kwargs)

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", fake_patch_account_graph)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="payment_link",
            params={"auto_checkout": "true"},
        )
    )

    assert result.ok is False
    assert result.error == "checkout failed"
    assert patched["cashier_url"] == "https://checkout.stripe.com/c/pay/cs_test_link"
    assert patched["summary_updates"]["cashier_url"] == "https://checkout.stripe.com/c/pay/cs_test_link"


def test_platform_runtime_persists_get_rt_tokens_and_user_info(monkeypatch):
    patched = {}

    class FakeSession:
        def __init__(self, engine):
            self.added = []
            self.committed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, model_cls, account_id):
            return type("Model", (), {"id": account_id, "platform": "chatgpt", "updated_at": None})()

        def add(self, model):
            self.added.append(model)

        def commit(self):
            self.committed = True

    class FakePlatform:
        def __init__(self, config=None):
            pass

        def execute_action(self, action_id, account, params):
            return {
                "ok": True,
                "data": {
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                    "id_token": "id-token",
                    "account_id": "acct-123",
                    "email": "real@example.com",
                    "expired": "2026-06-10T15:00:00Z",
                    "last_refresh": "2026-06-10T14:00:00Z",
                    "type": "codex",
                    "profile": {"email": "real@example.com", "name": "Real User"},
                    "id_token_claims": {"email": "real@example.com", "sub": "auth0|abc"},
                },
            }

    def fake_patch_account_graph(session, model, **kwargs):
        patched.update(kwargs)

    monkeypatch.setattr(runtime_module, "Session", FakeSession)
    monkeypatch.setattr(runtime_module, "load_all", lambda: None)
    monkeypatch.setattr(runtime_module, "get", lambda platform: FakePlatform)
    monkeypatch.setattr(runtime_module, "build_platform_account", lambda session, model: object())
    monkeypatch.setattr(runtime_module, "patch_account_graph", fake_patch_account_graph)

    result = runtime_module.PlatformRuntime().execute_action(
        ActionExecutionCommand(
            platform="chatgpt",
            account_id=123,
            action_id="get_rt",
            params={},
        )
    )

    assert result.ok is True
    assert patched["credential_updates"]["access_token"] == "access-token"
    assert patched["credential_updates"]["refresh_token"] == "refresh-token"
    assert patched["credential_updates"]["id_token"] == "id-token"
    assert patched["credential_updates"]["account_id"] == "acct-123"
    summary = patched["summary_updates"]
    assert summary["remote_email"] == "real@example.com"
    assert summary["codex_oauth"]["account_id"] == "acct-123"
    assert summary["codex_oauth"]["profile"]["name"] == "Real User"
    assert summary["codex_oauth"]["id_token_claims"]["sub"] == "auth0|abc"
