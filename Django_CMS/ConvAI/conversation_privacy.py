"""Conversations a client asked their link worker not to read.

One rule, in one place, because it is read from four different surfaces and
they must not drift: the detail panel, the client's conversation page, the
conversation download, and the API the agent's tool calls.

What "hidden" means here is narrow on purpose. A navigator still sees that the
conversation happened, when it ran, and how many messages are in it. What is
withheld is everything derived from the *words*: the messages themselves, the
classifier's summary, the topic, and the detector answers — an abstract of a
conversation is the conversation, and withholding the bubbles while printing a
paragraph describing them would be a promise kept in form only.

Three things it is deliberately not:

* **Not a secret from admins.** An admin holds ``access_configuration`` and is
  answerable for the service; they read everything, here as everywhere else.
  The promise made to the client is about their link worker.
* **Not stronger than the safety floor.** A conversation that tripped the
  self-harm detector is readable by the navigator whatever the client asked,
  because the whole point of that detector is that nothing configurable stands
  between a disclosure and somebody seeing it (see SAFETY_DETECTOR in
  utils_conversation_classification). An agent offering the choice should say
  so; this module only enforces it.
* **Not switched off by the switch.** ``enabled()`` gates whether a client can
  *make* the request — the API and the agent tool. It is not consulted when
  deciding whether an existing ``hidden`` conversation is withheld, so an
  admin turning the feature off cannot retroactively open exchanges somebody
  was promised were closed.
"""
from __future__ import annotations

from django.utils import timezone

from .roles import is_admin
from .site_config import get_bool
from .utils_conversation_classification import SAFETY_LABEL

__all__ = [
    "enabled", "safety_override", "is_withheld",
    "withheld_ids", "set_hidden",
]


def enabled() -> bool:
    """Whether this installation lets clients hide a conversation. Off by default."""
    return get_bool("CONVERSATION_PRIVACY_ENABLED", False)


def safety_override(conversation) -> bool:
    """Whether the self-harm floor fired on this conversation.

    Read from the flags the classifier wrote rather than from the Alert table:
    the flag is set on the conversation by the same pass that raises the alert,
    and a navigator who resolved the alert has not made the disclosure
    un-happen. A human answer on the label wins over the automatic one, in
    either direction, exactly as it does everywhere else the flags are read.
    """
    if conversation is None:
        return False
    human = getattr(conversation, "human_flags", None) or {}
    auto = getattr(conversation, "auto_flags", None) or {}
    if SAFETY_LABEL in human:
        return bool(human[SAFETY_LABEL])
    return bool(auto.get(SAFETY_LABEL))


def is_withheld(conversation, user) -> bool:
    """Whether ``user`` must not be shown this conversation's content."""
    if conversation is None or not getattr(conversation, "hidden", False):
        return False
    if is_admin(user):
        return False
    return not safety_override(conversation)


def withheld_ids(conversation_ids, user):
    """The subset of ``conversation_ids`` whose content ``user`` may not read.

    Ids are returned as the strings they were passed in as — Message rows key
    conversations by a text column, and callers match on that spelling.
    Anything that is not a conversation key at all (a reminder or care-plan
    row, an unparseable id) is simply absent from the result, which is correct:
    there is no conversation there to have been hidden.
    """
    from .models import Conversation

    wanted = {str(cid) for cid in conversation_ids if cid}
    if not wanted or is_admin(user):
        return set()

    # One query, and only over the ids actually on screen. Values rather than
    # objects because safety_override reads two JSON columns and nothing else.
    rows = (Conversation.objects
            .filter(id__in=[c for c in wanted if _is_uuid(c)], hidden=True)
            .values("id", "auto_flags", "human_flags"))

    out = set()
    for row in rows:
        if safety_override(_Flags(row)):
            continue
        # Back to the caller's spelling: a message may store the id in a
        # different case or without dashes than the UUID column renders it.
        canonical = str(row["id"])
        out.update(c for c in wanted if _same_uuid(c, canonical))
    return out


def set_hidden(conversation, hidden: bool):
    """Record the client's answer. Returns the saved conversation.

    ``hidden_at`` is stamped on the way in and left alone on the way out: what
    it answers is "when did they ask for this", and a conversation that was
    hidden and then opened again is more usefully described by the fact that it
    was once hidden than by having that erased.
    """
    conversation.hidden = bool(hidden)
    if hidden:
        conversation.hidden_at = timezone.now()
    conversation.save(update_fields=["hidden", "hidden_at"])
    return conversation


# --- internals -------------------------------------------------------------

class _Flags:
    """A values() row wearing just enough of a Conversation for safety_override."""

    def __init__(self, row):
        self.auto_flags = row.get("auto_flags") or {}
        self.human_flags = row.get("human_flags") or {}


def _is_uuid(value) -> bool:
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _same_uuid(a, b) -> bool:
    import uuid
    try:
        return uuid.UUID(str(a)) == uuid.UUID(str(b))
    except (TypeError, ValueError):
        return False
