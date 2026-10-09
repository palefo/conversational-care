"""Creating invites and sending links."""
from __future__ import annotations

import logging

from django.utils import timezone
from django.utils.translation import gettext as _

from . import links
from .models import InviteDelivery, MeetingInvite

logger = logging.getLogger(__name__)


def default_invitee(meeting):
    """Who an online meeting is with, by the same rule reminders use.

    The caregiver first — they are who appointments are arranged with — and
    the client when there is no caregiver.
    """
    patient = meeting.patient
    caregiver = getattr(patient, "caregiver", None)
    if caregiver:
        return MeetingInvite.Invitee.CAREGIVER, caregiver
    return MeetingInvite.Invitee.CLIENT, patient


def _first_name(person) -> str:
    return (getattr(person, "name", "") or "").strip().split(" ")[0][:80]


def primary_invite(meeting, *, create: bool = False, by=None):
    """The meeting's live invite for its usual invitee, made if asked to."""
    kind, person = default_invitee(meeting)
    invite = (MeetingInvite.objects
              .filter(meeting=meeting, invitee=kind, revoked_at__isnull=True)
              .order_by("-created_at").first())
    if invite or not create:
        return invite
    return create_invite(meeting, kind=kind, by=by)


def create_invite(meeting, *, kind=None, by=None, language="") -> MeetingInvite:
    if kind is None:
        kind, person = default_invitee(meeting)
    elif kind == MeetingInvite.Invitee.CLIENT:
        person = meeting.patient
    elif kind == MeetingInvite.Invitee.CAREGIVER:
        person = getattr(meeting.patient, "caregiver", None)
    else:
        person = None
    return MeetingInvite.objects.create(
        meeting=meeting,
        invitee=kind,
        patient=meeting.patient,
        caregiver=getattr(meeting.patient, "caregiver", None),
        display_name=_first_name(person) if person else "",
        language=language or "",
        record_allowed=True,
        created_by=by if getattr(by, "pk", None) else None,
    )


def invite_person(invite):
    """The Patient or Caregiver an invite is for (None for "other")."""
    if invite.invitee == MeetingInvite.Invitee.CLIENT:
        return invite.patient
    if invite.invitee == MeetingInvite.Invitee.CAREGIVER:
        return invite.caregiver
    return None


def _mask(value: str) -> str:
    value = (value or "").strip()
    if "@" in value:
        local, _sep, domain = value.partition("@")
        return (local[:1] + "•••@" + domain)[:80]
    return ("•••" + value[-4:]) if len(value) > 4 else "•••"


def send(invite, channel: str, *, by=None, request=None) -> tuple[bool, str]:
    """Send ``invite``'s link over ``channel``. Returns (ok, message for staff)."""
    from ConvAI.mailer import render_email, send_email
    from email.utils import formataddr

    meeting = invite.meeting
    person = invite_person(invite)
    url = links.url_for(invite, request)
    when = timezone.localtime(meeting.scheduled_time)
    navigator = meeting.patient.navigator
    host = (navigator.get_full_name() or navigator.username) if navigator else ""

    ok, err, to = False, "", ""
    try:
        if channel == InviteDelivery.Channel.EMAIL:
            to = (getattr(person, "email", "") or "").strip()
            if not to:
                raise ValueError(_("There is no email address for %(who)s.")
                                 % {"who": invite.display_name or _("this person")})
            text, html = render_email("meeting_invite", {
                "recipient_name": invite.display_name,
                "patient": meeting.patient,
                "host": host,
                "date": when.strftime("%d/%m/%Y"),
                "time": when.strftime("%H:%M"),
                "join_url": url,
            })
            subject = _("Your online meeting on %(date)s at %(time)s") % {
                "date": when.strftime("%d/%m/%Y"), "time": when.strftime("%H:%M")}
            from .ics import invite_ics
            send_email(subject=subject, to=[formataddr((invite.display_name, to))],
                       text=text, html=html,
                       attachments=[("meeting.ics", invite_ics(invite, request), "text/calendar")])
            ok = True
        elif channel in (InviteDelivery.Channel.SMS, InviteDelivery.Channel.WHATSAPP):
            to = str(getattr(person, "phone_number", "") or "")
            if not to:
                raise ValueError(_("There is no phone number for %(who)s.")
                                 % {"who": invite.display_name or _("this person")})
            body = _("Hello %(name)s, your online meeting with %(host)s is on %(date)s at "
                     "%(time)s. Join from your phone or computer — nothing to install: %(url)s") % {
                "name": invite.display_name or "", "host": host or _("your care team"),
                "date": when.strftime("%d/%m"), "time": when.strftime("%H:%M"), "url": url}
            if channel == InviteDelivery.Channel.SMS:
                from ConvAI.utils import send_sms_text
                ok = bool(send_sms_text(to, body))
                if not ok:
                    err = _("The SMS could not be sent.")
            else:
                # Free-form WhatsApp only reaches someone who wrote in during
                # the last 24 hours; outside that window it is refused, and the
                # reason is passed on so the navigator can fall back to SMS.
                from ConvAI.utils import send_whatsapp_text_result
                result = send_whatsapp_text_result(to, body)
                ok, err = bool(result.get("ok")), str(result.get("reason") or "")
        else:
            raise ValueError(_("Unknown channel."))
    except ValueError as exc:
        err = str(exc)
    except Exception as exc:  # provider failures: logged, reported, not raised
        logger.warning("Sending meeting invite %s by %s failed: %s", invite.pk, channel, exc)
        err = _("The message could not be sent.")

    InviteDelivery.objects.create(invite=invite, channel=channel, to_masked=_mask(to),
                                  ok=ok, error=err[:300],
                                  sent_by=by if getattr(by, "pk", None) else None)
    if ok:
        invite.last_sent_at = timezone.now()
        invite.save(update_fields=["last_sent_at"])
    return ok, err
