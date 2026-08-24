"""SMS provider unit tests."""
from __future__ import annotations

import time

import pytest
from core.base_sms import (
    HeroSmsProvider,
    SmsBowerProvider,
    SmsActivation,
    SmsActivateProvider,
    create_sms_provider,
    create_phone_callbacks,
    SMS_ACTIVATE_SERVICES,
    SMS_ACTIVATE_COUNTRIES,
)
import core.base_sms as sms_module


class TestSmsBowerPricingAndRiskRotation:
    def test_full_prices_merge_documented_gold_partner_evidence(self, monkeypatch):
        provider = SmsBowerProvider("test")
        monkeypatch.setattr(provider, "get_countries", lambda: [
            {"id": 151, "eng": "Chile", "chn": "智利"},
        ])

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        def fake_request(params, **_kwargs):
            if params["action"] == "getPricesV3":
                return FakeResponse({
                    "151": {"dr": {
                        "3419": {"price": 0.027, "count": 2},
                        "3109": {"price": 0.07, "count": 182},
                    }},
                })
            if params["action"] == "getTopCountriesByService":
                return FakeResponse({"chile": {"3419": {"price": 0.027, "count": 2}}})
            return FakeResponse({})

        monkeypatch.setattr(provider, "_request", fake_request)

        rows = provider.get_top_countries("dr")
        partners = {item["provider_id"]: item for item in rows[0]["providers"]}

        assert partners["3419"]["rank"] == "gold"
        assert partners["3109"]["rank"] == ""

    def test_phone_callback_caps_configured_code_timeout_at_five_minutes(self, monkeypatch):
        timeouts = []

        class FakeProvider:
            auto_report_success_on_code = False

            def get_number(self, *, service, country=""):
                return SmsActivation("a1", "+15550000001", country=country)

            def get_code(self, activation_id, timeout=0):
                timeouts.append(timeout)
                return ""

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, _cleanup = create_phone_callbacks(
            "smsbower_api",
            {"smsbower_api_key": "test", "sms_code_timeout_seconds": 420},
            service="dr",
            country="33",
        )

        callback()
        callback()

        assert timeouts == [300]

    def test_phone_callback_allows_short_configured_code_timeout(self, monkeypatch):
        timeouts = []

        class FakeProvider:
            auto_report_success_on_code = False

            def get_number(self, *, service, country=""):
                return SmsActivation("a-short", "+15550000002", country=country)

            def get_code(self, activation_id, timeout=0):
                timeouts.append(timeout)
                return ""

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(
            sms_module,
            "create_sms_provider",
            lambda *_args, **_kwargs: FakeProvider(),
        )
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {"smsbower_api_key": "test", "sms_code_timeout_seconds": 75},
            service="dr",
            country="33",
        )

        callback()
        callback()
        cleanup()

        assert timeouts == [75]

    def test_phone_callback_rotates_explicit_country_pool_after_rejection(self, monkeypatch):
        requested = []

        class FakeProvider:
            auto_report_success_on_code = False

            def get_number(self, *, service, country=""):
                requested.append(country)
                return SmsActivation(f"a-{country}", f"+10000000{country}", country=country)

            def mark_send_failed(self, activation_id, reason=""):
                return None

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, _cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "33,151,10",
                # Exhaust tier on first soft fail so country pool can rotate.
                "sms_same_tier_retries": 1,
            },
            service="dr",
            country="33",
        )

        callback()
        callback.mark_send_failed("phone already used")
        callback.phase = "need_number"
        callback.activation = None
        callback()

        assert requested == ["33", "151"]

    def test_phone_callback_reports_when_entire_country_pool_is_exhausted(self, monkeypatch):
        requested = []

        class SoldOutProvider:
            def get_number(self, *, service, country=""):
                requested.append(country)
                raise RuntimeError("NO_NUMBERS")

        monkeypatch.setattr(
            sms_module,
            "create_sms_provider",
            lambda *_args, **_kwargs: SoldOutProvider(),
        )
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "11,6",
                "sms_no_numbers_wait_seconds": 0,
            },
            service="dr",
            country="11",
        )

        with pytest.raises(RuntimeError, match="候选国家池本次已全部尝试.*11,6"):
            callback()
        assert requested == ["11", "6"]
        cleanup()

    def test_phone_callback_expands_smsbower_countries_after_live_sellout(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            auto_report_success_on_code = False

            def __init__(self):
                self.provider_ids = "3253"
                self.excluded_provider_ids = set()
                self.max_price = 0.017

            def get_top_countries(self, service=None):
                return [
                    {"country": "12", "price": 0.004, "count": 100},
                    {"country": "6", "price": 0.007, "count": 100},
                    {"country": "151", "price": 0.02, "count": 100},
                ]

            def get_best_country(self, service=None, *, min_stock=20, max_price=0):
                return "33"

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids))
                if country == "33":
                    raise RuntimeError("NO_NUMBERS")
                return SmsActivation(f"a-{country}", f"+10000000{country}", country=country)

            def cancel(self, activation_id):
                return True

        provider = FakeProvider()
        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: provider)
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "smsbower_auto_country": True,
                "smsbower_auto_country_min_stock": 1,
                "smsbower_auto_country_max_price": 0.017,
            },
            service="dr",
            country="33",
        )

        assert callback() == "+100000006"
        assert requested == [("33", "3253"), ("6", "")]
        assert callback.activation.country == "6"
        cleanup()

    def test_phone_callback_filters_virtual_country_from_explicit_pool(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append(country)
                return SmsActivation(f"a-{country}", "+62800000000", country=country)

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "12,6",
                "smsbower_allow_virtual": False,
            },
            service="dr",
            country="6",
        )

        assert callback() == "+62800000000"
        assert requested == ["6"]
        cleanup()

    def test_phone_callback_stops_before_renting_from_only_virtual_country_pool(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append(country)
                raise AssertionError("virtual country must not reach provider")

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "12",
                "smsbower_allow_virtual": False,
            },
            service="dr",
            country="6",
        )

        with pytest.raises(RuntimeError, match="虚拟/VOIP"):
            callback()

        assert requested == []
        cleanup()

    def test_phone_callback_applies_provider_ids_per_country(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids))
                if country == "6":
                    raise RuntimeError("NO_NUMBERS")
                return SmsActivation("a-33", "+573000000000", country="33")

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "6,33",
                "smsbower_provider_ids_by_country": {
                    "6": ["3212"],
                    "33": ["3253"],
                },
            },
            service="dr",
            country="6",
        )

        assert callback() == "+573000000000"
        assert requested == [("6", "3212"), ("33", "3253")]
        cleanup()

    def test_phone_callback_waits_and_retries_selected_providers_when_sold_out(self, monkeypatch):
        requested = []
        logs = []
        clock = [0.0]

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids))
                if len(requested) <= 2:
                    raise RuntimeError("NO_NUMBERS")
                return SmsActivation("a-33", "+573000000000", country="33")

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        monkeypatch.setattr(sms_module.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(sms_module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "33",
                "smsbower_provider_ids_by_country": {"33": ["3253", "3243"]},
                "sms_no_numbers_wait_seconds": 120,
                "sms_no_numbers_retry_interval_seconds": 20,
            },
            service="dr",
            country="33",
            log_fn=logs.append,
        )

        assert callback() == "+573000000000"
        assert requested == [("33", "3253"), ("33", "3243"), ("33", "3243")]
        assert any("20 秒后重试" in line for line in logs)
        cleanup()

    def test_phone_callback_stays_on_same_provider_after_one_rejection(self, monkeypatch):
        """Soft fail keeps the same cheap tier and only re-rents a number."""
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids, self.strict_provider_ids))
                return SmsActivation(
                    f"a-{len(requested)}",
                    f"+6280000000{len(requested)}",
                    country=country,
                )

            def mark_send_failed(self, activation_id, reason=""):
                return None

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "6",
                "smsbower_provider_ids_by_country": {"6": ["2295", "3168"]},
                "sms_same_tier_retries": 3,
            },
            service="dr",
            country="6",
        )

        callback()
        callback.mark_send_failed("account_creation_failed")
        cleanup()
        callback.phase = "need_number"
        callback.completed = False
        callback()

        # Still on first provider after a single soft failure.
        assert requested == [
            ("6", "2295", True),
            ("6", "2295", True),
        ]
        cleanup()

    def test_phone_callback_skips_provider_when_number_is_already_in_use(self, monkeypatch):
        requested = []
        logs = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids, self.strict_provider_ids))
                return SmsActivation(
                    f"a-{len(requested)}",
                    f"+4915111111{len(requested)}",
                    country=country,
                )

            def mark_send_failed(self, activation_id, reason=""):
                return None

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "43,1",
                "smsbower_provider_ids_by_country": {
                    "43": ["3237"],
                    "1": ["3178"],
                },
                # The number-in-use branch must not wait for this threshold.
                "sms_same_tier_retries": 8,
            },
            service="dr",
            country="43",
            log_fn=logs.append,
        )

        callback()
        callback.mark_send_failed("phone_number_in_use: Phone number already in use")
        cleanup()
        callback.phase = "need_number"
        callback.activation = None
        callback.completed = False
        callback()

        assert requested == [
            ("43", "3237", True),
            ("1", "3178", True),
        ]
        assert any("跳过当前档位" in line for line in logs)
        cleanup()

    def test_phone_callback_moves_to_next_provider_before_next_country(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids, self.strict_provider_ids))
                return SmsActivation(
                    f"a-{len(requested)}",
                    f"+4915222222{len(requested)}",
                    country=country,
                )

            def mark_send_failed(self, activation_id, reason=""):
                return None

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "43,1",
                "smsbower_provider_ids_by_country": {
                    "43": ["3237", "3238"],
                    "1": ["3178"],
                },
            },
            service="dr",
            country="43",
        )

        callback()
        callback.mark_send_failed("Phone number already in use")
        cleanup()
        callback.phase = "need_number"
        callback.activation = None
        callback.completed = False
        callback()

        assert requested == [
            ("43", "3237", True),
            ("43", "3238", True),
        ]
        cleanup()

    def test_phone_callback_rotates_selected_provider_after_tier_exhausted(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids, self.strict_provider_ids))
                return SmsActivation(
                    f"a-{len(requested)}",
                    f"+6280000000{len(requested)}",
                    country=country,
                )

            def mark_send_failed(self, activation_id, reason=""):
                return None

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "6",
                "smsbower_provider_ids_by_country": {"6": ["2295", "3168"]},
                "sms_same_tier_retries": 1,
            },
            service="dr",
            country="6",
        )

        callback()
        callback.mark_send_failed("account_creation_failed")
        cleanup()
        callback.phase = "need_number"
        callback.completed = False
        callback()

        assert requested == [
            ("6", "2295", True),
            ("6", "3168", True),
        ]
        cleanup()

    def test_phone_callback_keeps_country_while_same_tier_retries(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids, self.strict_provider_ids))
                return SmsActivation(
                    f"a-{len(requested)}",
                    f"+310000000{len(requested)}",
                    country=country,
                )

            def mark_send_failed(self, activation_id, reason=""):
                return None

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "48,6",
                "smsbower_provider_ids_by_country": {
                    "48": ["2442"],
                    "6": ["2295"],
                },
                "sms_same_tier_retries": 3,
            },
            service="dr",
            country="48",
        )

        for _ in range(3):
            callback()
            callback.mark_send_failed("未收到验证码")
            cleanup()
            callback.phase = "need_number"
            callback.completed = False

        # First 2 soft fails stay on NL; 3rd exhausts tier and moves to ID.
        assert requested == [
            ("48", "2442", True),
            ("48", "2442", True),
            ("48", "2442", True),
        ]
        callback()
        assert requested[-1] == ("6", "2295", True)
        cleanup()

    def test_phone_callback_default_same_tier_retry_limit_is_two(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids, self.strict_provider_ids))
                return SmsActivation(
                    f"a-{len(requested)}",
                    f"+310000001{len(requested)}",
                    country=country,
                )

            def mark_send_failed(self, activation_id, reason=""):
                return None

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "48,6",
                "smsbower_provider_ids_by_country": {
                    "48": ["2442"],
                    "6": ["2295"],
                },
            },
            service="dr",
            country="48",
        )

        for _ in range(2):
            callback()
            callback.mark_send_failed("未收到验证码")
            cleanup()
            callback.phase = "need_number"
            callback.activation = None
            callback.completed = False

        callback()

        assert requested == [
            ("48", "2442", True),
            ("48", "2442", True),
            ("6", "2295", True),
        ]
        cleanup()

    def test_phone_callback_cools_exhausted_tier_across_controllers(self, monkeypatch):
        """A newly created account flow skips a tier cooling for 30 minutes."""
        requested = []
        clock = [1000.0]
        monkeypatch.setattr(sms_module.time, "monotonic", lambda: clock[0])

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids))
                return SmsActivation(
                    f"a-{len(requested)}",
                    f"+628000010{len(requested)}",
                    country=country,
                )

            def mark_send_failed(self, activation_id, reason=""):
                return None

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        config = {
            "smsbower_api_key": "test",
            "sms_countries": "6",
            "smsbower_provider_ids_by_country": {"6": ["2295", "3168"]},
            "sms_same_tier_retries": 1,
            "sms_tier_cooldown_minutes": 30,
        }

        first, cleanup_first = create_phone_callbacks(
            "smsbower_api", config, service="dr", country="6"
        )
        first()
        first.mark_send_failed("account_creation_failed")
        cleanup_first()

        second, cleanup_second = create_phone_callbacks(
            "smsbower_api", config, service="dr", country="6"
        )
        second()
        cleanup_second()
        assert requested == [("6", "2295"), ("6", "3168")]

        clock[0] += 30 * 60 + 1
        third, cleanup_third = create_phone_callbacks(
            "smsbower_api", config, service="dr", country="6"
        )
        third()
        assert requested[-1] == ("6", "2295")
        cleanup_third()

    def test_phone_callback_cools_no_inventory_tier(self, monkeypatch):
        requested = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = ""
                self.strict_provider_ids = False
                self.excluded_provider_ids = set()

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids))
                if self.provider_ids == "2295":
                    raise RuntimeError("NO_NUMBERS")
                return SmsActivation("a-available", "+62800002000", country=country)

            def cancel(self, activation_id):
                return True

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        config = {
            "smsbower_api_key": "test",
            "sms_countries": "6",
            "smsbower_provider_ids_by_country": {"6": ["2295", "3168"]},
            "sms_tier_cooldown_minutes": 30,
        }

        first, cleanup_first = create_phone_callbacks(
            "smsbower_api", config, service="dr", country="6"
        )
        first()
        cleanup_first()

        second, cleanup_second = create_phone_callbacks(
            "smsbower_api", config, service="dr", country="6"
        )
        second()
        assert requested == [("6", "2295"), ("6", "3168"), ("6", "3168")]
        cleanup_second()

    def test_herosms_get_code_keeps_configured_hard_timeout(self, monkeypatch):
        provider = HeroSmsProvider("test")
        captured = []
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "activation_id": "act-hard-timeout",
            "acquired_at": time.time(),
        })
        monkeypatch.setattr(
            provider,
            "wait_for_code",
            lambda activation_id, timeout: captured.append(timeout) or None,
        )

        assert provider.get_code("act-hard-timeout", timeout=360) == ""
        assert captured == [360]

    def test_herosms_wait_for_code_honors_task_cancellation(self, monkeypatch):
        provider = HeroSmsProvider("test")
        provider.set_cancel_check(lambda: True)
        monkeypatch.setattr(
            provider,
            "get_status_v2",
            lambda activation_id: pytest.fail("cancelled polling must not call provider"),
        )

        assert provider.wait_for_code("act-cancelled", timeout=360) is None

    def test_phone_callback_keeps_explicit_country_pool_strict(self, monkeypatch):
        requested = []
        top_country_queries = []

        class FakeProvider(SmsBowerProvider):
            def __init__(self):
                self.provider_ids = "3253"
                self.excluded_provider_ids = set()
                self.max_price = 0.017

            def get_number(self, *, service, country=""):
                requested.append((country, self.provider_ids))
                raise RuntimeError("NO_NUMBERS")

            def get_top_countries(self, service=None):
                top_country_queries.append(service)
                return [{"country": "12", "price": 0.004, "count": 100}]

        monkeypatch.setattr(sms_module, "create_sms_provider", lambda *_args, **_kwargs: FakeProvider())
        callback, cleanup = create_phone_callbacks(
            "smsbower_api",
            {
                "smsbower_api_key": "test",
                "sms_countries": "33",
                "smsbower_auto_country": True,
            },
            service="dr",
            country="33",
        )

        with pytest.raises(RuntimeError, match="NO_NUMBERS"):
            callback()

        assert requested == [("33", "3253")]
        assert top_country_queries == []
        cleanup()

    def test_single_explicit_smsbower_provider_is_not_auto_excluded(self):
        provider = SmsBowerProvider("test", provider_ids="3253", provider_reject_threshold=1)
        provider.current_activation = SmsActivation(
            activation_id="act-1",
            phone_number="+15550001111",
            metadata={"number_info": {"providerId": "3253"}},
        )
        provider._stop_reuse = lambda reason: None

        provider.mark_send_failed("act-1", "phone rejected")

        assert provider.excluded_provider_ids == set()

    def test_parses_v3_partner_prices_for_chile_and_colombia(self, monkeypatch):
        provider = SmsBowerProvider("test")
        monkeypatch.setattr(provider, "get_countries", lambda: [
            {"id": 33, "eng": "Colombia", "chn": "哥伦比亚"},
            {"id": 151, "eng": "Chile", "chn": "智利"},
        ])

        rows = provider._parse_smsbower_top_countries({
            "33": {"dr": {"3170": {"price": 0.03, "count": 20}}},
            "151": {"dr": {
                "4120": {"price": 0.02, "count": 8},
                "4121": {"price": 0.025, "count": 4},
            }},
        }, service="dr")

        assert [row["country"] for row in rows] == ["151", "33"]
        assert rows[0]["name"] == "智利"
        assert rows[0]["price"] == 0.02
        assert rows[0]["count"] == 12
        assert rows[0]["provider_count"] == 2

    def test_parses_v2_price_tiers(self, monkeypatch):
        provider = SmsBowerProvider("test")
        monkeypatch.setattr(provider, "get_countries", lambda: [
            {"id": 33, "eng": "Colombia", "chn": "哥伦比亚"},
        ])

        rows = provider._parse_smsbower_top_countries({
            "33": {"dr": {"0.03": 10, "0.04": 5}},
        }, service="dr")

        assert rows[0]["price"] == 0.03
        assert rows[0]["count"] == 15

    def test_best_country_accepts_low_cost_countries(self, monkeypatch):
        provider = SmsBowerProvider("test")
        monkeypatch.setattr(provider, "get_top_countries", lambda service=None: [
            {"country": "151", "price": 0.02, "count": 2},
            {"country": "33", "price": 0.03, "count": 50},
        ])

        assert provider.get_best_country("dr", min_stock=10, max_price=0.05) == "33"

    def test_rejected_provider_is_added_to_except_provider_ids(self, monkeypatch):
        provider = SmsBowerProvider("test", provider_reject_threshold=2)
        provider.current_activation = SmsActivation(
            activation_id="activation",
            phone_number="+15550000001",
            metadata={"number_info": {"providerId": "3170"}},
        )
        monkeypatch.setattr(provider, "_stop_reuse", lambda _reason: None)

        provider.mark_send_failed("activation", "phone already used")
        provider.mark_send_failed("activation", "risk blocked")

        assert "3170" in provider.excluded_provider_ids

    def test_get_number_v2_receives_provider_filters(self, monkeypatch):
        provider = SmsBowerProvider(
            "test",
            provider_ids="3170,4120",
            except_provider_ids="9999",
            min_price=0.01,
            max_price=0.05,
        )
        monkeypatch.setattr(provider, "get_prices", lambda **_kwargs: {})
        calls = []

        class FakeResponse:
            text = '{"activationId":"a1","phoneNumber":"123"}'

            def json(self):
                return {"activationId": "a1", "phoneNumber": "123"}

        def fake_request(params, **_kwargs):
            calls.append(dict(params))
            return FakeResponse()

        monkeypatch.setattr(provider, "_request", fake_request)

        provider._request_number_raw("dr", "33")

        assert calls[0]["providerIds"] == "3170,4120"
        assert calls[0]["exceptProviderIds"] == "9999"
        assert calls[0]["minPrice"] == 0.01
        assert calls[0]["maxPrice"] == 0.05

    def test_get_number_v2_retries_country_without_sold_out_provider_filter(self, monkeypatch):
        provider = SmsBowerProvider(
            "test",
            provider_ids="3419",
            max_price=0.13,
        )
        monkeypatch.setattr(provider, "get_prices", lambda **_kwargs: {})
        calls = []

        class FakeResponse:
            def __init__(self, payload=None, text=""):
                self.payload = payload
                self.text = text

            def json(self):
                if self.payload is None:
                    raise ValueError("not json")
                return self.payload

        def fake_request(params, **_kwargs):
            calls.append(dict(params))
            if params.get("action") == "getNumberV2" and params.get("providerIds"):
                return FakeResponse(text="NO_NUMBERS")
            return FakeResponse({
                "activationId": "a2",
                "phoneNumber": "573001234567",
                "countryPhoneCode": "57",
                "providerId": "3253",
            })

        monkeypatch.setattr(provider, "_request", fake_request)

        result = provider._request_number_raw("dr", "33")

        assert result["activationId"] == "a2"
        assert calls[0]["country"] == "33"
        assert calls[0]["providerIds"] == "3419"
        assert calls[1]["country"] == "33"
        assert "providerIds" not in calls[1]

    def test_smsbower_no_numbers_error_uses_smsbower_label(self, monkeypatch):
        provider = SmsBowerProvider("test", provider_ids="3253", max_price=0.13)
        monkeypatch.setattr(provider, "get_prices", lambda **_kwargs: {})

        class FakeResponse:
            text = "NO_NUMBERS"

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: FakeResponse())

        with pytest.raises(RuntimeError, match=r"^SMSBower 获取号码失败: V2=NO_NUMBERS; V1=NO_NUMBERS$"):
            provider._request_number_raw("dr", "33")

    def test_rejected_number_is_cancelled_before_next_number(self, monkeypatch):
        provider = SmsBowerProvider("test")
        first = SmsActivation("a1", "+15550000001", country="33")
        second = SmsActivation("a2", "+15550000002", country="33")
        provider._rejected_numbers.add(first.phone_number)
        activations = iter([first, second])
        cancelled = []
        monkeypatch.setattr(HeroSmsProvider, "get_number", lambda self, **_kwargs: next(activations))
        monkeypatch.setattr(provider, "cancel", lambda activation_id: cancelled.append(activation_id) or True)

        result = provider.get_number(service="dr", country="33")

        assert result is second
        assert cancelled == ["a1"]


