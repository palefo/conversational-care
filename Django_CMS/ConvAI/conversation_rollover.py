"""When an idle conversation rolls over to a new one.

A client's conversation is a thread (``Patient.current_thread_id``). Their next
message after a quiet spell starts a new thread — a new ``Conversation`` — so
yesterday's chat is not dragged into today's, and the agent starts fresh. How
long that quiet spell is used to be three hours, hard-coded.

It is now a platform setting with a per-agent override:

* **Settings → Agents → Conversations** (``SiteConfiguration.
  conversation_idle_hours``, or ``CONVERSATION_IDLE_HOURS`` in ``.env``): the
  default for every agent. Blank keeps three hours.
* **The agent's own setting** (``Agent.conversation_idle_hours``): blank follows
  the platform; a number replaces it for that agent's clients.

``0`` means **never**: the conversation only ends when the client types /quit
or the agent itself ends it (``POST /api/v1/run/end/``). That is what RECO v2
wants, because it closes its own conversations after the rating link and a
rollover in the middle of its closure would cut the young person off.

The agent that decides is the client's agent at the time of their message.
"""
from datetime import timedelta

from django.utils import timezone

DEFAULT_IDLE_HOURS = 3
ENV_KEY = "CONVERSATION_IDLE_HOURS"


def _hours(value):
    """A stored or configured value as whole hours >= 0, or None if unset."""
    if value is None or value == "":
        return None
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return None
    return hours if hours >= 0 else None


def platform_idle_hours() -> int:
    """The platform default: Settings, then ``.env``, then three hours."""
    from .site_config import _env, _override

    for value in (_override("conversation_idle_hours"), _env(ENV_KEY)):
        hours = _hours(value)
        if hours is not None:
            return hours
    return DEFAULT_IDLE_HOURS


def idle_hours(agent) -> int:
    """How many idle hours end a conversation with ``agent``; 0 is never."""
    own = _hours(getattr(agent, "conversation_idle_hours", None)) if agent else None
    return platform_idle_hours() if own is None else own


def overrides_platform(agent) -> bool:
    """Whether ``agent`` has its own setting, and it differs from the platform's."""
    own = _hours(getattr(agent, "conversation_idle_hours", None)) if agent else None
    return own is not None and own != platform_idle_hours()


def is_stale(last_activity, agent, *, now=None) -> bool:
    """Whether a conversation last active at ``last_activity`` has rolled over."""
    hours = idle_hours(agent)
    if not hours or not last_activity:
        return False
    return ((now or timezone.now()) - last_activity) >= timedelta(hours=hours)


def describe(hours: int) -> str:
    """``hours`` as the Agents page says it."""
    from django.utils.translation import gettext as _, ngettext

    if not hours:
        return _("Never")
    return ngettext("After %(n)d hour idle", "After %(n)d hours idle", hours) % {"n": hours}
