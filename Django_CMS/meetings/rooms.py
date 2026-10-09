"""Opening and closing rooms, letting people in, sending agents in.

The rules a navigator meets live here, not in the views, so the webhook, the
reaper job and the buttons all apply the same ones.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from . import agent_tokens, config, livekit_api
from .models import AgentRun, Attendance, LobbyEntry, MeetingSession

logger = logging.getLogger(__name__)

SCRIBE_AGENT = "cc-scribe"
VOICE_AGENT = "cc-voice"
TOKEN_TTL_S = 15 * 60
MAX_RECORDER_DISPATCHES = 3


class MeetingError(Exception):
    """Something a navigator should be told, in words they can act on."""


# ── Who is who ─────────────────────────────────────────────────────────────

def staff_identity(user) -> str:
    return f"staff-{user.pk}"


def staff_name(user) -> str:
    return (user.get_full_name() or user.get_username() or "").strip()


def live_session(meeting):
    return (MeetingSession.objects
            .filter(meeting=meeting, status=MeetingSession.Status.LIVE).first())


# ── Starting and ending ────────────────────────────────────────────────────

def start(meeting, user) -> MeetingSession:
    """Open the meeting's room, or return the one already open.

    Idempotent and race-safe: the database allows one live session per
    meeting, so two people pressing Start together end up in the same room.
    """
    from ConvAI.models import Meeting

    # A room already open keeps working if the feature is switched off
    # meanwhile: the meeting in progress finishes, nothing new starts.
    session = live_session(meeting)
    if session:
        return session
    if not config.enabled():
        raise MeetingError(_("Online meetings are switched off."))
    if not config.livekit_configured():
        raise MeetingError(_("Online meetings are not connected to a meeting server yet. "
                             "An administrator can finish the setup in Settings → Online meetings."))
    if meeting.modality != Meeting.Modality.ONLINE:
        raise MeetingError(_("This meeting is not an online meeting."))
    if meeting.status == Meeting.Status.CANCELLED:
        raise MeetingError(_("This meeting was cancelled."))

    s = config.cfg()
    live_rooms = MeetingSession.objects.filter(status=MeetingSession.Status.LIVE).count()
    if live_rooms >= s.max_live_rooms:
        raise MeetingError(_("The maximum number of meetings that can run at once (%(n)d) "
                             "has been reached. Try again when one has ended.")
                           % {"n": s.max_live_rooms})

    try:
        with transaction.atomic():
            session = MeetingSession.objects.create(
                meeting=meeting, started_by=user,
                auto_admit=s.auto_admit,
                recording_enabled=s.record_by_default,
                recording_state=(MeetingSession.Recording.STARTING if s.record_by_default
                                 else MeetingSession.Recording.OFF),
            )
    except IntegrityError:
        # Someone else opened it a moment ago.
        session = live_session(meeting)
        if session:
            return session
        raise

    try:
        livekit_api.create_room(session.room_name, max_participants=8,
                                empty_timeout_s=300, departure_timeout_s=60)
    except livekit_api.LiveKitError as exc:
        session.status = MeetingSession.Status.ENDED
        session.ended_at = timezone.now()
        session.end_reason = "server_unavailable"
        session.recording_state = MeetingSession.Recording.OFF
        session.save(update_fields=["status", "ended_at", "end_reason", "recording_state"])
        raise MeetingError(str(exc)) from exc

    # A meeting that was started and never closed with an outcome is what the
    # panel's "Outcome missing" looks for (retries > 0 and still pending), the
    # same rule a placed phone call follows.
    if not MeetingSession.objects.filter(meeting=meeting).exclude(pk=session.pk).exists():
        Meeting.objects.filter(pk=meeting.pk).update(retries=F("retries") + 1)

    # Anyone already waiting in the lobby now has a room to be let into.
    LobbyEntry.objects.filter(meeting=meeting, state=LobbyEntry.State.WAITING).update(session=session)

    if session.recording_enabled:
        dispatch_recorder(session)
    return session


def end(session, *, reason: str = "ended", by=None) -> None:
    """Close the room for everyone and stop its agents."""
    if not session.is_live:
        return
    updated = MeetingSession.objects.filter(pk=session.pk, status=MeetingSession.Status.LIVE).update(
        status=MeetingSession.Status.ENDED, ended_at=timezone.now(), end_reason=reason[:60],
        recording_state=(MeetingSession.Recording.STOPPED if session.recording_enabled
                         else MeetingSession.Recording.OFF),
    )
    if not updated:
        return
    session.refresh_from_db()
    AgentRun.objects.filter(session=session, state__in=AgentRun.LIVE_STATES).update(
        state=AgentRun.State.STOPPED, ended_at=timezone.now())
    Attendance.objects.filter(session=session, left_at__isnull=True).update(
        left_at=timezone.now(), left_reason="room_closed")
    LobbyEntry.objects.filter(session=session, state=LobbyEntry.State.WAITING).update(
        state=LobbyEntry.State.LEFT)
    try:
        livekit_api.delete_room(session.room_name)
    except livekit_api.LiveKitError:
        logger.warning("Could not delete room %s; it will expire on its own.", session.room_name)
    if session.recording_enabled:
        schedule_finalize(session)


def schedule_finalize(session, delay_s: int = 20) -> None:
    """Queue the mixdown. Delayed a little so the recorder's last segment lands."""
    from ConvAI.jobs import enqueue

    enqueue("meetings.finalize_recording", {"session_id": session.pk},
            ref=f"meetingsession:{session.pk}",
            dedupe_key=f"meetings.finalize:{session.pk}",
            run_after=timezone.now() + timedelta(seconds=delay_s))


