"""Geo-, platform- and browser-coherent identity profile.

The protocol registration path used to ship a *constant* fingerprint: a fixed
User-Agent, a fixed ``Accept-Language: en-US``, and a Sentinel ``p`` payload that
always claimed ``1920x1080`` / ``GMT+0000 (Coordinated Universal Time)`` / ``en-US``.
That contradicts the proxy exit country (the IP says JP while the JS says UTC/en-US)
and makes every account share one identity, which is trivially clusterable.

It also contradicted *itself* twice over:

* **OS** - ``curl_cffi``'s Chrome targets are not all Windows. Every target from
  ``chrome119`` up is macOS, so the old ``Windows NT 10.0`` UA and
  ``sec-ch-ua-platform: "Windows"`` disagreed with the macOS TLS/HTTP2 fingerprint.
* **browser family** - the profile was always Chrome, while the reference
  implementations rotate between Chrome, Safari and Firefox.

Every value in ``_BROWSER_TARGETS`` was **measured**, not looked up: the installed
``curl_cffi`` was pointed at a local echo server and the headers it actually sent were
read back. That caught three things guessing got wrong - the OS, the Chrome "GREASE"
brand *and its position* (Chrome rotates both per build: 136/142 put it third, 146
second, 150 first), and the fact that Safari and Firefox send **no client hints at
all** and Safari additionally omits ``priority``, ``sec-fetch-user`` and
``upgrade-insecure-requests``. Emitting Chrome-shaped headers under a Safari UA would
be a contradiction in the other direction.

Run ``scripts/probe_curl_cffi_headers.py`` after upgrading curl_cffi; it re-measures
every target and exits non-zero on any drift.

The remaining layers stay coherent per session: the region decides timezone, locale and
``Accept-Language`` so they agree with the proxy exit IP, and hardware-shaped fields
vary deterministically per seed so accounts are not byte-identical.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone

DEFAULT_REGION = "US"
DEFAULT_IMPERSONATE = os.environ.get("ZCJ_CHATGPT_IMPERSONATE", "chrome142")
# Empty means "stay on Chrome", which is what the project did before families existed.
# Set ZCJ_CHATGPT_BROWSER_FAMILY=auto to rotate, or to a family name to pin one.
DEFAULT_FAMILY = os.environ.get("ZCJ_CHATGPT_BROWSER_FAMILY", "")

_Q = chr(34)

# From the reference implementation's fingerprint rotation (mac_safari 30, ios_safari
# 15, chrome 35, firefox 20). Only used when the family is explicitly "auto".
_FAMILY_WEIGHTS = (("chrome", 35), ("safari", 30), ("firefox", 20), ("safari_ios", 15))

_NAV_ACCEPT = {
    "chrome": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "firefox": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "safari": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "safari_ios": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Every field below was read back from the installed curl_cffi by pointing it at a
# local echo server. Keep in sync with scripts/probe_curl_cffi_headers.py.
_BROWSER_TARGETS: dict[str, dict[str, str]] = {
    "chrome136": {
        "family": "chrome",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "chrome142": {
        "family": "chrome",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "chrome146": {
        "family": "chrome",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "chrome150": {
        "family": "chrome",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "safari180": {
        "family": "safari",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "sec_ch_ua": "",
        "accept_encoding": "gzip, deflate, br",
    },
    "safari184": {
        "family": "safari",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
        "sec_ch_ua": "",
        "accept_encoding": "gzip, deflate, br",
    },
    "safari2601": {
        "family": "safari",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0.1 Safari/605.1.15",
        "sec_ch_ua": "",
        "accept_encoding": "gzip, deflate, br",
    },
    "safari180_ios": {
        "family": "safari_ios",
        "os_family": "iOS",
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
        "sec_ch_ua": "",
        "accept_encoding": "gzip, deflate, br",
    },
    "safari260_ios": {
        "family": "safari_ios",
        "os_family": "iOS",
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 26_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1",
        "sec_ch_ua": "",
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "firefox144": {
        "family": "firefox",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:144.0) Gecko/20100101 Firefox/144.0",
        "sec_ch_ua": "",
        "accept_encoding": "gzip, deflate, br, zstd",
    },
    "firefox147": {
        "family": "firefox",
        "os_family": "macOS",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) Gecko/20100101 Firefox/147.0",
        "sec_ch_ua": "",
        "accept_encoding": "gzip, deflate, br, zstd",
    },
}

_SCREENS_BY_OS: dict[str, tuple] = {
    "macOS": ("1440x900", "1512x982", "1728x1117", "2560x1600", "1920x1080"),
    "iOS": ("390x844", "393x852", "428x926", "375x812"),
    "Windows": ("1920x1080", "2560x1440", "1536x864", "1680x1050"),
}
_CORES = (4, 8, 12, 16)
_MEMORY = (4, 8, 16)


@dataclass(frozen=True)
class BrowserProfile:
    """One coherent browser identity for a single registration session."""

    region: str
    impersonate: str
    family: str
    os_family: str
    user_agent: str
    accept_language: str
    accept_encoding: str
    navigator_language: str
    timezone: str
    timezone_label: str
    timezone_resolved: bool
    screen: str
    platform: str
    sec_ch_ua: str
    browser_version: str
    hardware_concurrency: int
    device_memory: int

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "impersonate": self.impersonate,
            "family": self.family,
            "os_family": self.os_family,
            "user_agent": self.user_agent,
            "accept_language": self.accept_language,
            "navigator_language": self.navigator_language,
            "timezone": self.timezone,
            "timezone_resolved": self.timezone_resolved,
            "screen": self.screen,
            "platform": self.platform,
            "sec_ch_ua": self.sec_ch_ua,
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory": self.device_memory,
        }

    @property
    def is_chromium(self) -> bool:
        return bool(self.sec_ch_ua)

    def js_date_string(self) -> str:
        """Render ``Date.toString()`` in the profile timezone.

        JavaScript produces ``Mon Jan 01 2024 12:00:00 GMT+0900 (Japan Standard Time)``.
        The previous implementation emitted ``Mon, 01 Jan 2024 ... GMT+0000`` - both the
        field order and the timezone were wrong, so the payload disagreed with the IP.
        """
        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(self.timezone))
        except Exception:
            now = datetime.now(timezone.utc)
        offset = now.strftime("%z") or "+0000"
        label = self.timezone_label or now.tzname() or "Coordinated Universal Time"
        return now.strftime("%a %b %d %Y %H:%M:%S ") + "GMT" + offset + " (" + label + ")"

    def headers(self, *, navigation: bool = False, referer: str = "", origin: str = "") -> dict:
        """Request headers matching what the impersonated build would send.

        The families differ in more than the UA, and every difference below was measured:

        * Chrome sends ``sec-ch-ua``/``-platform``/``-mobile``; Safari and Firefox send
          **none** of them, so emitting them under those UAs would be the tell
        * Safari sends neither ``priority`` nor ``sec-fetch-user`` nor
          ``upgrade-insecure-requests``
        * Firefox sends ``te: trailers``
        * ``accept-encoding`` differs per target (older Safari lacks ``zstd``)

        ``navigation=True`` switches ``Sec-Fetch-*`` to a top-level document navigation;
        XHR requests use empty/cors/same-origin. ``Connection`` is never sent: the
        transport is HTTP/2, where a real browser does not send it.
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Language": self.accept_language,
            "Accept-Encoding": self.accept_encoding,
        }
        if self.is_chromium:
            headers["sec-ch-ua"] = self.sec_ch_ua
            headers["sec-ch-ua-platform"] = _Q + self.platform + _Q
            headers["sec-ch-ua-mobile"] = "?1" if self.os_family == "iOS" else "?0"

        if navigation:
            headers["Accept"] = _NAV_ACCEPT.get(self.family, _NAV_ACCEPT["safari"])
            headers["Sec-Fetch-Dest"] = "document"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-Site"] = "none"
        else:
            headers["Accept"] = "application/json"
            headers["Sec-Fetch-Dest"] = "empty"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Site"] = "same-origin"

        # Safari omits this trio; Chrome and Firefox send it.
        if self.family in ("chrome", "firefox"):
            headers["priority"] = "u=0, i" if navigation else "u=1, i"
            if navigation:
                headers["Sec-Fetch-User"] = "?1"
                headers["Upgrade-Insecure-Requests"] = "1"
        if self.family == "firefox":
            headers["TE"] = "trailers"

        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        return headers