class TestSmsActivateServiceMapping:
    def test_cursor_maps_to_ot(self):
        assert SMS_ACTIVATE_SERVICES["cursor"] == "ot"

    def test_chatgpt_maps_to_dr(self):
        assert SMS_ACTIVATE_SERVICES["chatgpt"] == "dr"

    def test_default_exists(self):
        assert "default" in SMS_ACTIVATE_SERVICES


class TestSmsActivateCountryMapping:
    def test_us_maps_to_187(self):
        assert SMS_ACTIVATE_COUNTRIES["us"] == "187"

    def test_ru_maps_to_0(self):
        assert SMS_ACTIVATE_COUNTRIES["ru"] == "0"

    def test_th_maps_to_52(self):
        assert SMS_ACTIVATE_COUNTRIES["th"] == "52"

    def test_default_exists(self):
        assert "default" in SMS_ACTIVATE_COUNTRIES


class TestCreateSmsProvider:
    def test_sms_activate(self):
        provider = create_sms_provider("sms_activate", {"sms_activate_api_key": "test123"})
        assert isinstance(provider, SmsActivateProvider)
        assert provider.api_key == "test123"

    def test_sms_activate_missing_key(self):
        with pytest.raises(RuntimeError, match="未配置"):
            create_sms_provider("sms_activate", {})

    def test_herosms(self):
        provider = create_sms_provider("herosms", {"herosms_api_key": "hero123"})
        assert isinstance(provider, HeroSmsProvider)
        assert provider.api_key == "hero123"
        assert provider.default_service == "dr"
        assert provider.default_country == "187"

    def test_herosms_reuse_flag_parses_string_false(self):
        provider = create_sms_provider(
            "herosms",
            {
                "herosms_api_key": "hero123",
                "register_reuse_phone_to_max": "false",
            },
        )
        assert isinstance(provider, HeroSmsProvider)
        assert provider.reuse_phone_to_max is False

    def test_herosms_missing_key(self):
        with pytest.raises(RuntimeError, match="HeroSMS 未配置"):
            create_sms_provider("herosms", {})

    def test_unknown_provider(self):
        with pytest.raises(RuntimeError, match="未知"):
            create_sms_provider("unknown", {})


