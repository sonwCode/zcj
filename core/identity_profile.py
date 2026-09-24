"""Geo- and platform-coherent browser identity profile.

The protocol registration path used to ship a *constant* fingerprint: a fixed
User-Agent, a fixed ``Accept-Language: en-US``, and a Sentinel ``p`` payload that
always claimed ``1920x1080`` / ``GMT+0000 (Coordinated Universal Time)`` / ``en-US``.
That contradicts the proxy exit country (the IP says JP while the JS says UTC/en-US)
and makes every account share one identity, which is trivially clusterable.

It also contradicted *itself*. ``curl_cffi`` impersonates a specific browser build,
and its Chrome targets are not all Windows: every target from ``chrome119`` up is
macOS (``chrome142`` is macOS Tahoe). The old code impersonated ``chrome142`` while
sending ``Windows NT 10.0; Win64; x64`` and ``sec-ch-ua-platform: "Windows"``, so the
TLS/HTTP2 fingerprint said macOS and the headers said Windows.

This module resolves one profile per session and keeps every layer consistent:

* the ``impersonate`` target decides the OS, and the OS decides the UA and the
  ``sec-ch-ua-platform`` hint
* the region decides timezone, locale and ``Accept-Language`` so they agree with the
  proxy exit IP
* the Chrome "GREASE" brand is pinned per major version, because Chrome randomises
  that token per build and a stale value for a claimed version is a contradiction
* the full client-hint set is emitted, since a real Chrome sends more than
  ``sec-ch-ua``/``platform``/``mobile``
* hardware-shaped fields vary deterministically per session seed so accounts are not
  byte-identical
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone

DEFAULT_REGION = "US"
DEFAULT_IMPERSONATE = os.environ.get("ZCJ_CHATGPT_IMPERSONATE", "chrome142")

_Q = chr(34)


def _ch(brand: str, version: str) -> str:
    """Render one ``sec-ch-ua`` brand token: ``"Brand";v="1"``."""
    return _Q + brand + _Q + ";v=" + _Q + version + _Q


# impersonate -> (major, os_family, ua_platform_token, platform_version)
#
# The OS for each target comes from the installed curl_cffi fingerprint table, not
# from guesswork: chrome119 and up are all macOS there, so a Windows UA is incoherent
# with them. Only targets with a sourced GREASE brand are listed.
_CHROME_TARGETS: dict[str, tuple[str, str, str, str]] = {
    "chrome136": ("136", "macOS", "Macintosh; Intel Mac OS X 10_15_7", "15"),
    "chrome142": ("142", "macOS", "Macintosh; Intel Mac OS X 10_15_7", "26"),
    "chrome146": ("146", "macOS", "Macintosh; Intel Mac OS X 10_15_7", "26"),
    "chrome150": ("150", "macOS", "Macintosh; Intel Mac OS X 10_15_7", "26"),
}

# Chrome randomises the "GREASE" brand token per build. Claiming one version while
# sending another version's token is a self-contradiction, so the token is pinned per
# major version instead of hardcoded once.
_GREASE_BRANDS: dict[str, tuple[str, str]] = {
    "136": ("Not.A/Brand", "99"),
    "142": ("Not/A)Brand", "8"),
    "146": ("Not-A.Brand", "24"),
    "150": ("Not;A=Brand", "8"),
}

_SCREENS_BY_OS: dict[str, tuple[str, ...]] = {
    "macOS": ("1440x900", "1512x982", "1728x1117", "2560x1600", "1920x1080"),
    "Windows": ("1920x1080", "2560x1440", "1536x864", "1680x1050"),
}
_CORES = (4, 8, 12, 16)
_MEMORY = (4, 8, 16)


@dataclass(frozen=True)
class BrowserProfile:
    """One coherent browser identity for a single registration session."""

    region: str
    impersonate: str
    os_family: str
    user_agent: str
    accept_language: str
    navigator_language: str
    timezone: str
    timezone_label: str
    screen: str
    platform: str
    sec_ch_ua: str
    sec_ch_ua_full_version_list: str
    sec_ch_ua_platform_version: str
    sec_ch_ua_arch: str
    chrome_version: str
    hardware_concurrency: int
    device_memory: int

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "impersonate": self.impersonate,
            "os_family": self.os_family,
            "user_agent": self.user_agent,
            "accept_language": self.accept_language,
            "navigator_language": self.navigator_language,
            "timezone": self.timezone,
            "screen": self.screen,
            "platform": self.platform,
            "sec_ch_ua": self.sec_ch_ua,
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory": self.device_memory,
        }

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

    def headers(self) -> dict:
        """Default request headers, including the full client-hint set.

        A real Chrome sends the whole ``sec-ch-ua*`` family; sending only a subset is
        itself a distinguishing signal, so every hint the browser would emit is present.
        """
        return {
            "User-Agent": self.user_agent,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-full-version-list": self.sec_ch_ua_full_version_list,
            "sec-ch-ua-platform": _Q + self.platform + _Q,
            "sec-ch-ua-platform-version": _Q + self.sec_ch_ua_platform_version + _Q,
            "sec-ch-ua-arch": _Q + self.sec_ch_ua_arch + _Q,
            "sec-ch-ua-bitness": _Q + "64" + _Q,
            "sec-ch-ua-model": _Q + _Q,
            "sec-ch-ua-mobile": "?0",
            "Accept": "application/json",
            "Accept-Language": self.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }


# region -> (navigator_language, accept_language, timezone, timezone_label)
_REGIONS: dict[str, tuple[str, str, str, str]] = {
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


def known_impersonate_targets() -> list:
    return sorted(_CHROME_TARGETS)


def resolve_impersonate(target: str | None = "") -> str:
    value = str(target or "").strip()
    if value in _CHROME_TARGETS:
        return value
    return DEFAULT_IMPERSONATE if DEFAULT_IMPERSONATE in _CHROME_TARGETS else "chrome142"


def _seed_int(seed: str) -> int:
    material = str(seed or "").encode("utf-8")
    return int(hashlib.sha256(material).hexdigest()[:12], 16)


def _build(region: str, seed: str, target: str) -> BrowserProfile:
    navigator_language, accept_language, tz, tz_label = _REGIONS[region]
    version, os_family, ua_token, platform_version = _CHROME_TARGETS[target]
    grease_name, grease_ver = _GREASE_BRANDS[version]
    n = _seed_int(seed)

    user_agent = (
        "Mozilla/5.0 (" + ua_token + ") "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/" + version + ".0.0.0 Safari/537.36"
    )
    sec_ch_ua = ", ".join((
        _ch("Chromium", version),
        _ch(grease_name, grease_ver),
        _ch("Google Chrome", version),
    ))
    sec_ch_ua_full = ", ".join((
        _ch("Chromium", version + ".0.0.0"),
        _ch(grease_name, grease_ver + ".0.0.0"),
        _ch("Google Chrome", version + ".0.0.0"),
    ))
    screens = _SCREENS_BY_OS.get(os_family) or _SCREENS_BY_OS["macOS"]

    return BrowserProfile(
        region=region,
        impersonate=target,
        os_family=os_family,
        user_agent=user_agent,
        accept_language=accept_language,
        navigator_language=navigator_language,
        timezone=tz,
        timezone_label=tz_label,
        screen=screens[n % len(screens)],
        platform=os_family,
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_full_version_list=sec_ch_ua_full,
        sec_ch_ua_platform_version=platform_version,
        sec_ch_ua_arch="arm" if (n >> 4) % 2 else "x86",
        chrome_version=version,
        hardware_concurrency=_CORES[(n >> 8) % len(_CORES)],
        device_memory=_MEMORY[(n >> 16) % len(_MEMORY)],
    )


def resolve_profile(
    region: str | None = "",
    *,
    seed: str | None = "",
    proxy_url: str | None = "",
    impersonate: str | None = "",
    allow_network: bool = False,
) -> BrowserProfile:
    """Resolve one coherent profile for a session.

    ``region`` is the proxy exit country (ISO-3166 alpha-2), normally supplied by the
    task layer which already probes the route. When it is missing the default region is
    used - we deliberately do *not* perform a network lookup here, so this stays a pure
    function that the offline preflight can call.
    """
    normalized = normalize_region(region)
    target = resolve_impersonate(impersonate)
    return _build(normalized, str(seed or proxy_url or normalized), target)


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


def profile_for_extra(extra: dict | None, *, seed: str = "") -> BrowserProfile:
    """Convenience for callers that only have the task ``extra`` mapping."""
    data = extra or {}
    region = str(data.get("proxy_route_country") or data.get("region") or "")
    target = str(data.get("browser_impersonate") or "")
    return resolve_profile(region, seed=seed, impersonate=target)