# region -> (navigator_language, accept_language, timezone, timezone_label)
_REGIONS: dict[str, tuple] = {
    "US": ("en-US", "en-US,en;q=0.9", "America/New_York", "Eastern Standard Time"),
    "CA": ("en-CA", "en-CA,en;q=0.9,fr-CA;q=0.8", "America/Toronto", "Eastern Standard Time"),
    "GB": ("en-GB", "en-GB,en;q=0.9", "Europe/London", "Greenwich Mean Time"),
    "IE": ("en-IE", "en-IE,en;q=0.9", "Europe/Dublin", "Greenwich Mean Time"),
    "DE": ("de-DE", "de-DE,de;q=0.9,en;q=0.8", "Europe/Berlin", "Central European Standard Time"),
    "FR": ("fr-FR", "fr-FR,fr;q=0.9,en;q=0.8", "Europe/Paris", "Central European Standard Time"),
    "NL": ("nl-NL", "nl-NL,nl;q=0.9,en;q=0.8", "Europe/Amsterdam", "Central European Standard Time"),
    "ES": ("es-ES", "es-ES,es;q=0.9,en;q=0.8", "Europe/Madrid", "Central European Standard Time"),
    "IT": ("it-IT", "it-IT,it;q=0.9,en;q=0.8", "Europe/Rome", "Central European Standard Time"),
    "PL": ("pl-PL", "pl-PL,pl;q=0.9,en;q=0.8", "Europe/Warsaw", "Central European Standard Time"),
    "SE": ("sv-SE", "sv-SE,sv;q=0.9,en;q=0.8", "Europe/Stockholm", "Central European Standard Time"),
    "JP": ("ja-JP", "ja-JP,ja;q=0.9,en;q=0.8", "Asia/Tokyo", "Japan Standard Time"),
    "KR": ("ko-KR", "ko-KR,ko;q=0.9,en;q=0.8", "Asia/Seoul", "Korea Standard Time"),
    "SG": ("en-SG", "en-SG,en;q=0.9,zh-SG;q=0.8", "Asia/Singapore", "Singapore Standard Time"),
    "HK": ("zh-HK", "zh-HK,zh;q=0.9,en;q=0.8", "Asia/Hong_Kong", "Hong Kong Standard Time"),
    "TW": ("zh-TW", "zh-TW,zh;q=0.9,en;q=0.8", "Asia/Taipei", "Taipei Standard Time"),
    "CN": ("zh-CN", "zh-CN,zh;q=0.9,en;q=0.8", "Asia/Shanghai", "China Standard Time"),
    "IN": ("en-IN", "en-IN,en;q=0.9,hi;q=0.8", "Asia/Kolkata", "India Standard Time"),
    "AU": ("en-AU", "en-AU,en;q=0.9", "Australia/Sydney", "Australian Eastern Standard Time"),
    "NZ": ("en-NZ", "en-NZ,en;q=0.9", "Pacific/Auckland", "New Zealand Standard Time"),
    "BR": ("pt-BR", "pt-BR,pt;q=0.9,en;q=0.8", "America/Sao_Paulo", "Brasilia Standard Time"),
    "MX": ("es-MX", "es-MX,es;q=0.9,en;q=0.8", "America/Mexico_City", "Central Standard Time"),
    "AR": ("es-AR", "es-AR,es;q=0.9,en;q=0.8", "America/Argentina/Buenos_Aires", "Argentina Standard Time"),
    "ZA": ("en-ZA", "en-ZA,en;q=0.9", "Africa/Johannesburg", "South Africa Standard Time"),
    "AE": ("en-AE", "en-AE,en;q=0.9,ar;q=0.8", "Asia/Dubai", "Gulf Standard Time"),
    "TR": ("tr-TR", "tr-TR,tr;q=0.9,en;q=0.8", "Europe/Istanbul", "Turkey Standard Time"),
    "RU": ("ru-RU", "ru-RU,ru;q=0.9,en;q=0.8", "Europe/Moscow", "Moscow Standard Time"),
    "UA": ("uk-UA", "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7", "Europe/Kiev", "Eastern European Standard Time"),
    "VN": ("vi-VN", "vi-VN,vi;q=0.9,en;q=0.8", "Asia/Ho_Chi_Minh", "Indochina Time"),
    "TH": ("th-TH", "th-TH,th;q=0.9,en;q=0.8", "Asia/Bangkok", "Indochina Time"),
    "ID": ("id-ID", "id-ID,id;q=0.9,en;q=0.8", "Asia/Jakarta", "Western Indonesia Time"),
    "PH": ("en-PH", "en-PH,en;q=0.9,fil;q=0.8", "Asia/Manila", "Philippine Standard Time"),
    "MY": ("en-MY", "en-MY,en;q=0.9,ms;q=0.8", "Asia/Kuala_Lumpur", "Malaysia Time"),
}


