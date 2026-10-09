"""LiveKit webhooks: who joined and left, and when a room closed.

Verified (a JWT signed with our key pair, carrying the SHA-256 of the body),
idempotent (each event id is handled once), and tolerant of arriving out of
order. Answered fast: rows are written and jobs queued, nothing slow happens
in the request. The periodic reconcile job covers anything a restart dropped.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .. import livekit_api, rooms
from ..models import AgentRun, Attendance, MeetingInvite, MeetingSession, WebhookEvent

logger = logging.getLogger(__name__)


def _get(d: dict, *names, default=None):
    """LiveKit's JSON may come camelCase or snake_case depending on the path."""
    for n in names:
        if isinstance(d, dict) and n in d and d[n] not in (None, ""):
            return d[n]
    return default


def _ts(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return timezone.now()


def _session_for(room: dict):
    name = _get(room, "name", default="")
    if not name.startswith("cc-"):
        return None
    try:
        import uuid
        return MeetingSession.objects.select_related("meeting").filter(
            uuid=uuid.UUID(hex=name[3:])).first()
    except ValueError:
        return None


def _kind_and_role(participant: dict):
    identity = _get(participant, "identity", default="")
    attrs = _get(participant, "attributes", default={}) or {}
    role = attrs.get("cc.role", "")
    kind = _get(participant, "kind", default="")
    if identity.startswith("staff-"):
        return Attendance.Kind.STAFF, role or "navigator"
    if identity.startswith("inv-"):
        return Attendance.Kind.INVITEE, role
    if str(kind).upper() in ("AGENT", "4") or identity.startswith("agent-"):
        return Attendance.Kind.AGENT, role
    return Attendance.Kind.INVITEE, role


@csrf_exempt
@require_POST
def livekit_webhook(request):
    event = livekit_api.verify_webhook(request.body, request.headers.get("Authorization", ""))
    if event is None:
        return HttpResponse(status=401)

    event_id = _get(event, "id", default="")
    name = _get(event, "event", default="")
    if event_id:
        try:
            with transaction.atomic():
                WebhookEvent.objects.create(event_id=event_id[:80], event=name[:40])
        except IntegrityError:
            return HttpResponse(status=200)  # already handled

    room = _get(event, "room", default={}) or {}
    session = _session_for(room)
    if session is None:
        return HttpResponse(status=200)

    try:
        handler = HANDLERS.get(name)
        if handler:
            handler(session, event)
    except Exception:
        logger.exception("Handling LiveKit %s for session %s failed", name, session.pk)
    return HttpResponse(status=200)


def _room_started(session, event):
    room = _get(event, "room", default={})
    sid = _get(room, "sid", default="")
    if sid and not session.room_sid:
        MeetingSession.objects.filter(pk=session.pk).update(room_sid=sid[:64])


def _room_finished(session, event):
    # The room is gone (emptied and timed out, or deleted). If the session
    # still thinks it is live, close it the same way End would.
    if session.is_live:
        rooms.end(session, reason="room_finished")


def _participant_joined(session, event):
    p = _get(event, "participant", default={}) or {}
    sid = _get(p, "sid", default="")
    identity = _get(p, "identity", default="")
    if not sid or not identity:
        return
    kind, role = _kind_and_role(p)
    invite = None
    if identity.startswith("inv-"):
        invite = MeetingInvite.objects.filter(public_id=identity[4:]).first()
    user_id = None
    if identity.startswith("staff-"):
        try:
            user_id = int(identity[6:])
        except ValueError:
            pass
    Attendance.objects.get_or_create(
        participant_sid=sid[:64],
        defaults={"session": session, "identity": identity[:96],
                  "name": (_get(p, "name", default="") or "")[:120],
                  "kind": kind, "role": role[:20], "invite": invite, "user_id": user_id,
                  "joined_at": _ts(_get(p, "joinedAt", "joined_at"))},
    )
    if kind != Attendance.Kind.AGENT:
        MeetingSession.objects.filter(pk=session.pk).update(last_human_at=timezone.now())
    if kind == Attendance.Kind.AGENT and role == "scribe":
        MeetingSession.objects.filter(pk=session.pk).update(recorder_seen_at=timezone.now())


def _participant_left(session, event):
    p = _get(event, "participant", default={}) or {}
    sid = _get(p, "sid", default="")
    identity = _get(p, "identity", default="")
    reason = str(_get(p, "disconnectReason", "disconnect_reason", default="") or "")
    Attendance.objects.filter(participant_sid=sid, left_at__isnull=True).update(
        left_at=timezone.now(), left_reason=reason[:40])
    kind, role = _kind_and_role(p)
    if kind != Attendance.Kind.AGENT:
        MeetingSession.objects.filter(pk=session.pk).update(last_human_at=timezone.now())
        return
    # An agent left. If it was the recorder and people are still talking, send
    # another one — the recording continues in a new segment.
    run = AgentRun.objects.filter(session=session, agent_identity=identity).order_by("-pk").first()
    if run and run.is_live:
        AgentRun.objects.filter(pk=run.pk).update(
            state=AgentRun.State.FAILED if role != "scribe" else AgentRun.State.STOPPED,
            ended_at=timezone.now(), error=(reason or "left the room")[:300])
    session.refresh_from_db()
    if role == "scribe" and session.is_live and session.recording_enabled:
        humans = Attendance.objects.filter(session=session, left_at__isnull=True).exclude(
            kind=Attendance.Kind.AGENT).exists()
        if humans:
            MeetingSession.objects.filter(pk=session.pk).update(
                recording_state=MeetingSession.Recording.STARTING)
            rooms.dispatch_recorder(session)


HANDLERS = {
    "room_started": _room_started,
    "room_finished": _room_finished,
    "participant_joined": _participant_joined,
    "participant_left": _participant_left,
}
