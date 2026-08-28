"""Communications — every interaction with every client, in one place.

Two halves rather than one timeline. What is still to come is a queue and wants
reading forwards, soonest first; what has already happened is a record and wants
reading backwards. A single reverse-chronological list can only serve one of
them, and it served the wrong one — 48 scheduled calls sat above the first thing
that had actually happened, putting today's work 48 rows below the fold.

A recording is not an event here. It belongs to the meeting it came from and is
played inside that meeting's panel; see build_patient_events, which folds the
two together for the same reason.
"""
import unicodedata

from ._base import *  # noqa: F401,F403
from ._panel import panel_context
from .patients import build_patient_events
from .calls import scoped_meeting_form, save_scheduled_meeting

from django.utils.timesince import timesince
from django.utils.translation import ngettext

__all__ = ['communications', 'comms_list_context']


# Rows per page. Twenty-five is about two screens with the day headers in
# place — enough that paging is rare while reading a week, few enough that the
# page stays quick on a caseload with hundreds of entries behind it.
PER_PAGE = 25


def _page_cells(current, total):
    """The numbers a pager shows, always the same count of cells.

    A window that changes width moves the arrows as you click through, so the
    next page ends up somewhere else each time. Beyond nine pages this is always
    seven cells — first, a five-wide window, last — with the ellipsis taking a
    number's place rather than being squeezed in beside it.
    """
    if total <= 9:
        return list(range(1, total + 1))
    start = min(max(current - 2, 2), total - 5)
    out = [1] + [start + i for i in range(5)] + [total]
    if start > 2:
        out[1] = None
    if start + 4 < total - 1:
        out[5] = None
    return out


def _kind_of(event):
    """The vocabulary the page filters on, which is not the model's.

    A meeting is a call or a visit depending on its modality, and that
    difference matters more to whoever is reading the list than the fact that
    both are Meeting rows.
    """
    if event['kind'] == 'meeting':
        return 'visit' if event.get('in_person') else 'call'
    if event['kind'] == 'conversation':
        return 'chat'
    if event['kind'] == 'alert':
        return 'alert'
    return 'other'


UP_CHIPS = (
    ('all', _("All")),
    ('late', _("Overdue")),
    ('call', _("Calls")),
    ('visit', _("Meetings")),
)
PAST_CHIPS = (
    ('all', _("All")),
    ('call', _("Calls")),
    ('visit', _("Meetings")),
    ('chat', _("Chats")),
    ('alert', _("Alerts")),
)

# How far back, on the Happened half only. Coming up is a queue you work to the
# end of, so cutting it by age would only ever hide work.
WHEN_CHIPS = (
    ('all', _("Any time"), None),
    ('7', _("Last 7 days"), 7),
    ('30', _("Last 30 days"), 30),
    ('90', _("Last 3 months"), 90),
)
WHEN_DAYS = {value: days for value, _label, days in WHEN_CHIPS}


def _day_label(when, today):
    """Named where naming helps, dated where it does not."""
    day = timezone.localtime(when).date()
    delta = (day - today).days
    rel = ''
    if delta == 0:
        rel = _("Today")
    elif delta == 1:
        rel = _("Tomorrow")
    elif delta == -1:
        rel = _("Yesterday")
    return day, formats.date_format(day, "D j M"), rel