def normalize_region(region: str | None) -> str:
    value = str(region or "").strip().upper()
    if not value:
        return DEFAULT_REGION
    return value if value in _REGIONS else DEFAULT_REGION


def known_regions() -> list:
    return sorted(_REGIONS)


def known_families() -> list:
    return sorted({spec["family"] for spec in _BROWSER_TARGETS.values()})


def known_impersonate_targets() -> list:
    return sorted(_BROWSER_TARGETS)


def _seed_int(seed: str) -> int:
    material = str(seed or "").encode("utf-8")
    return int(hashlib.sha256(material).hexdigest()[:12], 16)


def timezone_is_available(name: str) -> bool:
    """Whether ``ZoneInfo`` can resolve ``name`` on this host.

    The profile renders its timezone into the Sentinel payload as a JS
    ``Date.toString()``. When the zone cannot be resolved the renderer falls back to
    UTC, and the payload then claims ``GMT+0000`` while the proxy exit is somewhere
    else - the exact contradiction this module exists to remove. It is worth checking
    explicitly because it fails *only* on hosts without tzdata, so it passes in
    development and breaks on a stripped-down server image.
    """
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(str(name))
        return True
    except Exception:
        return False


def _targets_of_family(family: str) -> list:
    return sorted(t for t, spec in _BROWSER_TARGETS.items() if spec["family"] == family)


