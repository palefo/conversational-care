"""What the core asks this app, through ConvAI.extensions.

Kept import-light: the module is loaded at start-up even with the feature off,
and everything heavier is imported inside the method that needs it.
"""
from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _


class OnlineMeetingsProvider:
    def available(self) -> bool:
        from . import config
        return config.enabled()

    def panel_extras(self, request, meeting) -> dict:
        """What the meeting panel shows for an ONLINE meeting.

        With the feature off, the panel gets nothing and shows the meeting as a
        record with no way to start it.
        """
        from . import config, invites, links, rooms
        from .models import AgentRun, InviteDelivery

        if not config.enabled():
            return {}
        invite = invites.primary_invite(meeting)
        session = rooms.live_session(meeting)
        last = (InviteDelivery.objects.filter(invite=invite, ok=True).order_by("-sent_at").first()
                if invite else None)
        kind, person = invites.default_invitee(meeting)
        interview = (AgentRun.objects.filter(session=session, role=AgentRun.Role.INTERVIEWER,
                                             state__in=AgentRun.LIVE_STATES).exists()
                     if session else False)
        note = ""
        if invite and last:
            note = _("Link sent by %(channel)s") % {"channel": last.get_channel_display().lower()}
        elif invite:
            note = _("Link ready — not sent yet")
        else:
            note = _("No link yet")
        return {
            "footer_template": "meetings/_panel_footer.html",
            "configured": config.livekit_configured(),
            "invitee_name": (getattr(person, "name", "") or "").strip() if person else "",
            "invitee_role": (_("the caregiver") if kind == "caregiver" else _("the client")),
            "link_note": note,
            "invite": invite,
            "join_url": links.url_for(invite, request) if invite else "",
            "last_delivery": last,
            "has_email": bool(getattr(person, "email", "") if person else ""),
            "has_phone": bool(getattr(person, "phone_number", "") if person else ""),
            "live": bool(session),
            "interview_running": interview,
            "room_url": reverse("meetings:staff_room", args=[meeting.pk]),
            "invite_url": reverse("meetings:invite", args=[meeting.pk]),
            "send_url": reverse("meetings:invite_send", args=[meeting.pk]),
            "ics_url": reverse("meetings:invite_ics", args=[meeting.pk]),
        }

    def reminder_extras(self, meeting) -> dict:
        from . import config, invites, links
        if not config.enabled():
            return {}
        invite = invites.primary_invite(meeting, create=True)
        return {"join_url": links.url_for(invite)} if invite else {}

    def live_state(self, meeting):
        from . import rooms
        from .models import AgentRun
        session = rooms.live_session(meeting)
        if not session:
            return {"live": False, "interview_running": False}
        return {
            "live": True,
            "interview_running": AgentRun.objects.filter(
                session=session, role=AgentRun.Role.INTERVIEWER,
                state__in=AgentRun.LIVE_STATES).exists(),
        }

    def settings_tabs(self):
        from .settings_tab import TAB
        return [TAB]
