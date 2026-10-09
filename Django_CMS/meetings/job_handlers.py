"""Background jobs for online meetings. Run by the core job worker."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from ConvAI.jobs import PermanentError, handler, periodic

logger = logging.getLogger(__name__)


@handler("meetings.finalize_recording", max_attempts=3, backoff_s=60)
def finalize_recording(payload: dict):
    """Mix a closed session's recording down and queue its transcription."""
    from ConvAI.summarization import queue_transcription

    from .models import MeetingSession
    from .recording import mixdown

    session = MeetingSession.objects.filter(pk=payload.get("session_id")).select_related(
        "meeting", "meeting__patient").first()
    if session is None:
        raise PermanentError("The meeting session no longer exists.")
    if session.is_live:
        # Ended and restarted in the meantime: finalise when this one ends.
        return {"skipped": "session is live again"}
    recording = mixdown(session)
    if recording is None:
        return {"recorded": False}
    queue_transcription(recording)
    return {"recorded": True, "recording_id": recording.pk,
            "speakers": len(recording.speakers or {})}


@handler("meetings.reconcile", max_attempts=1)
@periodic("meetings.reconcile", every_s=120)
def reconcile(payload: dict):
    """Catch what a missed webhook or a crashed worker left behind.

    Cheap when nothing is happening: one indexed query for live sessions.
    """
    from . import config, livekit_api, rooms
    from .models import Attendance, MeetingSession

    live = list(MeetingSession.objects.filter(status=MeetingSession.Status.LIVE))
    if not live:
        return {"live": 0}
    s = config.cfg()
    now = timezone.now()
    ended = redispatched = 0
    for session in live:
        if now - session.started_at > timedelta(minutes=s.max_minutes):
            rooms.end(session, reason="time_limit")
            ended += 1
            continue
        try:
            participants = livekit_api.list_participants(session.room_name)
        except livekit_api.RoomGone:
            rooms.end(session, reason="room_gone")
            ended += 1
            continue
        except livekit_api.LiveKitError:
            continue  # server unreachable: try again next time
        humans = [p for p in participants
                  if not str(p.get("identity", "")).startswith("agent-")
                  and str(p.get("kind", "")).upper() not in ("AGENT", "4")]
        if humans:
            MeetingSession.objects.filter(pk=session.pk).update(last_human_at=now)
        elif now - session.last_human_at > timedelta(minutes=5):
            rooms.end(session, reason="empty")
            ended += 1
            continue
        # Attendance rows still open for people LiveKit no longer has.
        present = {p.get("sid") for p in participants}
        Attendance.objects.filter(session=session, left_at__isnull=True).exclude(
            participant_sid__in=present).update(left_at=now, left_reason="reconciled")
        # The recorder went missing without a webhook saying so.
        if session.recording_enabled and humans:
            has_scribe = any((p.get("attributes") or {}).get("cc.role") == "scribe"
                             for p in participants)
            stale = (session.recorder_seen_at is None
                     or now - session.recorder_seen_at > timedelta(seconds=90))
            if not has_scribe and stale and session.recording_state != MeetingSession.Recording.LOST:
                rooms.dispatch_recorder(session)
                redispatched += 1
    return {"live": len(live), "ended": ended, "recorders_redispatched": redispatched}
