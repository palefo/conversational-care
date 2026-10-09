"""Keeping online meetings in step with what happens to the meeting itself.

The core sends signals (ConvAI/extensions.py) when a meeting is cancelled,
edited or closed with an outcome; this app reacts. Nothing here is allowed to
fail the core's request: every receiver logs and carries on.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _end_live(meeting, reason):
    from . import rooms
    session = rooms.live_session(meeting)
    if session:
        rooms.end(session, reason=reason)


def _revoke_all(meeting, reason):
    from .models import MeetingInvite
    for invite in MeetingInvite.objects.filter(meeting=meeting, revoked_at__isnull=True):
        invite.revoke(reason)


def on_cancelled(sender, meeting, by=None, **kwargs):
    try:
        _revoke_all(meeting, "cancelled")
        _end_live(meeting, "cancelled")
    except Exception:
        logger.exception("Could not close online meeting %s on cancel", meeting.pk)


def on_changed(sender, meeting, changed, by=None, **kwargs):
    """A new client or a different modality means the old link must stop working.

    A new *time* does not: the link is checked against the meeting's current
    time whenever it is used, so the same link simply works at the new time.
    """
    from ConvAI.models import Meeting
    try:
        if "patient_id" in changed or ("modality" in changed
                                       and meeting.modality != Meeting.Modality.ONLINE):
            _revoke_all(meeting, "meeting_changed")
            _end_live(meeting, "meeting_changed")
    except Exception:
        logger.exception("Could not update online meeting %s after an edit", meeting.pk)


def on_completed(sender, meeting, status, by=None, **kwargs):
    from ConvAI.models import Meeting
    try:
        if status != Meeting.Status.PENDING:
            _end_live(meeting, "outcome_recorded")
    except Exception:
        logger.exception("Could not close online meeting %s on completion", meeting.pk)


def connect():
    from ConvAI import extensions
    extensions.meeting_cancelled.connect(on_cancelled, dispatch_uid="meetings.on_cancelled")
    extensions.meeting_changed.connect(on_changed, dispatch_uid="meetings.on_changed")
    extensions.meeting_completed.connect(on_completed, dispatch_uid="meetings.on_completed")
