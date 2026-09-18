"""Bare Server Browser abstraction.

The browser is deliberately a component, not a separate Firefox/Chromium
application. A desktop renderer can implement ``Renderer.load`` later.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class ServerTarget:
    url: str
    container: str | None = None


def validate_server_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Only HTTP(S) server URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in server URLs are not allowed")
    return url


class BareServerBrowser:
    def __init__(self, renderer=None):
        self.renderer = renderer

    @property
    def available(self) -> bool:
        return self.renderer is not None

    def open(self, target: ServerTarget | str):
        url = validate_server_url(target.url if isinstance(target, ServerTarget) else target)
        if self.renderer is None:
            return {"url": url, "mode": "bare", "renderer": None, "status": "renderer_unavailable"}
        return self.renderer.load(url)
