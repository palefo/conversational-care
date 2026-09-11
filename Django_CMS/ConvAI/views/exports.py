"""The download behind Settings -> Export. See ConvAI/message_export.py."""
import datetime as dt
import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from .. import message_export
from ..roles import admin_required

__all__ = ['export_messages']

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
