"""Task-scoped dynamic proxy manager for registration tasks.

Each worker draws a fresh proxy from a rotating-residential-proxy API
(``sessType=rotating`` gateway).  Calling ``get_proxy()`` re-fetches the
gateway entry point, so every new connection exits through a different
residential IP.  The same manager is also handed to the platform as the
``proxy_rotate_callback`` so a Cloudflare challenge swaps to a new IP and
rebuilds the session instead of giving up.
"""
from __future__ import annotations

import threading
from typing import Optional

import requests


class DynamicProxyManager:
    """Fetches one rotating-residential proxy URL per call from an extract API."""

    def __init__(self, api_url: str, *, timeout: int = 12):
        self._api_url = str(api_url or "").strip()
        self._timeout = max(int(timeout or 12), 3)
        self._lock = threading.Lock()
        self._current: Optional[str] = None

    @property
    def api_url(self) -> str:
        return self._api_url

    def get_proxy(self) -> Optional[str]:
        """Return a proxy URL by calling the extract API (each call = new IP).

        Returns ``None`` if the API is unreachable/empty so the caller can
        fall back to a static proxy or fail gracefully.
        """
        if not self._api_url:
            return None
        url = self._api_url
        # The gateway split token often arrives with a literal backslash-r
        # because the caller pasted it from a README; normalise it to a real CRLF.
        if "\\r\\n" in url or "\\n" in url:
            url = url.replace("\\r\\n", "\r\n").replace("\\n", "\n")
        try:
            resp = requests.get(url, timeout=self._timeout)
            resp.raise_for_status()
            text = resp.text.strip()
        except Exception:
            return None
        if not text:
            return None
        # The extract API usually returns one IP:PORT per line; a JSON array or
        # ``{"data": [...]}`` is handled the same way.
        candidates = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith(("{", "["))
        ]
        if not candidates:
            return None
        raw = candidates[0]
        if raw.startswith(("http://", "https://", "socks5://", "socks4://")):
            proxy = raw
        else:
            proxy = f"http://{raw}"
        with self._lock:
            self._current = proxy
        return proxy

    def rotate(self) -> Optional[str]:
        """Alias used as the platform ``proxy_rotate_callback``."""
        return self.get_proxy()

    def current(self) -> Optional[str]:
        with self._lock:
            return self._current
