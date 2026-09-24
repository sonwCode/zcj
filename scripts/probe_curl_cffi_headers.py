#!/usr/bin/env python3
"""Verify the browser profile against what curl_cffi actually sends.

The profile in ``core/identity_profile.py`` has to agree with the impersonated build
in several places that are easy to get wrong and impossible to see by reading the code:
the OS (which decides the User-Agent and the ``sec-ch-ua-platform`` hint), the Chrome
"GREASE" brand *and its position* in ``sec-ch-ua`` (Chrome rotates both per build),
and whether a family sends client hints at all. Two public projects disagreed with each
other on the Chrome 142 value, and one paired ``chrome142`` with a Windows UA even
though curl_cffi's chrome142 is macOS - so guessing is not good enough.

This script points the installed curl_cffi at a local echo server and reads back the
headers it really sends, then compares them with the table. No external network, no
Chrome, no credentials: just the transport talking to 127.0.0.1.

    python3 scripts/probe_curl_cffi_headers.py          # report and compare
    python3 scripts/probe_curl_cffi_headers.py --emit   # print a paste-ready table

Exit code is non-zero when the table disagrees, so it can gate a curl_cffi upgrade.
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.identity_profile import _BROWSER_TARGETS, resolve_profile  # noqa: E402


class _Echo(http.server.BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self) -> None:
        _Echo.captured = {k.lower(): v for k, v in self.headers.items()}
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:
        return


def _serve() -> tuple:
    server = socketserver.TCPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def probe(target: str, port: int) -> dict:
    """Return the headers curl_cffi sends for one impersonate target."""
    from curl_cffi import requests as cffi_requests

    _Echo.captured = {}
    session = cffi_requests.Session(impersonate=target)
    session.get("http://127.0.0.1:%d/" % port, timeout=10)
    return dict(_Echo.captured)


def compare(target: str, port: int) -> tuple:
    """Return (problems, expected_ua, expected_hints, expected_platform)."""
    actual = probe(target, port)
    spec = _BROWSER_TARGETS[target]
    profile = resolve_profile("US", seed="probe", impersonate=target)
    emitted = profile.headers()

    expected_ua = actual.get("user-agent", "")
    expected_encoding = actual.get("accept-encoding", "")
    expected_hints = actual.get("sec-ch-ua", "")
    expected_platform = actual.get("sec-ch-ua-platform", "")

    problems = []
    if profile.user_agent != expected_ua:
        problems.append("UA 不一致")
    if spec["user_agent"] != expected_ua:
        problems.append("表里的 UA 与 curl_cffi 不一致")
    if profile.accept_encoding != expected_encoding:
        problems.append("Accept-Encoding 不一致")

    if expected_hints:
        if spec["sec_ch_ua"] != expected_hints:
            problems.append("sec-ch-ua 不一致")
        if emitted.get("sec-ch-ua") != expected_hints:
            problems.append("headers() 的 sec-ch-ua 与 curl_cffi 不一致")
        if emitted.get("sec-ch-ua-platform") != expected_platform:
            problems.append("sec-ch-ua-platform 不一致")
        if profile.platform != expected_platform.strip(chr(34)):
            problems.append("画像 platform 与 client hint 不一致")
    else:
        # Safari / Firefox: curl_cffi sends no client hints, so emitting any is a tell.
        leaked = sorted(k for k in emitted if k.lower().startswith("sec-ch-ua"))
        if leaked:
            problems.append("非 Chromium 却发了 " + ", ".join(leaked))
        if spec["sec_ch_ua"]:
            problems.append("表里给非 Chromium 填了 sec-ch-ua")

    return problems, expected_ua, expected_hints, expected_platform


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit", action="store_true", help="print a paste-ready table")
    args = parser.parse_args()

    try:
        import curl_cffi

        version = getattr(curl_cffi, "__version__", "") or "unknown"
    except Exception as exc:
        print("curl_cffi 不可用，跳过: %s" % exc)
        return 0

    print("curl_cffi 版本: %s" % version)
    print()

    server, port = _serve()
    mismatches = []
    try:
        for target in sorted(_BROWSER_TARGETS):
            try:
                problems, ua, hints, platform = compare(target, port)
            except Exception as exc:
                problems, ua, hints, platform = ["探测失败: %s" % exc], "", "", ""
            family = _BROWSER_TARGETS[target]["family"]
            status = "OK  " if not problems else "FAIL"
            print("%s %-16s %-11s %s" % (status, target, family, ", ".join(problems) or "与 curl_cffi 完全一致"))
            if args.emit:
                print("        \"user_agent\": %r," % ua)
                print("        \"sec_ch_ua\": %r," % hints)
                print("        \"accept_encoding\": %r," % (_BROWSER_TARGETS[target]["accept_encoding"],))
            if problems:
                mismatches.append((target, ua, hints, platform, problems))
    finally:
        server.shutdown()

    if mismatches:
        print()
        print("以下目标与 curl_cffi 实际发送的内容不一致：")
        for target, ua, hints, platform, problems in mismatches:
            print("  %s" % target)
            for item in problems:
                print("    - %s" % item)
            print("    实际 UA          : %s" % ua)
            print("    实际 sec-ch-ua   : %s" % (hints or "(未发送)"))
            print("    实际 platform    : %s" % (platform or "(未发送)"))
        return 1

    print()
    print("全部 %d 个目标与 curl_cffi 实际发送的头一致。" % len(_BROWSER_TARGETS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())