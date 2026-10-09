"""Who may act on a conversation through the per-conversation endpoints.

``/api/v1/conversations/<id>/visibility/`` and ``…/summary/`` are for *people*
holding their own API token — an SDK caller acting on their own conversation, a
tester standing in for a client, an admin. Remote agents do not use them: they
call ``/api/v1/run/…`` with a per-run token scoped to one conversation (see
ConvAI.run_tokens). That split is deliberate. An earlier version handed agents
personal tokens and an admin-privileged service account so they could use these
endpoints, which put a whole person's access — every endpoint, every client, no
expiry — into a remote server's stored run config.

    admin  OR  conv.user  OR  the patient's tester account

A **navigator is deliberately not on the list.** For visibility that is
load-bearing: the switch is the client's own answer about their own privacy, and
a link worker setting it on their behalf would make it worth nothing. For the
summary it is consistency — a navigator has their own summary block in the
panel, which is a better place for their words than the agent's field.
"""
from __future__ import annotations

__all__ = ["may_act_on"]


def may_act_on(conversation, user) -> bool:
    """Whether ``user`` may read and change this conversation's own settings."""
    from .roles import is_admin

    if conversation is None or not user or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    if is_admin(user):
        return True
    if conversation.user_id and conversation.user_id == getattr(user, "pk", None):
        return True
    tester_id = getattr(getattr(conversation.patient, "tester_account", None), "id", None)
    return bool(tester_id and tester_id == getattr(user, "pk", None))
