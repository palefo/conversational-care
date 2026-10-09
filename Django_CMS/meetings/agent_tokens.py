"""Credentials for agent workers calling back into the platform.

Modelled on ``ConvAI.run_tokens``: a stateless, signed token naming exactly one
agent run (and through it one session and one meeting) with the scopes that
role needs, and a short life. It travels in the LiveKit *dispatch* metadata,
which only the job sees — never in room or participant metadata, which every
participant can read.

Two more layers, because a signed token alone would stay valid after the run
it names was stopped:

* every internal call also checks, in the database, that the run is still live;
* the whole internal API additionally requires ``X-CC-Service-Key``, a secret
  only the agent containers hold, so a token copied out of a log is useless
  from anywhere else.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from django.core import signing

SALT = "conversational-care.meetings.agent-run.v1"

PROTOCOL = "protocol"     # read questions, save answers, finish the interview
RECORDING = "recording"   # register recorded segments
SEARCH = "search"         # search the assistant agent's documents
STATUS = "status"         # report the run's own state

SCOPES_BY_ROLE = {
    "scribe": (RECORDING, STATUS),
    "interviewer": (PROTOCOL, STATUS),
    "assistant": (SEARCH, STATUS),
}

# A meeting is capped at max_minutes (default 90); a run outliving that has
# nothing left to do. Generous, because the database check is the real gate.
TTL_SECONDS = 4 * 60 * 60


@dataclass(frozen=True)
class AgentClaims:
    run_id: int
    session_uuid: str
    role: str
    scopes: tuple
    expires_at: int

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


def mint(run, *, ttl: int = TTL_SECONDS, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    return signing.dumps({
        "r": run.pk,
        "s": str(run.session.uuid),
        "o": run.role,
        "c": list(SCOPES_BY_ROLE.get(run.role, ())),
        "e": issued + int(ttl),
    }, salt=SALT, compress=True)


def verify(token: str, *, now: float | None = None) -> AgentClaims | None:
    try:
        payload = signing.loads(token or "", salt=SALT)
        claims = AgentClaims(
            run_id=int(payload["r"]),
            session_uuid=str(payload["s"]),
            role=str(payload["o"]),
            scopes=tuple(payload.get("c") or ()),
            expires_at=int(payload["e"]),
        )
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None
    if claims.expires_at <= int(now if now is not None else time.time()):
        return None
    return claims