def _row(event, now):
    """One list row, in the shape the template renders without further thought."""
    kind = _kind_of(event)
    patient = event['patient']
    caregiver = patient.caregiver
    when = timezone.localtime(event['ts'])

    row = {
        'kind': kind,
        'token': event['panel_token'],
        'who': f"{patient.name} {patient.lastname}".strip(),
        'time': formats.time_format(when, "H:i"),
        'badge': '', 'badge_class': '',
        'line2': '', 'detail': '', 'sub': '', 'dot': '',
        'is_late': False, 'cancelled': False,
    }

    if kind in ('call', 'visit'):
        # A call nobody booked has no protocol to be named after, and naming it
        # after its type printed "Protocol" over a row that covered none.
        row['line2'] = event['protocol'] or (
            _("Unscheduled call") if event.get('unscheduled') else event['meeting_type'])
        if kind == 'visit':
            row['detail'] = event['location'] or _("Location not set")
        elif event.get('dial_who') or caregiver:
            # Whoever the call actually rang. It was always the caregiver until
            # the client page could ring the client instead.
            row['detail'] = _("with %s") % (event.get('dial_who') or caregiver)

        if event['status_code'] == Meeting.Status.PENDING:
            if event.get('outcome_missing'):
                # The call went out and was never closed. Said on the row, so it
                # can be found again without opening every pending call to see
                # which ones were actually made.
                row['badge'] = _("Outcome missing")
                row['badge_class'] = 'late'
                row['sub'] = _("Called, not recorded")
                row['dot'] = 'no'
            elif event['ts'] < now:
                # The due date rather than "4 days late": same information, same
                # sort, no verdict. See _lateness in views/dashboard.py.
                row['is_late'] = True
                row['badge'] = _("Due %(d)s") % {
                    'd': formats.date_format(when, "D j M")}
                row['badge_class'] = 'late'
            else:
                row['badge'] = _("To do")
                row['badge_class'] = 'todo'
            if not event.get('outcome_missing'):
                row['sub'] = (
                    _("In-person meeting") if kind == 'visit'
                    else _("Unscheduled call") if event.get('unscheduled')
                    else _("Scheduled call"))
        else:
            row['sub'] = event['status']
            row['dot'] = {
                Meeting.Status.COMPLETED: 'done',
                Meeting.Status.INTERRUPTED: 'int',
                Meeting.Status.CANCELLED: 'off',
            }.get(event['status_code'], 'no')
            # Struck through, not removed. It stays in the history because it
            # was arranged; the stroke says it did not happen on purpose.
            if event['status_code'] == Meeting.Status.CANCELLED:
                row['cancelled'] = True
            if event.get('recording'):
                # The duration is the substantive recording's, and the count
                # says when there were earlier attempts, so a call that took
                # three tries to connect reads as one call rather than going
                # missing behind a single number.
                extra = event.get('recording_count', 1) - 1
                row['sub'] = "%s · %s" % (event['status'], event['recording']['duration_str'])
                if extra > 0:
                    row['sub'] = "%s %s" % (row['sub'], ngettext(
                        "(+%(n)d earlier attempt)", "(+%(n)d earlier attempts)",
                        extra) % {'n': extra})

    elif kind == 'chat':
        row['line2'] = _("Chatbot conversation")
        row['detail'] = ngettext("%(n)d message", "%(n)d messages",
                                 event['msg_count']) % {'n': event['msg_count']}
        row['sub'] = _("Chat")

    elif kind == 'alert':
        row['line2'] = event['title']
        row['detail'] = event['description']
        row['badge'] = event['priority']
        row['badge_class'] = {
            Alert.Priority.HIGH: 'high',
            Alert.Priority.MEDIUM: 'med',
        }.get(event['priority_level'], 'low')
        row['sub'] = event['status']

    else:
        # A recording that belongs to no call. Rare now that a call holds every
        # recording it produced, and named for what it is rather than "Call
        # recording": sitting in a list of calls under a title that reads like
        # one, it looked like a duplicate of the call above it. It is the
        # opposite — audio with no call to sit under, and this row is the only
        # way to reach it.
        row['line2'] = _("Recording with no call")
        row['detail'] = event.get('duration_str', '')
        row['sub'] = _("Unattached")

    return row


def _group_by_day(pairs, today):
    """Rows under day headers, keeping whatever order they arrive in."""
    groups, current = [], None
    for row, event in pairs:
        day, label, rel = _day_label(event['ts'], today)
        if current is None or current['day'] != day:
            current = {'day': day, 'label': label, 'rel': rel, 'rows': []}
            groups.append(current)
        current['rows'].append(row)
    for g in groups:
        g['count'] = len(g['rows'])
    return groups


def _fold(text):
    """Lower-case and strip accents, so a search box matches what people type."""
    return ''.join(
        ch for ch in unicodedata.normalize('NFKD', (text or '').lower())
        if not unicodedata.combining(ch)
    )


