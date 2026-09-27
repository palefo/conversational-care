"""Per-run credentials for remote agents that call back.

A remote agent runs on another server (a LangGraph deployment) and cannot reach
the database, so reporting a summary or hiding a conversation means calling the
REST API. It used to be handed a *personal* token to do it: the conversation
owner's, the admin's own when an admin was testing the agent, or an
admin-privileged service account's. Each of those is a whole person's access —
every endpoint, every client that person can see, no expiry — shipped to a
server that stores its run config.

A run token is the opposite of each of those:

* **One conversation.** It names the conversation it was issued for, and the
  ``/api/v1/run/`` endpoints act on that conversation and no other. They take no
  conversation id at all, so there is nothing for the agent — or a model inside
  it — to forge or get wrong.
* **Only what the tools need.** ``summary`` and ``visibility``. It is accepted
  by the ``/run/`` endpoints and rejected by every other endpoint, because no
  other endpoint lists its authentication class.
* **Short-lived.** It expires with the conversation's idle window (two hours),
  so one found in a log or a stored run is worth little.
* **Only to agents that asked.** Issued when a remote Agent has
  ``allow_callbacks`` on; an agent that never calls back never holds one.

Signed with Django's ``signing`` under its own salt, so it cannot be minted
without the secret key and cannot be confused with any other signed value the
platform issues. Nothing is stored: verifying one is a signature check and a
clock comparison.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from django.core import signing

__all__ = ["SCOPES", "SUMMARY", "VISIBILITY", "TTL_SECONDS", "RunClaims",
           "mint", "verify", "RunTokenAuthentication"]

SALT = "conversational-care.run-token.v1"

SUMMARY = "summary"
VISIBILITY = "visibility"
SCOPES = (SUMMARY, VISIBILITY)

# The conversation's own idle window (STALE_THREAD_HOURS): after this the next
# message starts a new conversation anyway, and a token for the old one has
# nothing left to do.
TTL_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class RunClaims:
    conversation_id: str
    agent_id: int | None
    patient_id: int | None
    scopes: tuple
    expires_at: int

    def allows(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def expires(self) -> datetime:
        return datetime.fromtimestamp(self.expires_at, tz=dt_timezone.utc)


def mint(conversation_id, *, agent_id=None, patient_id=None, scopes=SCOPES,
         ttl: int = TTL_SECONDS, now: float | None = None) -> str:
    """A signed token for one run of one agent in one conversation."""
    issued = int(now if now is not None else time.time())
    payload = {
        "c": str(conversation_id),
        "a": agent_id,
        "p": patient_id,
        "s": [s for s in scopes if s in SCOPES],
        "e": issued + int(ttl),
    }
    return signing.dumps(payload, salt=SALT, compress=True)


def verify(token: str, *, now: float | None = None) -> RunClaims | None:
    """The claims in ``token``, or ``None`` if it is forged, altered or expired."""
    try:
        payload = signing.loads(token or "", salt=SALT)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    try:
        claims = RunClaims(
            conversation_id=str(payload["c"]),
            agent_id=payload.get("a"),
            patient_id=payload.get("p"),
            scopes=tuple(payload.get("s") or ()),
            expires_at=int(payload["e"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if claims.expires_at <= int(now if now is not None else time.time()):
        return None
    return claims


# ── DRF authentication ────────────────────────────────────────────────────

class _RunPrincipal:
    """Stands in for ``request.user`` on a run-token request.

    Not a user, and deliberately unable to pass for one: no pk, no groups, no
    permissions. ``is_admin`` and every per-user check elsewhere answer no, so
    even an endpoint that listed this authentication class by mistake would
    treat the caller as nobody in particular.
    """

    is_authenticated = True
    is_active = True
    is_anonymous = False
    is_staff = False
    is_superuser = False
    pk = id = None

    def __init__(self, claims: RunClaims):
        self.claims = claims

    def get_username(self) -> str:
        return f"run:agent-{self.claims.agent_id}"

    def has_perm(self, *args, **kwargs) -> bool:
        return False

    def __str__(self):
        return self.get_username()


def _drf():
    from rest_framework.authentication import BaseAuthentication, get_authorization_header
    from rest_framework.exceptions import AuthenticationFailed
    return BaseAuthentication, get_authorization_header, AuthenticationFailed


_Base, _get_header, _Failed = _drf()


class RunTokenAuthentication(_Base):
    """``Authorization: RunToken <token>``.

    Its own keyword rather than ``Bearer`` or ``Token``, so a run token handed to
    any other endpoint is not even attempted — that endpoint's own
    authentication class does not recognise the keyword and the request is
    refused as unauthenticated.
    """

    keyword = "RunToken"

    def authenticate(self, request):
        parts = _get_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            return None
        if len(parts) != 2:
            raise _Failed("Malformed run token header.")
        claims = verify(parts[1].decode("utf-8", "ignore"))
        if claims is None:
            raise _Failed("Invalid or expired run token.")
        return _RunPrincipal(claims), claims

    def authenticate_header(self, request):
        return self.keyword