def resolve_impersonate(target: str | None = "", *, seed: str = "", family: str | None = "") -> str:
    """Pick one target. An explicit target always wins over a family request."""
    value = str(target or "").strip()
    if value in _BROWSER_TARGETS:
        return value

    wanted = str(family if family is not None else DEFAULT_FAMILY).strip().lower()
    if wanted == "auto":
        weighted: list = []
        for name, weight in _FAMILY_WEIGHTS:
            weighted.extend([name] * weight)
        wanted = weighted[_seed_int(str(seed) + ":family") % len(weighted)]
    if wanted:
        candidates = _targets_of_family(wanted)
        if candidates:
            return candidates[_seed_int(str(seed) + ":target") % len(candidates)]

    return DEFAULT_IMPERSONATE if DEFAULT_IMPERSONATE in _BROWSER_TARGETS else "chrome142"


def _build(region: str, seed: str, target: str) -> BrowserProfile:
    navigator_language, accept_language, tz, tz_label = _REGIONS[region]
    spec = _BROWSER_TARGETS[target]
    n = _seed_int(seed)
    os_family = spec["os_family"]
    screens = _SCREENS_BY_OS.get(os_family) or _SCREENS_BY_OS["macOS"]

    version = ""
    for pattern in (r"Chrome/([\d.]+)", r"Version/([\d.]+)", r"Firefox/([\d.]+)"):
        match = re.search(pattern, spec["user_agent"])
        if match:
            version = match.group(1)
            break

    return BrowserProfile(
        region=region,
        impersonate=target,
        family=spec["family"],
        os_family=os_family,
        user_agent=spec["user_agent"],
        accept_language=accept_language,
        accept_encoding=spec["accept_encoding"],
        navigator_language=navigator_language,
        timezone=tz,
        timezone_label=tz_label,
        timezone_resolved=timezone_is_available(tz),
        screen=screens[n % len(screens)],
        platform=os_family,
        sec_ch_ua=spec["sec_ch_ua"],
        browser_version=version,
        hardware_concurrency=_CORES[(n >> 8) % len(_CORES)],
        device_memory=_MEMORY[(n >> 16) % len(_MEMORY)],
    )


def resolve_profile(
    region: str | None = "",
    *,
    seed: str | None = "",
    proxy_url: str | None = "",
    impersonate: str | None = "",
    family: str | None = None,
    allow_network: bool = False,
) -> BrowserProfile:
    """Resolve one coherent profile for a session.

    ``region`` is the proxy exit country (ISO-3166 alpha-2), normally supplied by the
    task layer which already probes the route. When it is missing the default region is
    used - we deliberately do *not* perform a network lookup here, so this stays a pure
    function that the offline preflight can call.

    ``family`` defaults to ``DEFAULT_FAMILY``, which is empty - meaning Chrome, the
    behaviour the project had before families existed. Pass ``"auto"`` to rotate across
    Chrome/Safari/Firefox by the reference weights, or a family name to pin one.
    """
    normalized = normalize_region(region)
    material = str(seed or proxy_url or normalized)
    target = resolve_impersonate(impersonate, seed=material, family=family)
    return _build(normalized, material, target)


def with_overrides(
    profile: BrowserProfile,
    *,
    screen: str = "",
    navigator_language: str = "",
    timezone: str = "",
    hardware_concurrency: int = 0,
) -> BrowserProfile:
    """Overlay values observed from a live browser onto a resolved profile.

    Reading the values back from the page guarantees the Sentinel payload agrees with
    what the browser actually reports.
    """
    changes: dict = {}
    if screen and "x" in screen:
        changes["screen"] = screen
    if navigator_language:
        changes["navigator_language"] = navigator_language
    if timezone:
        changes["timezone"] = timezone
        changes["timezone_label"] = ""
    if hardware_concurrency > 0:
        changes["hardware_concurrency"] = hardware_concurrency
    return replace(profile, **changes) if changes else profile


def profile_for_extra(extra: dict | None, *, seed: str = "", family: str | None = None) -> BrowserProfile:
    """Convenience for callers that only have the task ``extra`` mapping."""
    data = extra or {}
    region = str(data.get("proxy_route_country") or data.get("region") or "")
    target = str(data.get("browser_impersonate") or "")
    chosen = family if family is not None else str(data.get("browser_family") or "")
    return resolve_profile(region, seed=seed, impersonate=target, family=chosen)