"""An .ics calendar entry for an online meeting.

A stable UID per meeting and a SEQUENCE that follows the start time, so a
calendar that already holds the event updates it when the meeting moves rather
than adding a second one; a cancelled meeting produces METHOD:CANCEL.
"""
from __future__ import annotations

from datetime import timedelta, timezone as dt_timezone

from django.utils import timezone
from django.utils.translation import gettext as _

from . import links

DURATION = timedelta(minutes=45)


def _fmt(dt) -> str:
    return dt.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _esc(text: str) -> str:
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545 lines are folded at 75 octets."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > 74:
            out.append(chunk.decode("utf-8"))
            chunk = b" " + b
        else:
            chunk += b
    out.append(chunk.decode("utf-8"))
    return "\r\n".join(out)


def invite_ics(invite, request=None, *, cancelled: bool = False) -> str:
    from ConvAI.site_config import brand_name

    meeting = invite.meeting
    start = meeting.scheduled_time
    url = links.url_for(invite, request)
    navigator = meeting.patient.navigator
    host = (navigator.get_full_name() or navigator.username) if navigator else brand_name()
    summary = _("Online meeting with %(host)s") % {"host": host}
    description = _("Join from your phone or computer — there is nothing to install:\n%(url)s") % {"url": url}
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{_esc(brand_name())}//Online meetings//EN",
        "CALSCALE:GREGORIAN",
        f"METHOD:{'CANCEL' if cancelled else 'PUBLISH'}",
        "BEGIN:VEVENT",
        f"UID:meeting-{meeting.pk}-{invite.public_id}@conversational-care",
        f"SEQUENCE:{int(start.timestamp()) // 60 % 100000}",
        f"DTSTAMP:{_fmt(timezone.now())}",
        f"DTSTART:{_fmt(start)}",
        f"DTEND:{_fmt(start + DURATION)}",
        f"SUMMARY:{_esc(summary)}",
        f"DESCRIPTION:{_esc(description)}",
        f"URL:{_esc(url)}",
        f"LOCATION:{_esc(url)}",
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
        "BEGIN:VALARM",
        "TRIGGER:-PT15M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_esc(summary)}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
