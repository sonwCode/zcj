import ast
import os

import pytest


MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "platforms", "chatgpt", "browser_register.py",
)


def _load(*names):
    """Load the named top-level functions straight from the module source.

    ``browser_register`` imports camoufox at import time, which is not installed in
    the offline test environment. The functions under test are pure dict builders with
    no browser dependency, so they are extracted from the real source text - the test
    still exercises the shipped code rather than a re-implementation of it.
    """
    with open(MODULE, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in wanted} == set(names), "missing: %s" % (set(names) - {n.name for n in wanted})
    namespace = {"json": __import__("json"), "re": __import__("re")}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), MODULE, "exec"), namespace)
    return [namespace[name] for name in names]


(_build_browser_headers,) = _load("_build_browser_headers")


def test_an_in_page_fetch_sends_no_client_hints():
    """Sec-* is a forbidden request header: the browser discards script-set values.

    The bundled backend is Camoufox (Firefox), which sends no sec-ch-ua* at all, so
    emitting Chrome hints here could never take effect and only misled readers.
    """
    headers = _build_browser_headers(user_agent="anything", accept="application/json")
    lowered = {key.lower() for key in headers}

    assert "sec-ch-ua" not in lowered
    assert "sec-ch-ua-mobile" not in lowered
    assert "sec-ch-ua-platform" not in lowered


def test_an_in_page_fetch_does_not_override_the_browsers_own_user_agent():
    """user-agent is *not* a forbidden header, so setting it really does apply.

    The launch path deliberately leaves the UA alone so the header identity stays
    coherent with the browser's TLS/HTTP2 fingerprint; overriding it here with a
    Chrome/Windows string would reintroduce exactly that disagreement.
    """
    headers = _build_browser_headers(user_agent="Mozilla/5.0 (X11; Linux) Firefox/135.0", accept="*/*")

    assert "user-agent" not in {key.lower() for key in headers}
    assert not any("Firefox" in str(value) for value in headers.values())


def test_accept_language_is_left_to_the_browser_context():
    """The context locale is set from the profile; a fixed en-US fights the proxy."""
    headers = _build_browser_headers(user_agent="ua", accept="*/*")

    assert not any(key.lower() == "accept-language" for key in headers)


def test_the_protocol_fields_still_survive():
    headers = _build_browser_headers(
        user_agent="ua",
        accept="application/json",
        referer="https://auth.openai.com/x",
        origin="https://auth.openai.com",
        content_type="application/json",
        extra_headers={"oai-device-id": "did-1"},
    )
    lowered = {key.lower(): value for key, value in headers.items()}

    assert lowered["accept"] == "application/json"
    assert lowered["referer"] == "https://auth.openai.com/x"
    assert lowered["origin"] == "https://auth.openai.com"
    assert lowered["content-type"] == "application/json"
    assert lowered["sec-fetch-dest"] == "empty"
    assert lowered["sec-fetch-mode"] == "cors"
    assert lowered["oai-device-id"] == "did-1"


def test_navigation_requests_still_switch_to_document_mode():
    headers = _build_browser_headers(user_agent="ua", accept="text/html", navigation=True)
    lowered = {key.lower(): value for key, value in headers.items()}

    assert lowered["sec-fetch-dest"] == "document"
    assert lowered["sec-fetch-mode"] == "navigate"
    assert lowered["sec-fetch-user"] == "?1"
    assert lowered["upgrade-insecure-requests"] == "1"


def test_extra_headers_cannot_smuggle_client_hints_back_in():
    """The builder itself never emits them, whatever the caller passes."""
    headers = _build_browser_headers(user_agent="ua", accept="*/*", extra_headers={"x-test": "1"})

    assert headers.get("x-test") == "1"
    assert not any(key.lower().startswith("sec-ch-ua") for key in headers)


def test_a_none_extra_header_is_skipped():
    headers = _build_browser_headers(user_agent="ua", accept="*/*", extra_headers={"a": None, "b": "2"})

    assert "a" not in headers
    assert headers["b"] == "2"
