"""Seams for optional apps, so the core never imports them.

Online meetings live in their own app (``meetings``), which can be switched off
in Settings, left out of the deployment entirely (``MEETINGS_APP=0``), or
deleted. The core only ever talks to it through the provider registered here
and the signals below. With no provider registered every question has a safe
answer — "not available", "nothing to add" — so a core page renders exactly as
it would if the app had never existed.

A provider is any object with these methods (all optional; missing ones answer
with the defaults in ``_NullProvider``)::

    available() -> bool                    # the feature is switched on
    panel_extras(request, meeting) -> dict # extra context for an ONLINE meeting's panel
    reminder_extras(meeting) -> dict       # e.g. {"join_url": ...} for reminders
    live_state(meeting) -> dict | None     # is a room open for this meeting right now?
    settings_tabs() -> list[dict]          # tabs to add to Settings

See online_meetings.md.
"""
from __future__ import annotations

import logging

from django.dispatch import Signal

logger = logging.getLogger(__name__)

# Sent by the core whenever a meeting changes in a way an add-on may care about.
#   meeting_changed(meeting, changed: set[str], by)  — after edit_meeting saves
#   meeting_cancelled(meeting, by)                   — cancelled (not deleted)
#   meeting_reinstated(meeting, by)
#   meeting_completed(meeting, status, by)           — an outcome was recorded
meeting_changed = Signal()
meeting_cancelled = Signal()
meeting_reinstated = Signal()
meeting_completed = Signal()


class _NullProvider:
    def available(self):
        return False

    def panel_extras(self, request, meeting):
        return {}

    def reminder_extras(self, meeting):
        return {}

    def live_state(self, meeting):
        return None

    def settings_tabs(self):
        return []


_NULL = _NullProvider()
_provider = None


def register_online_provider(provider) -> None:
    """Called once, from the meetings app's ``AppConfig.ready``."""
    global _provider
    _provider = provider


def _call(name, *args, default=None):
    target = _provider or _NULL
    fn = getattr(target, name, None) or getattr(_NULL, name)
    try:
        return fn(*args)
    except Exception:  # an add-on is never allowed to take a core page down
        logger.exception("Online meetings provider failed in %s", name)
        return default


def online_installed() -> bool:
    """The meetings app is part of this deployment (it may still be switched off)."""
    return _provider is not None


def online_available() -> bool:
    """Online meetings can be scheduled and joined right now."""
    return bool(_call("available", default=False))


def panel_extras(request, meeting) -> dict:
    return _call("panel_extras", request, meeting, default={}) or {}


def reminder_extras(meeting) -> dict:
    return _call("reminder_extras", meeting, default={}) or {}


def live_state(meeting):
    return _call("live_state", meeting, default=None)


def settings_tabs() -> list:
    return _call("settings_tabs", default=[]) or []
