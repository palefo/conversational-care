"""Where online meetings read their configuration from.

Two kinds of setting, kept apart on purpose:

* **Behaviour** (on/off, recording default, admission, limits) — editable in
  Settings → Online meetings, stored on ``MeetingsSettings``. The switch falls
  back to ``ONLINE_MEETINGS_ENABLED`` in ``.env`` while left on "Use the .env
  default", and is off when neither says otherwise.
* **Connection** (LiveKit URLs and key pair, the agents' service key) —
  environment only. The LiveKit server, this web app and the agent workers must
  agree on them, and only the environment is shared by all three.

    LIVEKIT_URL               wss://lk.example.org   what browsers connect to
    LIVEKIT_API_URL           http://livekit:7880    what Django calls (default: LIVEKIT_URL as http)
    LIVEKIT_API_KEY / LIVEKIT_API_SECRET
    MEETINGS_SERVICE_KEY      shared secret the agent workers present to Django
    MEETINGS_PUBLIC_BASE_URL  https://care.example.org — base for links sent to clients
    MEETING_LINK_KEY          signs client links (falls back to a key derived from SECRET_KEY)
"""
from __future__ import annotations

import os

from django.conf import settings

_TRUTHY = ("1", "true", "yes", "on")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or getattr(settings, name, "") or default).strip()


def cfg():
    from .models import MeetingsSettings
    return MeetingsSettings.load()


def enabled() -> bool:
    """Is the feature switched on? Never raises: a broken read means off."""
    try:
        value = cfg().enabled
    except Exception:
        value = ""
    if value in ("0", "1"):
        return value == "1"
    return _env("ONLINE_MEETINGS_ENABLED").lower() in _TRUTHY


def livekit_url() -> str:
    """The WebSocket URL browsers and agents connect to."""
    return _env("LIVEKIT_URL")


def livekit_api_url() -> str:
    """Where this server reaches LiveKit's HTTP API.

    Often different from LIVEKIT_URL: inside compose the web container talks to
    ``http://livekit:7880`` while browsers use the public ``wss://`` address.
    """
    explicit = _env("LIVEKIT_API_URL")
    if explicit:
        return explicit.rstrip("/")
    url = livekit_url()
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):].rstrip("/")
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):].rstrip("/")
    return url.rstrip("/")


def api_key() -> str:
    return _env("LIVEKIT_API_KEY")


def api_secret() -> str:
    return _env("LIVEKIT_API_SECRET")


def service_key() -> str:
    return _env("MEETINGS_SERVICE_KEY")


def public_base_url(request=None) -> str:
    """The base for links sent to clients.

    MEETINGS_PUBLIC_BASE_URL wins; AGENT_CALLBACK_URL is the existing setting
    for "how the outside world reaches us"; the current request is the last
    resort (right for a navigator copying a link in the browser).
    """
    base = _env("MEETINGS_PUBLIC_BASE_URL") or _env("AGENT_CALLBACK_URL")
    if base:
        return base.rstrip("/")
    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    return ""


def livekit_configured() -> bool:
    return bool(livekit_url() and api_key() and api_secret())


def status() -> dict:
    """What Settings shows: can a meeting actually be held right now?"""
    on = enabled()
    missing = []
    if not livekit_url():
        missing.append("LIVEKIT_URL")
    if not api_key():
        missing.append("LIVEKIT_API_KEY")
    if not api_secret():
        missing.append("LIVEKIT_API_SECRET")
    agents_missing = [] if service_key() else ["MEETINGS_SERVICE_KEY"]
    reachable = None
    if on and not missing:
        from . import livekit_api
        reachable = livekit_api.ping()
    return {
        "enabled": on,
        "missing": missing,
        "agents_missing": agents_missing,
        "reachable": reachable,
        "ok": on and not missing and reachable is not False,
        "livekit_url": livekit_url(),
    }
