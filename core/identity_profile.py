"""Geo-coherent browser identity profile.

The protocol registration path used to ship a *constant* fingerprint: a fixed
User-Agent, a fixed ``Accept-Language: en-US``, and a Sentinel ``p`` payload that
always claimed ``1920x1080`` / ``GMT+0000 (Coordinated Universal Time)`` / ``en-US``.
That contradicts the proxy exit country (IP says JP, JS says UTC/en-US) and makes
every account share one identity, which is trivially clusterable.

This module resolves one profile per registration session from the proxy exit
region, and every consumer derives from it:

* protocol request headers (``platforms/chatgpt/http_client.py``)
* the Sentinel ``p`` payload (``platforms/chatgpt/register.py``)
* the browser launch (locale / timezone / user agent)

Region-derived fields (timezone, locale, language, UA) stay stable for a region so
they always agree with the exit IP. Hardware-shaped fields (screen, CPU count,
device memory) vary deterministically per session seed so accounts are not
byte-identical.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone

DEFAULT_REGION = "US"
CHROME_VERSION = os.environ.get("ZCJ_CHATGPT_CHROME_VERSION", "142")


@dataclass(frozen=True)
class BrowserProfile:
    """One coherent browser identity for a single registration session."""

    region: str
    user_agent: str
    accept_language: str
    navigator_language: str
    timezone: str
    timezone_label: str
    screen: str
    platform: str
    sec_ch_ua: str
    chrome_version: str
    hardware_concurrency: int
    device_memory: int

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "user_agent": self.user_agent,
            "accept_language": self.accept_language,
            "navigator_language": self.navigator_language,
            "timezone": self.timezone,
            "screen": self.screen,
            "platform": self.platform,
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory": self.device_memory,
        }

    def js_date_string(self) -> str:
        """Render ``Date.toString()`` in the profile timezone.

        JavaScript produces ``Mon Jan 01 2024 12:00:00 GMT+0900 (Japan Standard Time)``.
        The previous implementation emitted ``Mon, 01 Jan 2024 ... GMT+0000`` — both the
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
        """Default request headers derived from this profile."""
        return {
            "User-Agent": self.user_agent,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-platform": '"%s"' % self.platform,
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

_SCREENS = ("1920x1080", "2560x1440", "1536x864", "1440x900", "1680x1050")
_CORES = (4, 8, 12, 16)
_MEMORY = (4, 8, 16)


def normalize_region(region: str | None) -> str:
    value = str(region or "").strip().upper()
    if not value:
        return DEFAULT_REGION
    return value if value in _REGIONS else DEFAULT_REGION


def known_regions() -> list[str]:
    return sorted(_REGIONS)


def _seed_int(seed: str) -> int:
    material = str(seed or "").encode("utf-8")
    return int(hashlib.sha256(material).hexdigest()[:12], 16)


def _build(region: str, seed: str) -> BrowserProfile:
    navigator_language, accept_language, tz, tz_label = _REGIONS[region]
    n = _seed_int(seed)
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36"
    )
    sec_ch_ua = (
        f'"Chromium";v="{CHROME_VERSION}", '
        f'"Google Chrome";v="{CHROME_VERSION}", '
        '"Not_A Brand";v="99"'
    )
    return BrowserProfile(
        region=region,
        user_agent=user_agent,
        accept_language=accept_language,
        navigator_language=navigator_language,
        timezone=tz,
        timezone_label=tz_label,
        screen=_SCREENS[n % len(_SCREENS)],
        platform="Windows",
        sec_ch_ua=sec_ch_ua,
        chrome_version=CHROME_VERSION,
        hardware_concurrency=_CORES[(n >> 8) % len(_CORES)],
        device_memory=_MEMORY[(n >> 16) % len(_MEMORY)],
    )


def resolve_profile(
    region: str | None = "",
    *,
    seed: str | None = "",
    proxy_url: str | None = "",
    allow_network: bool = False,
) -> BrowserProfile:
    """Resolve one coherent profile for a session.

    ``region`` is the proxy exit country (ISO-3166 alpha-2), normally supplied by
    the task layer which already probes the route. When it is missing the default
    region is used — we deliberately do *not* perform a network lookup here, so
    this stays a pure function that the offline preflight can call.
    """
    normalized = normalize_region(region)
    return _build(normalized, str(seed or proxy_url or normalized))


def with_overrides(
    profile: BrowserProfile,
    *,
    screen: str = "",
    navigator_language: str = "",
    timezone: str = "",
    hardware_concurrency: int = 0,
) -> BrowserProfile:
    """Overlay values observed from a live browser onto a resolved profile.

    Reading the values back from the page guarantees the Sentinel payload agrees
    with what the browser actually reports.
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
    return resolve_profile(region, seed=seed)