# ── Tokens ─────────────────────────────────────────────────────────────────

def staff_token(session, user) -> str:
    return livekit_api.participant_token(
        identity=staff_identity(user), name=staff_name(user), room=session.room_name,
        attributes={"cc.role": "navigator", "cc.record": "1"},
        # Staff talk to the agents over RPC and data, so they can publish data;
        # clients cannot.
        can_publish_data=True, ttl_s=TOKEN_TTL_S,
    )


def invitee_token(session, invite) -> str:
    return livekit_api.participant_token(
        identity=invite.identity, name=invite.display_name or _("Guest"),
        room=session.room_name,
        attributes={"cc.role": invite.invitee,
                    "cc.record": "1" if invite.record_allowed else "0"},
        can_publish_data=False, ttl_s=TOKEN_TTL_S,
    )


# ── Admission ──────────────────────────────────────────────────────────────

def knock(invite, *, user_agent: str = "") -> LobbyEntry:
    """Someone with a link asks to come in."""
    from .models import RecordingConsent

    session = live_session(invite.meeting)
    entry, _created = LobbyEntry.objects.get_or_create(
        meeting=invite.meeting, invite=invite,
        defaults={"session": session, "state": LobbyEntry.State.WAITING},
    )
    if entry.state in (LobbyEntry.State.LEFT, LobbyEntry.State.DENIED) or entry.session_id != (
            session.pk if session else None):
        # Coming back after leaving, or a new room since: ask again. A denial
        # does not stick to a link forever — the navigator may have turned away
        # the wrong person — but it does have to be asked for again.
        entry.state = LobbyEntry.State.WAITING
        entry.decided_at = None
        entry.decided_by = None
    entry.session = session
    entry.seen_at = timezone.now()
    if session and session.auto_admit and _host_present(session):
        entry.state = LobbyEntry.State.ADMITTED
        entry.decided_at = timezone.now()
    entry.save()

    if session and session.recording_enabled and invite.record_allowed:
        RecordingConsent.objects.create(
            invite=invite, session=session, accepted=True,
            notice_version=RECORDING_NOTICE_VERSION, user_agent=user_agent[:300])

    if session and entry.state == LobbyEntry.State.WAITING:
        _notify_staff(session, {"type": "knock", "invite": invite.public_id,
                                "name": invite.display_name})
    return entry


RECORDING_NOTICE_VERSION = "1"


def _host_present(session) -> bool:
    return Attendance.objects.filter(session=session, kind=Attendance.Kind.STAFF,
                                     left_at__isnull=True).exists()


