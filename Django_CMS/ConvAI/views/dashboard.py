from collections import Counter

from django.utils.timesince import timesince

from ._base import *  # noqa: F401,F403
from ._panel import panel_context

__all__ = ['dashboard', 'hide_next_call']

def _humanise_age(when, now):
    """How long ago, in the unit a person would use out loud.

    Precise while it is fresh, human for a day or two, absolute once it is old
    enough that a duration stops meaning anything. The queue is worked by age,
    so a clock time is the one thing that never answers the question — it reads
    as today whatever day it came from.
    """
    if not when:
        return "", ""
    when = timezone.localtime(when)
    exact = when.strftime("%d/%m/%Y %H:%M")
    delta = now - when
    secs = delta.total_seconds()
    days = (now.date() - when.date()).days

    if secs < 3600:
        return _("Just now"), exact
    if days == 0:
        return _("%(h)dh ago") % {"h": int(secs // 3600)}, exact
    if days == 1:
        return _("Yesterday"), exact
    if days < 7:
        return _("%(d)d days ago") % {"d": days}, exact
    return when.strftime("%d %b"), exact


def _when_labels(when, now):
    """Day and hour for a scheduled call, written the way you would say it.

    "Today 18:28", "Tomorrow 09:30", "Fri 31 Jul 10:00". The weekday alone is
    ambiguous once a list crosses a month, and this one runs from July into
    August, so the month is always included beyond tomorrow.
    """
    when = timezone.localtime(when)
    delta = (when.date() - now.date()).days
    if delta == 0:
        day = _("Today")
    elif delta == 1:
        day = _("Tomorrow")
    elif delta == -1:
        day = _("Yesterday")
    else:
        day = formats.date_format(when, "D j M")
    return day, when.strftime("%H:%M")


def _lateness(when, now):
    """When it was due, or how soon it is.

    Deliberately not "4 days late". Late reads as a failing and is ambiguous
    about whose — usually it is nobody's: people are ill, or busy, or have said
    no. The due date sorts identically, carries the same information, and grades
    nobody for it. The red stays, because the fact still needs to be seen.
    """
    when = timezone.localtime(when)
    if when < now:
        return _("Due %(d)s") % {"d": formats.date_format(when, "D j M")}, True
    ahead = when - now
    hours = int(ahead.total_seconds() // 3600)
    if hours < 1:
        return _("in under an hour"), False
    if hours < 24:
        return _("in %(h)dh") % {"h": hours}, False
    return _("scheduled"), False


# How many rows fit on one page of a box. Both queues page the same way, so
# they stay the same shape as each other however long they get.
MEETINGS_PER_PAGE = 7
ALERTS_PER_PAGE = 7

# A ceiling on how many alert rows the page carries at once. The box pages
# client-side, so every row is in the document; without a limit a very old
# queue would put thousands there. Reaching it is surfaced rather than silent.
ALERTS_MAX = 200

# How the alert queue can be ordered. Priority first is the default because the
# list is worked top-down: newest-first buried High items below newer Lows.
ALERT_SORTS = {
    "priority": ("priority", "-created_at"),
    "newest": ("-created_at",),
    "oldest": ("created_at",),
}

# How far ahead counts as "coming up" for the card at the top of Home. A week
# covers the weekly protocol rhythm the platform is built around, so the card
# is quiet only when there is genuinely nothing near. Anything already overdue
# shows regardless of this — it is not coming up, it is late.
NEXT_CALL_HORIZON = timedelta(days=7)


def _mark_unread(request, items):
    """Flag the alert rows this navigator has not opened, and count them.

    Mutates the rows in place — they are plain dicts built a few lines above,
    and threading a parallel set through the template would only give the two
    a chance to disagree.
    """
    tokens = [i["panel_token"] for i in items]
    marked = SeenMark.seen_tokens(request.user, tokens)
    unread = 0
    for i in items:
        i["unread"] = i["panel_token"] not in marked
        if i["unread"]:
            unread += 1
    return {"alerts_unread": unread}


@login_required
def dashboard(request):
    user = request.user
    now = timezone.localtime()               # hora local
    today = now.date()

    # Meetings this user is responsible for. Staff see every meeting, matching
    # how alerts already behave below — without this an admin who schedules a
    # call for another navigator's client never sees it again on Home.
    user_meetings = (Meeting.objects
                     .select_related('patient', 'patient__caregiver')
                     # Every row on Home reads both, so they come along
                     # rather than costing two queries per meeting.
                     .prefetch_related('executed_protocols', 'scheduled_protocols'))
    if not is_admin(user):
        user_meetings = user_meetings.filter(patient__navigator=user)

    # Reuniones del día de hoy
    today_meetings = user_meetings.filter(scheduled_time__date=today)

    # De esas, cuántas están pendientes
    pending_meetings_today = today_meetings.filter(status=Meeting.Status.PENDING)
    pending_count = pending_meetings_today.count()

    # Every pending call, not just today's. The old fortnight window meant 46 of
    # 52 calls sat behind a link to another page; the box now carries the lot and
    # pages through them, which is also what makes searching it worth anything.
    carried_over_count = (
        user_meetings.filter(status=Meeting.Status.PENDING, scheduled_time__lt=now).count()
    )

    # Completadas de hoy
    completed_meetings_today = today_meetings.filter(~Q(status=Meeting.Status.PENDING)).order_by('scheduled_time')
    completed_count = completed_meetings_today.count()

    # Active alerts (not acted or resolved)
    alerts_qs = Alert.objects.exclude(status=Alert.AlertStatus.RESOLVED)
    if not is_admin(request.user):
        alerts_qs = alerts_qs.filter(Q(user=request.user) | Q(patient__navigator=request.user))

    alert_counts = {
        "all": alerts_qs.count(),
        "high": alerts_qs.filter(priority=Alert.Priority.HIGH).count(),
        "medium": alerts_qs.filter(priority=Alert.Priority.MEDIUM).count(),
        "low": alerts_qs.filter(priority=Alert.Priority.LOW).count(),
    }

    # Find a client by name. One box beats four filter chips: priority is on
    # every row already, so narrowing by it answered a question you can see.
    query = (request.GET.get("q") or "").strip()
    shown_alerts_qs = alerts_qs
    if query:
        shown_alerts_qs = shown_alerts_qs.filter(
            Q(patient__name__icontains=query)
            | Q(patient__lastname__icontains=query)
            | Q(title__icontains=query)
        )

    sort = request.GET.get("sort", "priority")
    if sort not in ALERT_SORTS:
        sort = "priority"
    shown_alerts_qs = shown_alerts_qs.order_by(*ALERT_SORTS[sort])

    # What the high-priority items are actually about. A count tells you how
    # much is waiting; this tells you what kind of problem it is, which is the
    # one thing the badge on the box cannot say. Grouped by title so four
    # alerts about the same issue read as one theme, not four separate jobs.
    high_topics = [
        {"title": title, "count": count}
        for title, count in Counter(
            alerts_qs.filter(priority=Alert.Priority.HIGH)
            .values_list("title", flat=True)
        ).most_common(3)
    ]

    # The next call still to come — the header's whole job is to answer "what
    # now", so it names the call and carries the button rather than describing
    # the page to somebody already looking at it.
    # The call to make now, which is the oldest overdue one when any are
    # overdue and only otherwise the soonest upcoming. Excluding the past would
    # point the card at Tuesday while six calls sit late in the list below it.
    # Same order as the pending box, so the card and its first row agree.
    next_meeting = (
        user_meetings
        .filter(status=Meeting.Status.PENDING)
        .order_by("scheduled_time")
        .first()
    )

    # Three things can silence the card, and they are deliberately different.
    prefs = request.user.dashboard_prefs if isinstance(request.user.dashboard_prefs, dict) else {}

    # 1. Switched off for good, in Profile. Nothing brings it back but the
    #    same switch.
    if prefs.get("next_call_off"):
        next_meeting = None

    # 2. Nothing is coming up. A call three weeks out is not "what now", and a
    #    card that is always there stops being read. Overdue always qualifies —
    #    that one is not coming up, it is already late.
    elif next_meeting and next_meeting.scheduled_time > now + NEXT_CALL_HORIZON:
        next_meeting = None

    # 3. Put away by hand. Keyed to the call rather than to the card, so it
    #    clears today's clutter without hiding tomorrow's: once this one is
    #    made, moved or finished, another becomes next and the card returns on
    #    its own. Stored on the user, so putting it away on a laptop keeps it
    #    away on a phone.
    elif next_meeting and prefs.get("next_call_hidden") == next_meeting.panel_token:
        next_meeting = None

    hour = now.hour
    greeting = (_("Good morning") if hour < 12
                else _("Good afternoon") if hour < 18
                else _("Good evening"))
    first_name = (user.get_short_name() or user.get_full_name() or user.username).split(" ")[0]

    # How many questions each protocol carries, for the "3 of 8 recorded" line.
    proto_questions = dict(
        Protocol.objects.annotate(n=Count("questions")).values_list("pk", "n")
    )

    def _covered(meeting):
        """The protocols this call covered, or failing that what it is booked for."""
        return (list(meeting.executed_protocols.all())
                or list(meeting.scheduled_protocols.all()))

    def _protocol_label(meeting):
        covered = _covered(meeting)
        if covered:
            return ", ".join(f"{p.number}. {p.title}" for p in covered)
        # A call with no protocol against it is ordinary — a check-in, a
        # conversation that went elsewhere. The row says what kind of call it
        # is rather than leaving the column blank.
        return meeting.get_type_display()

    def _meeting_row(m, answered=None):
        day, hour = _when_labels(m.scheduled_time, now)
        late_text, is_late = _lateness(m.scheduled_time, now)
        caregiver = m.patient.caregiver
        row = {
            "meeting": m,
            "name": f"{m.patient.name} {m.patient.lastname}",
            "protocol": _protocol_label(m),
            "day": day,
            "hour": hour,
            "late_text": late_text,
            "is_late": is_late,
            "panel_token": m.panel_token,
            "with_": f"{caregiver.name} {caregiver.lastname}" if caregiver else _("No caregiver"),
            "status": m.get_status_display(),
            "status_code": m.status,
            # A call and a visit are different jobs, so the row says which —
            # same vocabulary the Communications page filters on.
            "kind": "visit" if m.modality == Meeting.Modality.IN_PERSON else "call",
            "in_person": m.modality == Meeting.Modality.IN_PERSON,
            "location": m.location,
        }
        if answered is not None:
            # Whether the call produced anything is the useful half of "complete".
            # Summed across every protocol the call covered, since it can cover
            # more than one.
            total = sum(proto_questions.get(p.pk, 0) for p in _covered(m))
            row["recorded"] = (
                _("nothing recorded") if not answered
                else _("%(a)d of %(t)d recorded") % {"a": answered, "t": total}
            )
        return row

    # Every pending call, rendered in full. Paging, searching and sorting all
    # happen in the page: the list is small enough to hold, and a round trip
    # would close the detail panel and lose your scroll position for something
    # that is only a view of rows already fetched. The form below still posts
    # for anyone without JavaScript, and this view honours it.
    pending_qs = user_meetings.filter(status=Meeting.Status.PENDING)
    mq = (request.GET.get("mq") or "").strip()
    if mq:
        pending_qs = pending_qs.filter(
            Q(patient__name__icontains=mq) | Q(patient__lastname__icontains=mq)
        )
    msort = request.GET.get("msort", "soon")
    pending_qs = pending_qs.order_by(
        "-scheduled_time" if msort == "late" else "scheduled_time"
    )
    call_meetings = [_meeting_row(m) for m in pending_qs]
    pending_total = len(call_meetings)

    next_row = _meeting_row(next_meeting) if next_meeting else None

    # Completed today, with how much each call actually captured.
    done_rows = []
    for m in completed_meetings_today.annotate(n_answers=Count("answers")):
        done_rows.append(_meeting_row(m, answered=m.n_answers))

    def _alert_row(a):
        age, exact = _humanise_age(a.created_at, now)
        # Keep whatever the reviewer has narrowed or sorted to when a panel opens.
        keep = {k: v for k, v in (("q", query), ("sort", sort if sort != "priority" else "")) if v}
        return {
            "id": a.id,
            # A classifier alert's title is the detector label, stored in
            # English so it keeps matching itself. Read in the viewer's own.
            "title": display_label(a.title) or "(sin título)",
            "patient": f"{a.patient.name} {a.patient.lastname}" if a.patient else "",
            "description": a.description,
            "priority": a.get_priority_display(),
            "priority_level": a.priority,
            "status": a.get_status_display(),
            "status_code": a.status,
            # Age, not a clock time: the queue is worked by how long something
            # has waited, and a bare time reads as today whatever day it is.
            "age": age,
            "age_exact": exact,
            "created_ts": a.created_at,
            # Opens the panel in place rather than navigating away, so a queue of
            # alerts can be cleared without leaving Home.
            "url": f"?{urlencode({**keep, 'item': f'alert-{a.id}'})}",
            # Lets the row mark itself when its panel is the one open.
            "panel_token": f"alert-{a.id}",
        }

    # What changed for someone, because of this work. Home otherwise ends with
    # what is still owed, on every screen, all day — and the evidence that any
    # of it helped is nowhere. Deliberately not counted, ranked or averaged:
    # the moment it becomes a metric it stops being the thing it is for.
    digest = []
    for a in (Alert.objects
              .filter(status=Alert.AlertStatus.RESOLVED,
                      updated_at__gte=now - timedelta(days=7))
              .select_related("patient")
              .order_by("-updated_at")[:40]):
        data = a.data if isinstance(a.data, dict) else {}
        line = (data.get("outcome") or "").strip()
        if not line:
            continue
        if not (is_admin(user) or (a.patient and a.patient.navigator_id == user.id)
                or a.user_id == user.id):
            continue
        digest.append({"who": str(a.patient) if a.patient else "", "line": line})
        if len(digest) == 4:
            break

    alerts = [_alert_row(a) for a in
              shown_alerts_qs.select_related("patient")[:ALERTS_MAX]]
    alerts_capped = len(alerts) == ALERTS_MAX



    ### Message trends ###

    # --- Message trends (today, 4h buckets) ---
    local_now = timezone.localtime()
    start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    msg_qs = Message.objects.filter(timestamp__gte=start_of_day, timestamp__lt=end_of_day)

    # Restrict to this CTN's patients if not staff. Match by phone (WhatsApp)
    # OR by patient-linked Conversation (tester/voice chat store a username).
    if not is_admin(request.user):
        phones = set()
        for p in Patient.objects.filter(navigator=user).select_related("caregiver"):
            if p.phone_number:
                phones.add(str(p.phone_number))
            if p.caregiver and p.caregiver.phone_number:
                phones.add(str(p.caregiver.phone_number))
        conv_ids = [
            str(cid) for cid in
            Conversation.objects.filter(patient__navigator=user).values_list("id", flat=True)
        ]
        scope_q = Q(pk__in=[])
        if phones:
            scope_q |= Q(user__in=list(phones))
        if conv_ids:
            scope_q |= Q(conversation_id__in=conv_ids)
        msg_qs = msg_qs.filter(scope_q)

    total_msgs_today = msg_qs.count()

    bucket_hours = 4
    buckets = []
    max_avg = 0.0
    for i in range(0, 24, bucket_hours):
        b_start = start_of_day + timedelta(hours=i)
        b_end = b_start + timedelta(hours=bucket_hours)
        cnt = msg_qs.filter(timestamp__gte=b_start, timestamp__lt=b_end).count()
        avg_per_hour = cnt / bucket_hours
        max_avg = max(max_avg, avg_per_hour)
        buckets.append({
            "start": b_start,
            "end": b_end,
            "count": cnt,
            "avg": avg_per_hour,
        })

    # Normalize to percentages for bar heights, mark peak
    peak_idx = 0
    if max_avg > 0:
        for idx, b in enumerate(buckets):
            if b["avg"] == max_avg and peak_idx == 0:
                peak_idx = idx

    for idx, b in enumerate(buckets):
        b["height_pct"] = int(round((b["avg"] / max_avg) * 95)) if max_avg > 0 else 0
        b["label"] = f"{int(round(b['avg']))}/h"
        b["is_peak"] = (idx == peak_idx)

    peak_window = "—"
    if buckets:
        bs, be = buckets[peak_idx]["start"], buckets[peak_idx]["end"]
        peak_window = f"{bs.strftime('%H:%M')} - {be.strftime('%H:%M')}"

    ### Message trends ###

    return render(request, 'dashboard/dashboard.html', {
        **panel_context(request),
        # Which alerts this navigator has not opened yet. Asked after
        # panel_context, so opening one clears it on the same render.
        **_mark_unread(request, alerts),
        'pending_count': pending_count,
        'completed_count': completed_count,
        'now': now,
        'completed_meetings_today': done_rows,
        'call_meetings': call_meetings,
        'meetings_total': pending_total,
        'meetings_per_page': MEETINGS_PER_PAGE,
        'mq': mq,
        'msort': msort,
        'alerts': alerts,
        'digest': digest,
        'alerts_capped': alerts_capped,
        'alerts_per_page': ALERTS_PER_PAGE,
        'alert_counts': alert_counts,
        'high_topics': high_topics,
        'next_meeting': next_row,
        'greeting': greeting,
        # The date under the greeting. Formatted here rather than in the
        # template so it follows the active locale like every other date on the
        # page, instead of an English format baked into the markup.
        'today_label': formats.date_format(now.date(), "l, j F Y"),
        'first_name': first_name,
        'query': query,
        'sort': sort,
        'carried_over_count': carried_over_count,
        'todo_count': pending_total,
        'active_page' : 'dashboard',
        ### message trends ###
        "msg_buckets": buckets,
        "msg_total_today": total_msgs_today,
        "msg_peak_window": peak_window,
        ### end of message trends ###
    })




@login_required
@require_POST
def hide_next_call(request):
    """Put the next-call card away, or switch it off for good.

    Two different intentions, so two different stores. Dismissing one call
    records that call's token — once it is made, moved or finished, another
    becomes next and the card comes back by itself. Switching the card off is a
    standing preference and only the same switch reverses it.

    Both live on the user rather than in localStorage, so a card put away on a
    laptop stays away on a phone.
    """
    prefs = request.user.dashboard_prefs if isinstance(request.user.dashboard_prefs, dict) else {}

    off = request.POST.get("off")
    if off == "1":
        prefs["next_call_off"] = True
        prefs.pop("next_call_hidden", None)
    elif off == "0":
        prefs["next_call_off"] = False
        prefs.pop("next_call_hidden", None)
    else:
        token = (request.POST.get("token") or "").strip()[:64]
        if not token:
            return JsonResponse({"status": "error"}, status=400)
        prefs["next_call_hidden"] = token

    request.user.dashboard_prefs = prefs
    request.user.save(update_fields=["dashboard_prefs"])

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"status": "ok", "off": bool(prefs.get("next_call_off"))})
    return redirect(request.POST.get("next") or "dashboard")