@login_required
def communications(request):
    # Scheduling posts here rather than navigating away, so a success returns
    # you to the list you were reading and a failure re-renders it with the
    # dialog still open and your answers still in it.
    schedule_form = None
    if request.method == 'POST' and request.POST.get('schedule'):
        schedule_form = scoped_meeting_form(request, request.POST)
        if save_scheduled_meeting(schedule_form):
            messages.success(request, _("Meeting scheduled."))
            return redirect(f"{request.path}?{request.GET.urlencode()}"
                            if request.GET else request.path)

    patients = Patient.objects.select_related('caregiver', 'navigator')
    if not is_admin(request.user):
        patients = patients.filter(navigator=request.user)

    # Narrow to one client, so the panel's way out lands on that person's
    # activity rather than on everybody's. Filtered from the already
    # permission-scoped queryset, so it cannot widen access.
    focus_patient = None
    raw_patient = request.GET.get('patient')
    if raw_patient:
        try:
            focus_patient = patients.filter(pk=int(raw_patient)).first()
        except (TypeError, ValueError):
            focus_patient = None
        if focus_patient:
            patients = patients.filter(pk=focus_patient.pk)

    protocol_numbers = dict(Protocol.objects.values_list('number', 'title'))
    events = []
    for p in patients:
        events.extend(build_patient_events(p, protocol_numbers=protocol_numbers))

    return render(request, 'communications/communications.html', {
        **comms_list_context(request, events),
        'focus_patient': focus_patient,
        'schedule_form': schedule_form or scoped_meeting_form(request),
        'active_page': 'communications',
    })