class TestCreatePhoneCallbacks:
    def test_returns_tuple(self):
        # This will fail on actual API call, but we can test the structure
        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="cursor",
        )
        assert callable(callback)
        assert callable(cleanup)

    def test_provider_is_created_lazily_and_cleanup_cancels_pending_activation(self, monkeypatch):
        events = []
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_1", phone_number="+15551234567")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return ""

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="us",
            log_fn=logs.append,
        )

        assert events == []
        assert callback() == "+15551234567"
        cleanup()
        assert ("get_number", "chatgpt", "us") in events
        assert ("cancel", "act_1") in events
        assert any("准备租用手机号" in item for item in logs)
        assert any("已成功租到号码" in item for item in logs)
        assert any("已释放未完成验证的号码" in item for item in logs)

    def test_cleanup_does_not_cancel_after_success(self, monkeypatch):
        events = []
        logs = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_2", phone_number="+15557654321")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "123456"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            log_fn=logs.append,
        )

        assert callback() == "+15557654321"
        assert callback() == "123456"
        cleanup()
        assert ("report_success", "act_2") in events
        assert ("cancel", "act_2") not in events
        assert any("等待短信验证码" in item for item in logs)
        assert any("短信验证成功" in item for item in logs)

    def test_deferred_success_provider_cancels_on_cleanup_without_external_confirmation(self, monkeypatch):
        events = []

        class FakeProvider:
            auto_report_success_on_code = False

            def get_number(self, *, service: str, country: str = ""):
                events.append(("get_number", service, country))
                return SmsActivation(activation_id="act_deferred", phone_number="+15550001111")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "111222"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="cursor",
        )

        assert callback() == "+15550001111"
        assert callback() == "111222"
        cleanup()
        assert ("report_success", "act_deferred") not in events
        assert ("cancel", "act_deferred") in events

    def test_explicit_report_success_completes_deferred_provider(self, monkeypatch):
        events = []

        class FakeProvider:
            auto_report_success_on_code = False

            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_verified", phone_number="+15550002222")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                return "222333"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms_api",
            {"herosms_api_key": "test"},
            service="chatgpt",
        )

        assert callback() == "+15550002222"
        assert callback() == "222333"
        callback.report_success()
        cleanup()
        assert ("report_success", "act_verified") in events
        assert ("cancel", "act_verified") not in events

    def test_first_number_fetch_failure_does_not_poison_future_retries(self, monkeypatch):
        events = []

        class FakeProvider:
            def __init__(self):
                self.calls = 0

            def get_number(self, *, service: str, country: str = ""):
                self.calls += 1
                events.append(("get_number", self.calls, service, country))
                if self.calls == 1:
                    raise RuntimeError("temporary failure")
                return SmsActivation(activation_id="act_retry", phone_number="+66123456789")

            def get_code(self, activation_id: str, *, timeout: int = 120) -> str:
                events.append(("get_code", activation_id, timeout))
                return "654321"

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

            def report_success(self, activation_id: str) -> bool:
                events.append(("report_success", activation_id))
                return True

        provider = FakeProvider()
        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: provider)

        callback, cleanup = create_phone_callbacks(
            "sms_activate",
            {"sms_activate_api_key": "test"},
            service="chatgpt",
            country="th",
        )

        with pytest.raises(RuntimeError, match="temporary failure"):
            callback()

        assert callback() == "+66123456789"
        assert callback() == "654321"
        cleanup()
        assert ("report_success", "act_retry") in events

    def test_herosms_number_fetch_failure_releases_verify_lock(self, monkeypatch):
        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                raise RuntimeError("temporary failure")

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
        )

        with pytest.raises(RuntimeError, match="temporary failure"):
            callback()

        assert callback._verify_lock_acquired is False
        cleanup()

    def test_herosms_one_shot_callbacks_can_acquire_numbers_concurrently(self, monkeypatch):
        """Reuse-disabled callbacks must not hold the transaction-wide reuse lock."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        worker_count = 5
        acquisition_barrier = threading.Barrier(worker_count)
        state = {"created": 0, "active": 0, "max_active": 0}
        state_lock = threading.Lock()

        class ParallelHeroProvider(HeroSmsProvider):
            def __init__(self, worker_number):
                super().__init__("test-key", reuse_phone_to_max=False)
                self.worker_number = worker_number

            def _request_number_raw(self, service: str, country: str):
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                try:
                    acquisition_barrier.wait(timeout=3)
                finally:
                    with state_lock:
                        state["active"] -= 1
                return {
                    "activationId": f"act_{self.worker_number}",
                    "phoneNumber": f"555000000{self.worker_number}",
                    "countryPhoneCode": "1",
                }

            def cancel(self, activation_id: str) -> bool:
                return True

        def build_provider(_provider_key, _config):
            with state_lock:
                state["created"] += 1
                worker_number = state["created"]
            return ParallelHeroProvider(worker_number)

        monkeypatch.setattr(sms_module, "create_sms_provider", build_provider)
        callbacks = [
            create_phone_callbacks(
                "herosms",
                {
                    "herosms_api_key": "test-key",
                    "register_reuse_phone_to_max": False,
                },
                service="chatgpt",
                country="187",
            )
            for _ in range(worker_count)
        ]

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            numbers = list(pool.map(lambda pair: pair[0](), callbacks))

        assert len(set(numbers)) == worker_count
        assert state["max_active"] == worker_count
        assert all(callback._verify_lock_acquired is False for callback, _cleanup in callbacks)
        for _callback, cleanup in callbacks:
            cleanup()

    def test_mark_send_succeeded_delegates_to_provider(self, monkeypatch):
        events = []

        class FakeProvider:
            def get_number(self, *, service: str, country: str = ""):
                return SmsActivation(activation_id="act_sent", phone_number="+15551234567")

            def mark_send_succeeded(self, activation_id: str) -> None:
                events.append(("mark_send_succeeded", activation_id))

            def cancel(self, activation_id: str) -> bool:
                events.append(("cancel", activation_id))
                return True

        monkeypatch.setattr("core.base_sms.create_sms_provider", lambda provider_key, config: FakeProvider())

        callback, cleanup = create_phone_callbacks(
            "herosms",
            {"herosms_api_key": "test"},
            service="chatgpt",
        )

        assert callback() == "+15551234567"
        callback.mark_send_succeeded()
        cleanup()
        assert ("mark_send_succeeded", "act_sent") in events


class TestSmsActivateProviderCountryResolution:
    def test_get_number_accepts_numeric_country_id(self, monkeypatch):
        captured = {}

        def fake_request(self, action: str, **params):
            captured["action"] = action
            captured["params"] = params
            return "NO_NUMBERS"

        monkeypatch.setattr(SmsActivateProvider, "_request", fake_request)
        provider = SmsActivateProvider("test123", default_country="ru")

        with pytest.raises(RuntimeError, match="NO_NUMBERS|无可用号码"):
            provider.get_number(service="chatgpt", country="52")

        assert captured["action"] == "getNumber"
        assert captured["params"]["country"] == "52"


class TestHeroSmsProvider:
    def test_get_number_uses_v2_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
        calls = []

        class FakeResp:
            text = '{"activationId":"act_1","phoneNumber":"5551234","countryPhoneCode":"1","activationCost":"0.6"}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"activationId": "act_1", "phoneNumber": "5551234", "countryPhoneCode": "1", "activationCost": "0.6"}

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params)
            return FakeResp()

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        activation = provider.get_number(service="chatgpt", country="187")

        assert activation.activation_id == "act_1"
        assert activation.phone_number == "+15551234"
        assert any(call["action"] == "getNumberV2" for call in calls)

    def test_get_number_falls_back_to_v1_text(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
        calls = []

        class FakeResp:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

            def json(self):
                raise ValueError("not json")

        def fake_get(url, params, timeout=30, proxies=None):
            calls.append(params["action"])
            if params["action"] == "getNumberV2":
                return FakeResp("BAD")
            return FakeResp("ACCESS_NUMBER:act_2:15557654321")

        monkeypatch.setattr("core.base_sms.requests.get", fake_get)
        provider = HeroSmsProvider("hero123")
        activation = provider.get_number(service="chatgpt", country="187")

        assert activation.activation_id == "act_2"
        assert activation.phone_number == "+15557654321"
        assert calls[-2:] == ["getNumberV2", "getNumber"]

    def test_get_number_does_not_publish_one_shot_activation_to_reuse_cache(
        self,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setattr(
            sms_module,
            "hero_sms_cache_file",
            lambda: tmp_path / ".herosms_phone_cache.json",
        )
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", None)
        provider = HeroSmsProvider("hero123", reuse_phone_to_max=False)
        monkeypatch.setattr(
            provider,
            "_request_number_raw",
            lambda service, country: {
                "activationId": "one_shot_1",
                "phoneNumber": "15550000001",
                "countryPhoneCode": "1",
            },
        )

        activation = provider.get_number(service="chatgpt", country="187")

        assert activation.activation_id == "one_shot_1"
        assert sms_module._HERO_SMS_CACHE is None
        assert not sms_module.hero_sms_cache_file().exists()

    def test_get_code_skips_attempted_sms_event(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_3",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        provider = HeroSmsProvider("hero123")
        first = {"status": "ok", "code": "111111", "sms_key": "sms_1", "allow_same_code": True}
        second = {"status": "ok", "code": "222222", "sms_key": "sms_2", "allow_same_code": True}
        results = [first, second]

        monkeypatch.setattr(provider, "get_status_v2", lambda activation_id: results.pop(0))
        monkeypatch.setattr(provider, "get_status", lambda activation_id: {"status": "wait_code"})
        monkeypatch.setattr(provider, "get_active_activations", lambda: [])
        monkeypatch.setattr(provider, "request_resend_sms", lambda activation_id: True)

        assert provider.get_code("act_3", timeout=1) == "111111"
        provider.mark_code_failed("act_3", "invalid otp")
        assert provider.get_code("act_3", timeout=1) == "222222"

    def test_mark_send_succeeded_sets_sms_sent_status(self, monkeypatch):
        calls = []
        provider = HeroSmsProvider("hero123")
        monkeypatch.setattr(provider, "set_status", lambda activation_id, status: calls.append((activation_id, status)) or "ACCESS_READY")

        provider.mark_send_succeeded("act_4")

        assert calls == [("act_4", 1)]

    def test_mark_code_failed_triggers_openai_and_herosms_resend(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_5",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        events = []
        provider = HeroSmsProvider("hero123")
        provider.last_code_result = {"code": "333333", "sms_key": "sms_3"}
        provider.set_resend_callback(lambda: events.append(("openai_resend",)))
        monkeypatch.setattr(provider, "request_resend_sms", lambda activation_id: events.append(("hero_resend", activation_id)) or True)

        provider.mark_code_failed("act_5", "invalid otp")

        assert ("openai_resend",) in events
        assert ("hero_resend", "act_5") in events
        assert "333333" in sms_module._HERO_SMS_CACHE["used_codes"]
        assert "sms_3" in sms_module._HERO_SMS_CACHE["attempted_sms_keys"]

    def test_report_success_finishes_activation_when_reuse_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sms_module, "hero_sms_cache_file", lambda: tmp_path / ".herosms_phone_cache.json")
        monkeypatch.setattr(sms_module, "_HERO_SMS_CACHE", {
            "api_key_hash": sms_module._hash_secret("hero123"),
            "service": "dr",
            "country": "187",
            "activation_id": "act_6",
            "phone_number": "+15550000000",
            "acquired_at": sms_module.time.time(),
            "use_count": 0,
            "used_codes": set(),
            "attempted_sms_keys": set(),
            "reuse_stopped": False,
        })
        events = []
        provider = HeroSmsProvider("hero123", reuse_phone_to_max=False)
        provider.last_code_result = {"code": "444444", "sms_key": "sms_4"}
        monkeypatch.setattr(provider, "finish_activation", lambda activation_id: events.append(("finish", activation_id)) or True)

        assert provider.report_success("act_6") is True

        assert events == [("finish", "act_6")]
        assert sms_module._HERO_SMS_CACHE is None


class TestSmsActivation:
    def test_dataclass(self):
        a = SmsActivation(activation_id="123", phone_number="+79001234567")
        assert a.activation_id == "123"
        assert a.phone_number == "+79001234567"
        assert a.country == ""

    def test_with_country(self):
        a = SmsActivation(activation_id="1", phone_number="+1555", country="us")
        assert a.country == "us"
