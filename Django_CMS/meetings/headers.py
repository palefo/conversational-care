"""Response headers for the meeting pages only.

The platform-wide policy (ConvAI/security_headers.py) blocks the camera and
only lets pages connect back to themselves and Azure. A meeting page needs the
camera, autoplay for remote audio, and a WebSocket to the LiveKit server — so
these pages, and only these, set a wider policy before the middleware's
``setdefault`` would apply the narrow one. Every other page is unchanged.

COEP ``require-corp`` stays on: the LiveKit client is vendored and served from
our own origin, and WebSocket/WebRTC traffic is not governed by COEP.
"""
from __future__ import annotations

from functools import wraps
from urllib.parse import urlparse

from ConvAI.security_headers import CSP

from . import config


def _livekit_origins() -> str:
    url = config.livekit_url()
    if not url:
        return ""
    parts = urlparse(url)
    host = parts.netloc
    if not host:
        return ""
    if parts.scheme in ("wss", "https"):
        return f"wss://{host} https://{host}"
    return f"ws://{host} http://{host}"


def meeting_csp() -> str:
    extra = _livekit_origins()
    if not extra:
        return CSP
    return CSP.replace("connect-src 'self'", f"connect-src 'self' {extra}", 1)


def meeting_page(view):
    """Decorate a view that renders a live room (or its lobby)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        resp = view(request, *args, **kwargs)
        resp["Content-Security-Policy"] = meeting_csp()
        resp["Permissions-Policy"] = ("geolocation=(), microphone=(self), camera=(self), "
                                      "autoplay=(self), display-capture=()")
        return resp
    return wrapper


def private_link(view):
    """Decorate a view reached by a client link: never cached, never leaked.

    ``same-origin``, not ``no-referrer``: both keep the tokened URL from ever
    reaching another site, but ``no-referrer`` also makes browsers send
    ``Origin: null`` on the page's own form POST, which Django's CSRF check
    (rightly) refuses — the link page's Continue button would never work.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        resp = view(request, *args, **kwargs)
        resp["Referrer-Policy"] = "same-origin"
        resp["Cache-Control"] = "no-store, private"
        resp["X-Robots-Tag"] = "noindex, nofollow"
        return resp
    return wrapper
