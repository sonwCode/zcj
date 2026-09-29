"""Disabled placeholder for the private HAR capture workflow.

HAR files can contain credentials, cookies, one-time codes and security
challenge material. The public build does not open a capture browser or write
network traces to disk.
"""
from __future__ import annotations


def default_capture_path(name: str = "") -> str:
    raise RuntimeError("HAR capture is intentionally unavailable in the public build")


def open_capture_browser(*_args, **_kwargs):
    raise RuntimeError("HAR capture is intentionally unavailable in the public build")
