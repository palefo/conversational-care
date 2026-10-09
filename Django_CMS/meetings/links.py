"""Client links: ``/m/<public_id>/<token>``.

The token is ``HMAC-SHA256(MEETING_LINK_KEY, "<public_id>:<nonce>")``, cut to
128 bits and base64url-encoded. So:

* **Nothing secret is stored.** The link can be rebuilt to send again, and a
  database dump does not contain working links.
* **Revoking is a row update.** ``revoked_at`` stops an invite; rotating its
  ``nonce`` kills every copy of the old link while issuing a new one.
* **Guessing is not a strategy.** 128 bits per invite; the lookup is by the
  public id and the comparison constant-time.

Without ``MEETING_LINK_KEY`` the key is derived from SECRET_KEY under its own
salt, which is fine until SECRET_KEY rotates — at which point every outstanding
link stops working, which is exactly what rotating it should do.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac

from . import config

_SALT = "conversational-care.meetings.link.v1"


def _key() -> bytes:
    explicit = config._env("MEETING_LINK_KEY")
    if explicit:
        return explicit.encode()
    return salted_hmac(_SALT, "link-key", secret=settings.SECRET_KEY,
                       algorithm="sha256").digest()


def token_for(invite) -> str:
    mac = hmac.new(_key(), f"{invite.public_id}:{invite.nonce}".encode(),
                   hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def path_for(invite) -> str:
    return f"/m/{invite.public_id}/{token_for(invite)}"


def url_for(invite, request=None) -> str:
    return config.public_base_url(request) + path_for(invite)


def resolve(public_id: str, token: str):
    """The invite this link belongs to, or None. Does not judge validity."""
    from .models import MeetingInvite

    if not public_id or not token or len(public_id) > 24 or len(token) > 64:
        return None
    invite = (MeetingInvite.objects
              .select_related("meeting", "meeting__patient", "meeting__patient__navigator")
              .filter(public_id=public_id).first())
    if invite is None:
        # Spend the same effort as a real check, so timing does not say
        # whether the public id exists.
        hmac.compare_digest(token, token_for(_Dummy))
        return None
    if not hmac.compare_digest(token, token_for(invite)):
        return None
    return invite


class _Dummy:
    public_id = "x" * 16
    nonce = "x" * 24


# ── Is the link usable right now? ──────────────────────────────────────────

class Verdict:
    OK = "ok"
    EARLY = "early"           # valid, too soon — show the time
    UNAVAILABLE = "unavailable"  # anything else: revoked, cancelled, over, off


def window(meeting):
    s = config.cfg()
    start = meeting.scheduled_time - timedelta(minutes=s.link_early_minutes)
    end = meeting.scheduled_time + timedelta(hours=s.link_late_hours)
    return start, end


def verdict(invite, *, now=None) -> str:
    """Whether this invite can be used now.

    Checked against the meeting as it is *now* — a rescheduled meeting keeps
    its link, which simply works at the new time. A room the navigator has
    actually opened always admits its invitees, early or late, as long as the
    meeting is still on.
    """
    from ConvAI.models import Meeting

    from .models import MeetingSession

    now = now or timezone.now()
    meeting = invite.meeting
    if invite.is_revoked or not config.enabled():
        return Verdict.UNAVAILABLE
    if meeting.modality != Meeting.Modality.ONLINE:
        return Verdict.UNAVAILABLE
    if meeting.status == Meeting.Status.CANCELLED:
        return Verdict.UNAVAILABLE
    live = MeetingSession.objects.filter(meeting=meeting,
                                         status=MeetingSession.Status.LIVE).exists()
    if live:
        return Verdict.OK
    if meeting.status != Meeting.Status.PENDING:
        return Verdict.UNAVAILABLE
    start, end = window(meeting)
    if now < start:
        return Verdict.EARLY
    if now > end:
        return Verdict.UNAVAILABLE
    return Verdict.OK
