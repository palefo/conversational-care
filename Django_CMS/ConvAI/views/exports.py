"""The downloads behind Settings -> Export. See ConvAI/message_export.py.

Two of them, each behind its own switch: every message at once for admins,
and one conversation at a time from the panel for whoever can read it.
"""
import datetime as dt
import logging
import re

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from .. import message_export
from ..models import Message, Patient
from ..roles import admin_required, navigator_required
from ..utils import patient_message_q
from ._panel import _can_see

__all__ = ['export_messages', 'download_conversation']

log = logging.getLogger(__name__)


def _day(request, key):
    """The date in ``?key=YYYY-MM-DD``: None when absent, False when unreadable."""
    raw = (request.GET.get(key) or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return False


def _midnight(day):
    return timezone.make_aware(dt.datetime.combine(day, dt.time.min))


@login_required
@admin_required
@require_GET
def export_messages(request):
    """Every stored message as CSV, optionally limited to a range of days.

    Admins only, and only while MESSAGE_EXPORT_ENABLED is on. With it off the
    page does not exist — 404, not 403 — so an installation that never turned
    it on has nothing to find.
    """
    if not message_export.enabled():
        raise Http404

    first, last = _day(request, "from"), _day(request, "to")
    # Refused rather than ignored: a typo in a date would otherwise quietly
    # export everything, which is the opposite of what was asked for.
    if first is False or last is False:
        return HttpResponseBadRequest("Dates must be in YYYY-MM-DD format.")
    if first and last and first > last:
        return HttpResponseBadRequest("The start date is after the end date.")

    # Whole days in the platform timezone, both ends included.
    start = _midnight(first) if first else None
    end = _midnight(last + dt.timedelta(days=1)) if last else None

    name = f"messages_{timezone.localdate():%Y%m%d}"
    if first:
        name += f"_from_{first}"
    if last:
        name += f"_to_{last}"

    # Worth a line in the log: this is every client's conversation leaving the
    # platform in one file.
    log.info("Message export by user %s (from=%s, to=%s)", request.user.pk, first, last)

    response = StreamingHttpResponse(
        message_export.csv_chunks(start, end),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
@navigator_required
@require_GET
def download_conversation(request, patient_pk, conversation_id):
    """One conversation of one client, as CSV — the button on each conversation
    in the panel's Conversation tab.

    Same file as the full export, columns and all, narrowed to one
    conversation: whoever analyses these gets one format, not two.

    Anyone who can open the client's panel can take it — the client's own
    navigator, or an admin — and only while CONVERSATION_DOWNLOAD_ENABLED is
    on. Everything else is a 404: the switch being off, a client who is not
    yours, a conversation with nothing of this client's in it. None of those
    should tell the asker which of them it was.

    The whole conversation, not just the day the panel was showing: a
    conversation that ran past midnight is still one conversation, and the
    panel says so on the divider where that happens. Only this client's
    messages in it, though — the same predicate the panel reads them with — so
    the id in the URL cannot reach anyone else's.
    """
    if not message_export.conversation_download_enabled():
        raise Http404
    patient = Patient.objects.select_related("caregiver").filter(pk=patient_pk).first()
    if not _can_see(request.user, patient):
        raise Http404

    msgs = Message.objects.filter(patient_message_q(patient), conversation_id=conversation_id)
    first = msgs.order_by("timestamp").values_list("timestamp", flat=True).first()
    if first is None:
        raise Http404

    # Named by when it started, so a folder of these sorts into the order they
    # happened. No names: the file can outlive the reason it was taken.
    short = re.sub(r"[^A-Za-z0-9-]", "", conversation_id)[:8] or "chat"
    name = f"conversation_{timezone.localtime(first):%Y%m%d_%H%M}_{short}"

    log.info("Conversation download by user %s (patient=%s, conversation=%s)",
             request.user.pk, patient.pk, conversation_id)

    response = StreamingHttpResponse(
        message_export.csv_chunks(messages=msgs),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    response["Cache-Control"] = "no-store"
    return response