def decide(session, invite, *, admit: bool, by) -> LobbyEntry | None:
    entry = LobbyEntry.objects.filter(meeting=session.meeting, invite=invite).first()
    if entry is None:
        return None
    entry.state = LobbyEntry.State.ADMITTED if admit else LobbyEntry.State.DENIED
    entry.session = session
    entry.decided_at = timezone.now()
    entry.decided_by = by
    entry.save(update_fields=["state", "session", "decided_at", "decided_by"])
    if not admit:
        # In case they were already in (admitted earlier, then removed).
        try:
            livekit_api.remove_participant(session.room_name, invite.identity)
        except livekit_api.LiveKitError:
            pass
    return entry


def _notify_staff(session, payload: dict) -> None:
    """Tell the staff in the room something happened, without them polling."""
    staff = list(Attendance.objects.filter(session=session, kind=Attendance.Kind.STAFF,
                                           left_at__isnull=True)
                 .values_list("identity", flat=True).distinct())
    if not staff:
        return
    try:
        livekit_api.send_data(session.room_name, payload, to=staff, topic="cc.lobby")
    except livekit_api.LiveKitError:
        pass  # the room page also polls; a lost nudge only costs a second or two


# ── Agents ─────────────────────────────────────────────────────────────────

def _dispatch(run: AgentRun, agent_name: str, extra: dict | None = None) -> AgentRun:
    metadata = {
        "run_id": run.pk,
        "role": run.role,
        "token": agent_tokens.mint(run),
        "api_base": config._env("MEETINGS_INTERNAL_API_URL", "http://web:8000"),
        **(extra or {}),
    }
    try:
        run.dispatch_id = livekit_api.create_dispatch(run.session.room_name, agent_name, metadata)
    except livekit_api.LiveKitError as exc:
        run.state = AgentRun.State.FAILED
        run.error = str(exc)[:300]
        run.ended_at = timezone.now()
        run.save(update_fields=["state", "error", "ended_at"])
        raise MeetingError(str(exc)) from exc
    run.save(update_fields=["dispatch_id"])
    return run


def dispatch_recorder(session) -> AgentRun | None:
    if not config.service_key():
        logger.warning("Not recording session %s: MEETINGS_SERVICE_KEY is not set.", session.pk)
        MeetingSession.objects.filter(pk=session.pk).update(
            recording_state=MeetingSession.Recording.LOST)
        return None
    if session.recorder_dispatches >= MAX_RECORDER_DISPATCHES:
        MeetingSession.objects.filter(pk=session.pk).update(
            recording_state=MeetingSession.Recording.LOST)
        return None
    run = AgentRun.objects.create(session=session, role=AgentRun.Role.SCRIBE)
    MeetingSession.objects.filter(pk=session.pk).update(
        recorder_dispatches=F("recorder_dispatches") + 1,
        recording_state=MeetingSession.Recording.STARTING)
    try:
        return _dispatch(run, SCRIBE_AGENT)
    except MeetingError:
        MeetingSession.objects.filter(pk=session.pk).update(
            recording_state=MeetingSession.Recording.LOST)
        return None


def start_interview(session, *, protocol, respondent_identity: str, respondent_name: str,
                    language: str, by) -> AgentRun:
    """Send the voice interviewer in to work through ``protocol`` with one person."""
    meeting = session.meeting
    if not config.service_key():
        raise MeetingError(_("The meeting agents are not set up on this server "
                             "(MEETINGS_SERVICE_KEY is missing)."))
    if AgentRun.objects.filter(session=session, role=AgentRun.Role.INTERVIEWER,
                               state__in=AgentRun.LIVE_STATES).exists():
        raise MeetingError(_("An interview is already running in this meeting."))
    patient = meeting.patient
    if patient.automation_active and patient.automation_meeting_id == meeting.pk:
        raise MeetingError(_("These questions are out with the client by text right now. "
                             "Wait for that to finish before interviewing by voice."))
    run = AgentRun.objects.create(
        session=session, role=AgentRun.Role.INTERVIEWER, protocol=protocol,
        respondent_identity=respondent_identity, respondent_name=respondent_name[:80],
        controller_identity=staff_identity(by), language=language[:10], started_by=by,
    )
    return _dispatch(run, VOICE_AGENT)


