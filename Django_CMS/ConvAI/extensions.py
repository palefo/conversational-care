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

See online_meetings.md. Other optional apps use the add-on registry further
down (``register_addon``), which can hold several.
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
    tabs = list(_call("settings_tabs", default=[]) or [])
    for addon in _addons:
        tabs.extend(_addon_call(addon, "settings_tabs", default=[]) or [])
    return tabs


# ── Add-ons ─────────────────────────────────────────────────────────────────
#
# A second, more general seam, for optional apps that are not online meetings
# (the Field App integration is the first, see field_app/). Any number can be
# registered, from the add-on's ``AppConfig.ready``. Each is any object with
# some of these methods; a missing one, or one that raises, contributes nothing
# and never takes a core page or an agent turn down::
#
#     settings_tabs() -> list[dict]                       # same shape as above
#     client_cards(request, patient) -> list[dict]        # {"template", "ctx"} on the client page
#     profile_cards(request) -> list[dict]                # {"template", "ctx"} on the Profile page
#     agent_data_sources(agent, data) -> list             # sections for the agent form (prompt agents)
#     agent_context(agent, thread_id) -> str              # extra system-prompt text for one run
#
# An agent-form section is an object with ``template`` and ``ctx`` (to draw
# it), ``is_valid()`` and ``save(agent)``; ``data`` is request.POST or None.

_addons: list = []


def register_addon(addon) -> None:
    """Called once per add-on, from its ``AppConfig.ready``."""
    if addon not in _addons:
        _addons.append(addon)


def _addon_call(addon, name, *args, default=None):
    fn = getattr(addon, name, None)
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:  # an add-on is never allowed to take a core page down
        logger.exception("Add-on %s failed in %s", type(addon).__name__, name)
        return default


def _collect(name, *args) -> list:
    out = []
    for addon in _addons:
        out.extend(_addon_call(addon, name, *args, default=[]) or [])
    return out


def client_cards(request, patient) -> list:
    return _collect("client_cards", request, patient)


def profile_cards(request) -> list:
    return _collect("profile_cards", request)


def agent_data_sources(agent, data=None) -> list:
    return _collect("agent_data_sources", agent, data)


def agent_context(agent, thread_id) -> str:
    """Extra system-prompt text the add-ons supply for one run of ``agent``.

    Called from the sync side of a prompt agent's run, so add-ons may use the
    ORM and blocking HTTP. Empty when nothing applies.
    """
    parts = [_addon_call(addon, "agent_context", agent, thread_id, default="") or ""
             for addon in _addons]
    return "".join("\n\n" + p.strip() for p in parts if p and p.strip())
