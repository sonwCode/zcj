def test_build_info_prefers_explicit_public_sha(monkeypatch):
    from core import build_info

    monkeypatch.setenv("APP_GIT_SHA", "0123456789abcdef")
    monkeypatch.setenv("APP_BUILD_TIME", "2026-08-24T02:30:00Z")

    info = build_info.get_build_info()

    assert info["git_sha"] == "0123456789abcdef"
    assert info["build_time"] == "2026-08-24T02:30:00Z"
    assert info["started_at"].endswith("Z")
    assert build_info.build_identity().endswith("@0123456789abcdef")


def test_build_info_rejects_non_sha_environment_value(monkeypatch):
    from core import build_info

    monkeypatch.setenv("APP_GIT_SHA", "secret-looking-but-not-a-sha")
    monkeypatch.setattr(build_info, "_git_sha_from_checkout", lambda: "abcdef012345")

    assert build_info.get_build_info()["git_sha"] == "abcdef012345"


def test_system_identity_and_scheduler_routes_keep_legacy_aliases():
    from api.system import router

    paths = {route.path for route in router.routes}

    assert "/system/version" in paths
    assert "/version" in paths
    assert "/system/scheduler/status" in paths
    assert "/scheduler/status" in paths
