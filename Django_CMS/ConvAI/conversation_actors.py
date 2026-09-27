"""Who may act on a conversation through the API, and what the agent acts as.

Two endpoints let an agent write to a conversation it is holding — its
visibility (``conversation_privacy``) and its summary
(``conversation_summary``). Both need the same answer to the same question, so
the question is asked in one place.

The rule is bound to the **patient**, not to a user account, because on the
channel most clients actually use there is no user account to bind to.
``save_message`` is the single path every WhatsApp, SMS, external-chat and
tester turn is persisted through, and it has never set ``Conversation.user`` —
only ``/api/v1/messages/`` does, for SDK callers. So a check that could only
match ``conv.user`` worked for the SDK and the web tester chat and silently
404'd for everybody on WhatsApp.

    admin  OR  conv.user  OR  the patient's tester account

The agent-service account below is in the first of those. That is the honest
shape of this design and worth stating plainly rather than burying: **a service
token can write to any conversation.** What keeps it to the right one is the
tool, which takes its conversation id from the run config and never from a
model-supplied argument, and not this function. The trade accepted here is that
the tool is in-process code we control, while the alternative — a capability
token minted per run — is a second auth path to get wrong. What this module can
do instead is make the writes traceable: every one is logged with the acting
account's name, and the service account is never a person, so the log does not
read as an admin who was asleep at the time.

A **navigator is deliberately not on the list.** For visibility that is
load-bearing: the switch is the client's own answer about their own privacy, and
a link worker setting it on their behalf would make it worth nothing. For the
summary it is consistency — a navigator has their own summary block in the
panel, which is a better place for their words than the agent's field.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["may_act_on", "service_account", "service_token", "token_for_conversation",
           "SERVICE_USERNAME"]

# Not a person, and named so it reads that way in an audit log, in the Django
# admin user list, and in the `author` of anything it ever writes.
SERVICE_USERNAME = "agent-service"


def may_act_on(conversation, user) -> bool:
    """Whether ``user`` may read and change this conversation's own settings."""
    from .roles import is_admin

    if conversation is None or not user or not getattr(user, "is_authenticated", False):
        return False
    if not user.is_active:
        return False
    if is_admin(user):
        return True
    if conversation.user_id and conversation.user_id == user.id:
        return True
    tester_id = getattr(getattr(conversation.patient, "tester_account", None), "id", None)
    return bool(tester_id and tester_id == user.id)


def service_account():
    """The login-disabled account an agent acts as when the client has none.

    Created on demand rather than seeded in a migration, so an installation that
    never runs a tool-using agent never grows the account — and one that does
    gets it without a deployment step. ``set_unusable_password`` and
    ``is_active=True`` together mean it can authenticate a **token** and cannot
    be signed into: DRF token auth checks ``is_active`` and never the password,
    while every login form checks the password and would reject this one.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission

    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=SERVICE_USERNAME,
        defaults={
            "first_name": "Agent",
            "last_name": "Service",
            "is_active": True,
            "is_staff": False,
        },
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
        logger.info("Created the %s account for agent tool callbacks.", SERVICE_USERNAME)

    # Granted directly rather than through a group: a group an admin could edit
    # is a group an admin could empty, and an agent that quietly stops being
    # able to write summaries is a hard thing to notice.
    perm = Permission.objects.filter(
        codename="access_configuration",
        content_type__app_label="ConvAI",
    ).first()
    if perm and not user.user_permissions.filter(pk=perm.pk).exists():
        user.user_permissions.add(perm)
        # has_perm caches per instance; drop it so a caller checking straight
        # after creation sees the permission it just granted.
        for attr in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            user.__dict__.pop(attr, None)
    return user


def service_token() -> str:
    """The service account's API token key, creating both if needed."""
    from rest_framework.authtoken.models import Token

    token, _created = Token.objects.get_or_create(user=service_account())
    return token.key


def token_for_conversation(conversation, user_or_patient) -> str:
    """The token to hand a remote agent so it can call back about this run.

    Preference order is narrowest first, because a token that can only touch
    one client's conversations is a better thing to put on the wire than one
    that can touch all of them:

    1. the account that holds the conversation (SDK and API callers), or the
       signed-in user driving this run;
    2. the patient's tester account, where the client has one;
    3. the service account, for the account-less channels.

    Failures are swallowed and return ``""``. A remote agent handed no token
    loses the two callback tools; it must not lose the turn.
    """
    from rest_framework.authtoken.models import Token

    try:
        candidates = []
        if conversation is not None and conversation.user_id:
            candidates.append(conversation.user_id)
        # A ConvAIUser driving the run (the web tester chat, the API) — a
        # Patient has no pk in the user table and is skipped by the isinstance
        # check below.
        from .models import Patient
        if user_or_patient is not None and not isinstance(user_or_patient, Patient):
            pk = getattr(user_or_patient, "pk", None)
            if pk:
                candidates.append(pk)

        patient = (user_or_patient if isinstance(user_or_patient, Patient)
                   else getattr(conversation, "patient", None))
        tester_id = getattr(getattr(patient, "tester_account", None), "id", None)
        if tester_id:
            candidates.append(tester_id)

        for pk in candidates:
            token, _created = Token.objects.get_or_create(user_id=pk)
            return token.key
        return service_token()
    except Exception:  # pragma: no cover - a missing token must not cost the turn
        logger.exception("Could not mint a callback token for conversation %s",
                         getattr(conversation, "id", None))
        return ""