def start_assistant(session, *, by) -> AgentRun:
    if not config.service_key():
        raise MeetingError(_("The meeting agents are not set up on this server "
                             "(MEETINGS_SERVICE_KEY is missing)."))
    if not config.cfg().assistant_agent_id:
        raise MeetingError(_("No assistant is chosen for meetings. An administrator can "
                             "pick one in Settings → Online meetings."))
    live = AgentRun.objects.filter(session=session, role=AgentRun.Role.ASSISTANT,
                                   state__in=AgentRun.LIVE_STATES).first()
    if live:
        return live
    run = AgentRun.objects.create(
        session=session, role=AgentRun.Role.ASSISTANT,
        controller_identity=staff_identity(by), started_by=by,
    )
    return _dispatch(run, VOICE_AGENT)


def stop_run(run: AgentRun, *, state=AgentRun.State.STOPPED) -> None:
    """Take an agent out of the room, whatever it is doing."""
    if run.is_live:
        AgentRun.objects.filter(pk=run.pk).update(state=state, ended_at=timezone.now())
    session = run.session
    try:
        if run.agent_identity:
            livekit_api.remove_participant(session.room_name, run.agent_identity)
        livekit_api.delete_dispatch(session.room_name, run.dispatch_id)
    except livekit_api.LiveKitError:
        pass


# ── What the room page shows ───────────────────────────────────────────────

def state_for(session, *, for_staff: bool) -> dict:
    """A snapshot of the room for the pages that render it."""
    if session is None:
        return {"live": False}
    out = {
        "live": session.is_live,
        "session": str(session.uuid),
        "started_at": session.started_at.isoformat(),
        "recording": {
            "enabled": session.recording_enabled,
            "state": session.recording_state,
        },
    }
    if not for_staff:
        return out
    # An agent nobody picked up: no worker is running, or none has room. Said
    # after a reasonable wait rather than spinning "Connecting…" forever.
    cutoff = timezone.now() - timedelta(seconds=45)
    AgentRun.objects.filter(session=session, state=AgentRun.State.DISPATCHED,
                            created_at__lt=cutoff, last_status_at__isnull=True).update(
        state=AgentRun.State.FAILED, ended_at=timezone.now(),
        error=_("No agent worker picked this up. Check that the meeting agents are running."))
    if (session.recording_state == MeetingSession.Recording.STARTING
            and not AgentRun.objects.filter(session=session, role=AgentRun.Role.SCRIBE,
                                            state__in=AgentRun.LIVE_STATES).exists()):
        MeetingSession.objects.filter(pk=session.pk).update(
            recording_state=MeetingSession.Recording.LOST)
        out["recording"]["state"] = MeetingSession.Recording.LOST
    out["lobby"] = [
        {"invite": e.invite.public_id, "name": e.invite.display_name or _("Guest"),
         "role": e.invite.get_invitee_display(), "since": e.created_at.isoformat(),
         # A lobby tab that stopped polling has most likely been closed.
         "stale": (timezone.now() - e.seen_at).total_seconds() > 12}
        for e in (LobbyEntry.objects.filter(meeting=session.meeting, state=LobbyEntry.State.WAITING)
                  .select_related("invite"))
        if (timezone.now() - e.seen_at).total_seconds() < 60
    ]
    out["runs"] = [
        {"id": r.pk, "role": r.role, "state": r.state, "identity": r.agent_identity,
         "protocol": r.protocol.number if r.protocol_id else None,
         "protocol_title": r.protocol.title if r.protocol_id else "",
         "respondent": r.respondent_identity, "respondent_name": r.respondent_name,
         "answers_saved": r.answers_saved, "error": r.error}
        for r in session.agent_runs.select_related("protocol").order_by("-created_at")[:10]
    ]
    out["auto_admit"] = session.auto_admit
    return out


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))