def comms_list_context(request, events, for_patient=None):
    """Everything templates/communications/_list.html needs, from raw events.

    Shared by the cross-client page and the Communications tab on a client's
    own page so the two lists cannot drift apart. Passing `for_patient` scopes
    it to that person: rows lead with what the entry is rather than whose it is,
    and the panel drops its "who is this" strip, because the page already
    answers both.
    """
    scoped = for_patient is not None
    now = timezone.now()
    today = timezone.localdate(now)

    # Only a pending meeting can be ahead of you; nothing else is schedulable.
    def is_upcoming(e):
        return e['kind'] == 'meeting' and e['status_code'] == Meeting.Status.PENDING

    upcoming = [e for e in events if is_upcoming(e)]
    happened = [e for e in events if not is_upcoming(e)]

    # Which half to open on. Following "Open in timeline" from an alert used to
    # land on Coming up — the one half that cannot contain an alert — so the
    # thing you asked to see was behind a tab you had to know to click. When the
    # URL names an item and no tab, the item chooses the tab.
    panel = panel_context(request, in_client_page_for=for_patient)
    open_token = (panel.get('panel_item') or {}).get('token')

    tab = request.GET.get('tab')
    if tab not in ('up', 'past'):
        tab = None
    if tab is None and open_token:
        tab = 'up' if any(e['panel_token'] == open_token for e in upcoming) else 'past'
    tab = tab or 'up'
    chips = UP_CHIPS if tab == 'up' else PAST_CHIPS
    kind = request.GET.get('kind', 'all')
    if kind not in dict(chips):
        kind = 'all'
    when = request.GET.get('when', 'all') if tab == 'past' else 'all'
    if when not in WHEN_DAYS:
        when = 'all'
    q = (request.GET.get('q') or '').strip()

    pool = upcoming if tab == 'up' else happened
    overdue = [e for e in upcoming if e['ts'] < now]

    if q:
        # Every word has to appear somewhere, rather than the whole query having
        # to appear inside one field. Matching the raw string against each field
        # separately meant "Elena" found her and "Marchetti" found her, but
        # "Elena Marchetti" — which is what anyone actually types — found
        # nothing, because no single field holds both words.
        # Accents are folded on both sides. Half the client base is called
        # María or Ramírez, and nobody types the accent into a search box.
        words = [_fold(w) for w in q.split()]

        def hit(e):
            p = e['patient']
            hay = _fold(' '.join([
                p.name or '', p.lastname or '', str(p.caregiver or ''),
                e.get('title') or '', e.get('protocol') or '',
                e.get('location') or '',
            ]))
            return all(w in hay for w in words)

        pool = [e for e in pool if hit(e)]

    def by_kind(events, value):
        if value == 'all':
            return events
        if value == 'late':
            return [e for e in events if e['ts'] < now]
        return [e for e in events if _kind_of(e) == value]

    def by_when(events, value):
        days = WHEN_DAYS.get(value)
        if not days:
            return events
        cut = now - dt.timedelta(days=days)
        return [e for e in events if e['ts'] >= cut]

    # Each group of chips counts what you would get if you clicked it, given
    # your other choices — so narrowing to Alerts updates the ranges, and
    # narrowing to last week updates the types. Counting each group against the
    # unfiltered half instead would leave "Alerts 49" showing beside six rows.
    in_range = by_when(pool, when)
    counts = {value: len(by_kind(in_range, value))
              for value, _label in chips}
    counts['late'] = len(by_kind(in_range, 'late'))

    of_kind = by_kind(pool, kind)
    when_counts = {value: len(by_when(of_kind, value))
                   for value, _label, _days in WHEN_CHIPS}

    shown = by_when(of_kind, when)

    # A queue counts forward; a record counts back.
    shown = sorted(shown, key=lambda e: e['ts'], reverse=(tab == 'past'))

    total = len(shown)
    pages = max(1, -(-total // PER_PAGE))
    try:
        # Zero means "not asked for", which is what lets the open item choose.
        page = max(0, int(request.GET.get('page', 0)))
    except (TypeError, ValueError):
        page = 0
    if not page and open_token:
        # And the page the item is actually on, for the same reason: landing on
        # the right half is no use if the entry is forty rows further down.
        for i, e in enumerate(shown):
            if e['panel_token'] == open_token:
                page = i // PER_PAGE + 1
                break
    page = min(max(page, 1), pages)
    start = (page - 1) * PER_PAGE
    window = shown[start:start + PER_PAGE]

    rows = [(_row(e, now), e) for e in window]

    # Which of the ones the system produced has this navigator not opened yet.
    # Asked only for the rows on screen, so the query stays a page wide however
    # long the history gets.
    marked = SeenMark.seen_tokens(
        request.user,
        [r['token'] for r, _e in rows if r['kind'] in ('alert', 'chat')],
    )
    for row, _e in rows:
        row['unread'] = row['kind'] in ('alert', 'chat') and row['token'] not in marked

    groups = _group_by_day(rows, today)

    def keep(**changes):
        """The current query with a few parameters changed.

        Chips keep the tab, search keeps the filter, and `item` survives all of
        them so the panel stays open while you narrow the list underneath it.
        """
        rest = request.GET.copy()
        for k, v in changes.items():
            rest.pop(k, None)
            if v is not None:
                rest[k] = v
        return rest.urlencode()

    # Unread counts for the tab badges, over each whole tab rather than the page
    # on screen. Only alerts and chats can be unread — a call you scheduled
    # yourself was never news — so the badge answers "is there anything here I
    # have not read", not "how many rows are there", which the list already
    # shows by being long.
    def unread_count(events):
        tokens = [e['panel_token'] for e in events
                  if _kind_of(e) in ('alert', 'chat') and e.get('panel_token')]
        if not tokens:
            return 0
        seen = SeenMark.seen_tokens(request.user, tokens)
        return sum(1 for t in tokens if t not in seen)

    return {
        **panel,
        'scoped': scoped,
        'up_unread': unread_count(upcoming),
        'past_unread': unread_count(happened),
        # Rendered onto the list so base.html can tell the panel endpoint which
        # client's page a row is being opened from — the fragment drops the
        # context strip for their own items, exactly as this view does.
        'client_pk': for_patient.pk if scoped else None,
        'tab': tab,
        'kind': kind,
        'q': q,
        'groups': groups,
        'chips': [(v, label, counts.get(v, 0)) for v, label in chips],
        'when': when,
        'when_chips': [(v, label, when_counts.get(v, 0)) for v, label, _d in WHEN_CHIPS],
        'qs_when': keep(when=None, page=None),
        'up_count': len(upcoming),
        'past_count': len(happened),
        'overdue_count': len(overdue),
        'total': total,
        'shown_count': len(window),
        'page': page,
        'pages': pages,
        'page_cells': _page_cells(page, pages),
        'first_shown': start + 1 if total else 0,
        'last_shown': start + len(window),
        'qs_page': keep(page=None),
        'qs_tab_up': keep(tab='up', kind=None, page=None),
        'qs_tab_past': keep(tab='past', kind=None, page=None),
        'qs_base': keep(kind=None, page=None),
    }
