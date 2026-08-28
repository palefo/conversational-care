from ._base import *  # noqa: F401,F403
import logging

from django.db import transaction

from ._panel import panel_context

logger = logging.getLogger(__name__)

__all__ = ['scoped_meeting_form', 'save_scheduled_meeting', 'calendar_view', 'calendar_create_meeting', 'complete_meeting', 'cancel_meeting', 'edit_meeting', 'pending_call', 'make_phone_call', 'start_client_call', 'schedule_call', 'send_whatsapp_reminder_view', 'send_meeting_reminder_view']


@login_required
def pending_call(request, call_id):
    """Retired. The call now opens in the detail panel, wherever you are.

    This was the separate working page for a call. Everything it did — the
    protocols, the answers, the notes, the reminder, the outcome — is in the
    panel now, and reaching it from the calendar meant leaving the calendar.

    Kept as a redirect rather than deleted so old links, bookmarks and the
    `_back_to` fallbacks below still land somewhere sensible instead of 404ing.
    Ownership is checked before redirecting: without it this would say whether a
    meeting exists to anyone who guessed an id.
    """
    meeting = get_object_or_404(
        Meeting.objects.select_related('patient__navigator'), pk=call_id
    )
    # R3-03 fix: enforce ownership (was an unguarded cross-tenant read).
    if not (is_admin(request.user)
            or (meeting.patient and meeting.patient.navigator_id == request.user.id)):
        return HttpResponseForbidden(_("Not authorised."))

    return redirect(
        f"{reverse('communications')}?item={meeting.panel_token}"
    )


@login_required
def scoped_meeting_form(request, data=None):
    """A MeetingForm that can only schedule for clients you are allowed to see.

    Shared by the standalone page and the dialog on Communications so the two
    cannot drift apart on who may be scheduled or what counts as a clash.
    """
    form = MeetingForm(data) if data is not None else MeetingForm()
    if not is_admin(request.user):
        form.fields['patient'].queryset = Patient.objects.filter(navigator=request.user)
    return form


def save_scheduled_meeting(form):
    """Save the meeting, or mark the clash on the form. Returns the meeting or None."""
    if not form.is_valid():
        return None

    meeting = form.save(commit=False)
    mt = meeting.scheduled_time

    # Clash detection runs against whoever owns the client, not against whoever
    # is filling in the form — an admin scheduling on someone else's behalf must
    # not be told their own diary is free.
    clash = Meeting.objects.filter(
        patient__navigator=meeting.patient.navigator,
        scheduled_time__gte=mt - timedelta(minutes=19),
        scheduled_time__lt=mt + timedelta(minutes=19),
    ).exists()
    if clash:
        form.add_error('scheduled_time',
                       _("There is already a meeting scheduled within this time range."))
        return None

    meeting.save()
    # save(commit=False) defers the m2m, so without this the protocols picked in
    # the dialog never reach the meeting.
    form.save_m2m()
    return meeting


def schedule_call(request):
    """Retired as a page. Scheduling is the dialog on Communications.

    The dialog and this page rendered the same MeetingForm, which meant two
    surfaces to keep in step for one job. A GET now opens the dialog instead;
    a POST is still honoured so nothing that was already posting here breaks.
    """
    if request.method == 'POST':
        form = scoped_meeting_form(request, request.POST)
        if save_scheduled_meeting(form):
            messages.success(request, _("Meeting scheduled."))
            return redirect('communications')
        for field, errors in form.errors.items():
            for err in errors:
                messages.error(request, err)

    return redirect('communications')


def _call_error(message, status, fix_href=None, fix_label=None):
    """A refusal the panel can actually render.

    These used to be bare HttpResponseForbidden/BadRequest bodies, and the
    panel discarded every one of them in favour of "Could not start the call."
    Three of the four reasons below are things the navigator can fix in under a
    minute — but only if they are told which one it is, and where to go.
    """
    payload = {'error': message}
    if fix_href:
        payload['fix'] = {'href': fix_href, 'label': fix_label}
    return JsonResponse(payload, status=status)


def _record_call_legs(meeting, conference, user):
    """Write down which Twilio call is which side of this meeting's conference.

    This is the only moment the answer is free. Twilio names each leg here,
    with the meeting and the client already in hand; the recording turns up
    later carrying nothing but that name and two phone numbers, and a phone
    number cannot say which client it is — or whether it is a client at all,
    the navigator's own leg being a staff number. See CallLeg.

    Deliberately after the conference has been placed, and deliberately
    swallowed. By the time this runs both phones are already ringing, and
    failing to write down what the call was must not turn a placed call into an
    error on the navigator's screen. What is lost when it fails is the exact
    link, and the recording falls back to being matched by number — which is
    where every recording was before this.
    """
    legs = (conference or {}).get('legs') or []
    rows = [
        CallLeg(
            call_sid=leg.get('call_sid'),
            meeting=meeting,
            patient=meeting.patient,
            leg=leg.get('leg'),
            to_number=str(leg.get('to_number') or ''),
            conference_name=str((conference or {}).get('conference') or '')[:64],
            placed_by=user if getattr(user, 'pk', None) else None,
        )
        for leg in legs if leg.get('call_sid')
    ]
    if not rows:
        return
    try:
        # The savepoint is what makes swallowing this safe. Without it, a
        # database error caught here would leave the surrounding transaction
        # unusable — should ATOMIC_REQUESTS ever be switched on — and the retry
        # counter below would fail next, turning a bookkeeping miss into the
        # failed call this is written to avoid.
        #
        # ignore_conflicts because call_sid is unique: a SID already written
        # down is the same leg, not a second one.
        with transaction.atomic():
            CallLeg.objects.bulk_create(rows, ignore_conflicts=True)
    except Exception:
        logger.exception(
            "Call placed for meeting %s but its legs could not be recorded; "
            "its recordings will fall back to number matching.", meeting.pk,
        )


def _place_call(request, meeting):
    """Bridge the navigator's phone to whoever this meeting is with.

    Split out of make_phone_call so the Call button on the client page places
    its call the same way a booked one is placed, rather than growing a second
    copy of the Twilio round trip, the leg bookkeeping and the four refusals.
    The only difference between the two entry points is where the meeting came
    from; everything from here down is identical, and has to stay identical —
    an unscheduled call that skipped _record_call_legs would lose its recording.
    """
    recipient = meeting.dial_recipient
    if recipient is None:
        # Named, rather than "no caregiver": with two people to choose between,
        # the reason has to say which of them was chosen and came up short, or
        # the navigator goes looking at the wrong record.
        missing = (_("This client has no phone number of their own to call.")
                   if meeting.dial_target == Meeting.DialTarget.CLIENT
                   else _("This client has no caregiver with a phone number to call."))
        return _call_error(
            missing, 400,
            reverse('patient_detail', args=[meeting.patient_id]), _("Open the client"),
        )

    # The conference bridges the navigator's own phone too, so a missing number
    # here used to reach Twilio as the string "None" and fail obscurely.
    if not request.user.phone_number:
        return _call_error(
            _("Your own phone number is not set, so the call cannot be placed."), 400,
            reverse('profile'), _("Add your number"),
        )

    # Convertir a string para Twilio
    dyad_phone = str(recipient.phone_number)
    ctn_phone  = str(request.user.phone_number)
    platform_phone = get_platform_phone()

    try:
        # Llamada al helper que inicia la conferencia
        conference = make_phone_conference({
            'CTN': ctn_phone,
            'Dyad': dyad_phone,
            'Platform': platform_phone
        })
    except Exception:
        # The exception text used to be handed to the browser verbatim. Twilio
        # errors carry account SIDs and endpoint detail, none of which belongs
        # on a navigator's screen and none of which they could act on — so it
        # goes to the log, and the panel gets a sentence instead.
        logger.exception("Failed to start conference for meeting %s", meeting.pk)
        return _call_error(
            _("The phone system did not accept the call. Please try again."), 502,
        )

    _record_call_legs(meeting, conference, request.user)

    # Incrementar retries en 1
    Meeting.objects.filter(pk=meeting.pk).update(retries=F('retries') + 1)

    # The conference name only. The Call SIDs the helper also returns stay on
    # this side: they are Twilio's identifiers for the call, the browser has no
    # use for them, and the panel already deliberately keeps Twilio detail off
    # the navigator's screen — see _call_error.
    return JsonResponse({
        'status': 'ok',
        'conference': (conference or {}).get('conference')
    })


@login_required
def make_phone_call(request, meeting_id):
    """
    Inicia una conferencia telefónica para la reunión indicada.
    """
    meeting = get_object_or_404(
        Meeting.objects.select_related('patient__caregiver', 'patient__navigator'),
        pk=meeting_id
    )

    if not (is_admin(request.user) or meeting.patient.navigator_id == request.user.id):
        return _call_error(_("You do not have permission to start this call."), 403)

    return _place_call(request, meeting)


@require_POST
@login_required
def start_client_call(request, patient_id):
    """Call this client now, without anything having been booked.

    The Call button on the client page. It rings whichever of the client's two
    numbers was picked — see Meeting.DialTarget — and the call it places is an
    ordinary Meeting, created here and marked unscheduled.

    A Meeting rather than a bare Twilio call, because a call outside one is a
    call the platform cannot hold: CallLeg hangs off a meeting and is what ties
    the recording arriving hours later back to this client, and the panel's
    protocols, notes and outcome are all addressed to one. Placing this call
    without a meeting would mean a call that is recorded nowhere, answered
    nowhere, and closed nowhere.

    Deliberately never reuses a booked call, even one due in ten minutes.
    Folding an unscheduled call into a scheduled one would silently record the
    booked call as made — and if this was a different conversation, that is a
    call that now looks done and will not be made.
    """
    patient = get_object_or_404(
        Patient.objects.select_related('caregiver', 'navigator'), pk=patient_id
    )
    if not (is_admin(request.user) or patient.navigator_id == request.user.id):
        return _call_error(_("You do not have permission to call this client."), 403)

    target = (Meeting.DialTarget.CLIENT
              if (request.POST.get('to') or '').strip() == 'client'
              else Meeting.DialTarget.CAREGIVER)

    meeting = Meeting.objects.create(
        patient=patient,
        # Now, because that is when it is happening. ended_at is what dates it
        # in Happened once the outcome is recorded; until then this is what the
        # lists sort it by, and a call placed at 15:40 belongs at 15:40.
        scheduled_time=timezone.now(),
        modality=Meeting.Modality.PHONE,
        status=Meeting.Status.PENDING,
        dial_target=target,
        unscheduled=True,
    )

    response = _place_call(request, meeting)

    if response.status_code != 200:
        # Nothing was placed, so there was no call — and a meeting left behind
        # here would be one: a row on the timeline, in the navigator's queue,
        # asking for an outcome nobody owes it. It only earns its place once
        # the phones are actually ringing.
        meeting.delete()
        return response

    # Where to carry on. The client page opens this meeting's panel rather than
    # navigating anywhere new, so the call arrives with its protocols, its notes
    # and its outcome already around it — the same panel a booked call is worked
    # through in.
    payload = {'status': 'ok', 'item': meeting.panel_token}
    recipient = meeting.dial_recipient
    if recipient is not None:
        payload['who'] = str(recipient)
    return JsonResponse(payload)


@require_POST
@login_required
def complete_meeting(request, meeting_id):
    if request.method != 'POST':
        return redirect('dashboard')

    if is_admin(request.user):
        qs = Meeting.objects.select_related("patient", "patient__navigator")
    else:
        qs = Meeting.objects.select_related("patient", "patient__navigator") \
                            .filter(patient__navigator=request.user)

    meeting = get_object_or_404(qs, pk=meeting_id)

    # Leer el nuevo estado
    try:
        new_status = int(request.POST.get('status', ''))
    except (ValueError, TypeError):
        messages.error(request, _("Invalid status."))
        return _back_to(request, 'pending_call', call_id=meeting_id)

    meeting.status = new_status

    # When it actually happened, as opposed to when it was booked. Stamped on
    # the first outcome only: pressing Change to correct a mis-click is fixing
    # the record of a call, not moving when the call took place.
    if new_status != Meeting.Status.PENDING and meeting.ended_at is None:
        meeting.ended_at = timezone.now()

    meeting.save()

    # What the call actually covered. A call can work through more than one
    # protocol, so this is a set rather than a single number, and it is only
    # recorded on a call that happened — an unanswered call covered nothing.
    #
    # Nothing is rejected for being empty any more: a completed call with no
    # protocol against it is an ordinary thing (a check-in, a conversation that
    # went elsewhere), and refusing to record the outcome over it meant the
    # outcome went unrecorded instead.
    if new_status == Meeting.Status.COMPLETED:
        ids = []
        for raw in request.POST.getlist('executed_protocols'):
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        meeting.executed_protocols.set(Protocol.objects.filter(pk__in=ids))
    else:
        meeting.executed_protocols.clear()
    messages.success(request, _("Meeting status updated."))
    return _back_to(request, 'dashboard')


def _back_to(request, fallback, **kwargs):
    """Return to the page an action was taken from.

    The detail panel posts a `next` pointing at whatever page it was open on,
    so acting on a call from the dashboard leaves you on the dashboard. Only
    same-site paths are honoured — mirrors _after_action in views/alerts.py.
    """
    nxt = (request.POST.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(fallback, **kwargs)


@login_required
def calendar_view(request):
    # View mode: month (default), week or day.
    view = request.GET.get('view', 'month')
    if view not in ('month', 'week', 'day'):
        view = 'month'

    # Offset navigates in units of the current view; clamp per view.
    _max = {'month': 3, 'week': 26, 'day': 90}[view]
    try:
        offset = int(request.GET.get('offset', 0))
    except ValueError:
        offset = 0
    offset = max(-_max, min(_max, offset))

    tz = timezone.get_current_timezone()
    today_local = timezone.localtime().date()

    def _aware(d):
        return timezone.make_aware(datetime(d.year, d.month, d.day, 0, 0, 0), tz)

    def _meetings_between(start_date, end_date):
        """Return meetings for this navigator in [start_date, end_date), grouped
        by local date, each list sorted by local time."""
        qs = Meeting.objects.filter(
            scheduled_time__gte=_aware(start_date),
            scheduled_time__lt=_aware(end_date),
        ).select_related('patient')
        # Staff see every meeting; a navigator sees only their own clients'.
        # Without the staff case, an admin who schedules a call for another
        # navigator's client can never find it again on Home or the calendar.
        if not is_admin(request.user):
            qs = qs.filter(patient__navigator=request.user)
        by_date = {}
        for m in qs:
            local_date = timezone.localtime(m.scheduled_time, tz).date()
            by_date.setdefault(local_date, []).append(m)
        for lst in by_date.values():
            lst.sort(key=lambda mm: timezone.localtime(mm.scheduled_time, tz))
        return by_date

    context = {
        'active_page': 'calendar',
        'view': view,
        'offset': offset,
        'offset_max': _max,
        'at_min': offset <= -_max,
        'at_max': offset >= _max,
        'view_options': [('month', _('Month')), ('week', _('Week')), ('day', _('Day'))],
    }

    # Every view, not only the ones with clickable slots. The Schedule button in
    # the header opens the same modal from the month as from the week, and a
    # month rendering it without a form gave a New meeting dialog with nothing
    # in it but Cancel and Schedule.
    meeting_form = MeetingForm()
    if not is_admin(request.user):
        meeting_form.fields['patient'].queryset = Patient.objects.filter(navigator=request.user)
    context['meeting_form'] = meeting_form
    # Which protocols each client is on, so the dialog can float the chosen
    # client's own to the top the moment they are chosen. Scoped to the clients
    # the form can pick, which is already scoped to who the user may see.
    context['protocol_owners'] = meeting_form.protocol_owners()

    if view == 'month':
        first_of_month = today_local.replace(day=1) + relativedelta(months=offset)
        year, month = first_of_month.year, first_of_month.month
        cal = calendar.Calendar(firstweekday=0)  # Monday = 0
        month_days = cal.monthdayscalendar(year, month)
        next_month = first_of_month + relativedelta(months=1)
        by_date = _meetings_between(first_of_month, next_month.replace(day=1))
        meetings_by_day = {d.day: lst for d, lst in by_date.items()}
        context.update({
            'period_label': f'{formats.date_format(first_of_month, "F")} {year}',
            'month_days': month_days,
            'meetings_by_day': meetings_by_day,
            'today_day': today_local.day if offset == 0 else None,
        })
    else:
        # Time grid in half-hour blocks over the visible day window.
        DAY_START, DAY_END = 7, 21
        if view == 'week':
            monday = today_local - timedelta(days=today_local.weekday())
            start_date = monday + timedelta(weeks=offset)
            span = 7
        else:  # day
            start_date = today_local + timedelta(days=offset)
            span = 1
        end_date = start_date + timedelta(days=span)
        by_date = _meetings_between(start_date, end_date)

        days = [start_date + timedelta(days=i) for i in range(span)]
        day_headers = [{'date': d, 'is_today': d == today_local} for d in days]

        time_slots = []
        h, mnt = DAY_START, 0
        while h < DAY_END:
            time_slots.append({'hour': h, 'minute': mnt, 'label': f'{h:02d}:{mnt:02d}'})
            mnt += 30
            if mnt == 60:
                mnt, h = 0, h + 1
        n_slots = len(time_slots)

        def _slot_index(m):
            lt = timezone.localtime(m.scheduled_time, tz)
            idx = (lt.hour - DAY_START) * 2 + (1 if lt.minute >= 30 else 0)
            return max(0, min(n_slots - 1, idx))

        grid_rows = []
        for si, slot in enumerate(time_slots):
            cells = []
            for d in days:
                cells.append({
                    'datetime': f"{d:%Y-%m-%d}T{slot['hour']:02d}:{slot['minute']:02d}",
                    'is_today': d == today_local,
                    'meetings': [m for m in by_date.get(d, []) if _slot_index(m) == si],
                })
            grid_rows.append({'label': slot['label'], 'cells': cells})

        if view == 'week':
            last = start_date + timedelta(days=6)
            label = f'{formats.date_format(start_date, "d M")} – {formats.date_format(last, "d M Y")}'
        else:
            label = formats.date_format(start_date, "l, d F Y")
        context.update({
            'period_label': label,
            'day_headers': day_headers,
            'grid_rows': grid_rows,
            'span': span,
        })

    # The panel is meant to survive moving around the platform, and the
    # calendar is exactly where you go mid-triage to find a slot — closing it
    # on arrival loses the thing you were scheduling around.
    panel = panel_context(request)
    context.update(panel)

    # Everything except `item`, so clicking an event opens it in place without
    # throwing you back to this month in the default view. Ends in `&` (or is
    # empty) so the template can append `item=` to it directly.
    rest = request.GET.copy()
    rest.pop('item', None)
    encoded = rest.urlencode()
    context['cal_qs'] = f"{encoded}&" if encoded else ""

    # The opposite, for the view switcher, Today and the arrows. Those are
    # third-layer moves — they change what the calendar is showing, not what you
    # are looking at — so the open panel goes with them. They build their
    # querystring from scratch rather than from `keep()`, so `item` has to be
    # handed to them explicitly or it is simply dropped, which is what used to
    # close the panel on every change of week.
    open_token = (panel.get('panel_item') or {}).get('token')
    context['cal_item_qs'] = f"&item={quote_plus(open_token)}" if open_token else ""

    return render(request, 'calls/calendar.html', context)


@login_required
@require_POST
def calendar_create_meeting(request):
    """Create a meeting from the week/day calendar's click-to-schedule modal."""
    form = MeetingForm(request.POST)
    if not is_admin(request.user):
        form.fields['patient'].queryset = Patient.objects.filter(navigator=request.user)

    view = request.POST.get('view', 'week')
    try:
        offset = int(request.POST.get('offset', 0))
    except ValueError:
        offset = 0
    back = f"{reverse('calendar')}?view={view}&offset={offset}"

    if form.is_valid():
        meeting = form.save(commit=False)
        meeting.navigator = request.user
        mt = meeting.scheduled_time
        conflict = Meeting.objects.filter(
            patient__navigator=request.user,
            scheduled_time__gte=mt - timedelta(minutes=19),
            scheduled_time__lt=mt + timedelta(minutes=19),
        ).exists()
        if conflict:
            messages.error(request, _("You already have a meeting scheduled within this time range."))
        else:
            meeting.save()
            # save(commit=False) defers the m2m write; without this the
            # protocols picked in the dialog are silently dropped.
            form.save_m2m()
            messages.success(request, _("Meeting scheduled."))
    else:
        messages.error(request, _("Please check the meeting details and try again."))
    return redirect(back)


@login_required
def edit_meeting(request, meeting_id):
    """
    Reprograma la hora de una reunión existente (solo si está pendiente).
    """
    # Cargar la reunión permitiendo acceso a staff o al CTN asignado
    if is_admin(request.user):
        meeting = get_object_or_404(
            Meeting.objects.select_related('patient__caregiver'),
            pk=meeting_id
        )
    else:
        meeting = get_object_or_404(
            Meeting.objects.select_related('patient__caregiver'),
            pk=meeting_id,
            patient__navigator=request.user
        )

    # Solo permitir reprogramar si está PENDIENTE
    if meeting.status != Meeting.Status.PENDING:
        messages.error(request, _("Only pending meetings can be rescheduled"))
        return redirect('calendar')

    # Posted from the dialog in the detail panel. Editing a meeting is a small
    # form about one thing, and sending someone to a page of their own for it
    # lost the list, the calendar and the panel they were working in.
    if request.method == 'POST':
        form = MeetingForm(request.POST, instance=meeting)
        # Si no es staff, limitar pacientes al CTN (igual que en schedule_call)
        if not is_admin(request.user):
            form.fields['patient'].queryset = Patient.objects.filter(navigator=request.user)

        if form.is_valid():
            meeting = form.save(commit=False)
            meeting.navigator = request.user
            meeting.save()
            form.save_m2m()
            messages.success(request, _("Meeting rescheduled successfully."))
        else:
            # The dialog is gone by the time this lands, so the errors have to
            # arrive as messages or they arrive nowhere.
            for field, errors in form.errors.items():
                for err in errors:
                    messages.error(request, err)
        return _back_to(request, 'calendar')

    # The page this used to render is retired. A GET lands here from an old
    # link or a bookmark; send it to the panel, which is where the dialog is.
    return redirect(f"{reverse('communications')}?item={meeting.panel_token}")


@login_required
def send_meeting_reminder_view(request, meeting_id):
    """Remind the caregiver about a meeting, over whichever channel is configured.

    Only the client's navigator or an admin may. The channel — WhatsApp or
    email — is chosen in Settings → Messaging; this view does not care which,
    it only reports which one carried it.
    """
    meeting = get_object_or_404(
        Meeting.objects.select_related('patient__caregiver'),
        pk=meeting_id
    )

    # Permisos: debe ser el CTN (navigator) o staff
    if not (is_admin(request.user) or meeting.patient.navigator == request.user):
        return HttpResponseForbidden(_("You do not have permission to send reminders."))

    try:
        channel = send_meeting_reminder(meeting)
    except ValueError as e:
        # Nothing to send to — a missing phone number or email address. Worth
        # saying so, because it is fixed on the client's record, not by retrying.
        logger.warning("Could not send reminder for meeting %s: %s", meeting_id, e)
        messages.error(request, _(
            "The reminder could not be sent: there is no contact address for the "
            "configured reminder channel."
        ))
        return _back_to(request, 'pending_call', call_id=meeting_id)
    except Exception as e:
        # Anything the provider raised — Twilio, Azure or the SMTP server.
        logger.warning("Provider error sending reminder for meeting %s: %s", meeting_id, e)
        messages.error(request, _("The reminder could not be sent."))
        return _back_to(request, 'pending_call', call_id=meeting_id)

    if channel == "email":
        messages.success(request, _("Reminder sent by email."))
    else:
        messages.success(request, _("Reminder sent via WhatsApp."))
    return _back_to(request, 'pending_call', call_id=meeting_id)


# The button and the route were called send_whatsapp_reminder back when WhatsApp
# was the only channel. Kept as an alias so old links and bookmarks still work.
send_whatsapp_reminder_view = send_meeting_reminder_view


@require_POST
@login_required
def cancel_meeting(request, meeting_id):
    """Call off a scheduled call, or put it back.

    A cancelled call leaves the queue but stays in the history. Deleting it
    would make the record say the call was never arranged, which is a different
    fact from one that was arranged and called off — and the second is the one
    worth keeping, because it is usually about the client.
    """
    qs = Meeting.objects.select_related("patient", "patient__navigator")
    if not is_admin(request.user):
        qs = qs.filter(patient__navigator=request.user)
    meeting = get_object_or_404(qs, pk=meeting_id)

    if request.POST.get("reinstate") == "1":
        if meeting.status != Meeting.Status.CANCELLED:
            messages.error(request, _("That call was not cancelled."))
        else:
            meeting.status = Meeting.Status.PENDING
            meeting.cancel_reason = ""
            meeting.cancelled_at = None
            meeting.save(update_fields=["status", "cancel_reason", "cancelled_at"])
            messages.success(request, _("The call is back on the schedule."))
        return _back_to(request, 'communications')

    # Only something still expected to happen can be called off. A finished
    # call already has an outcome, and overwriting it would lose what happened.
    if meeting.status != Meeting.Status.PENDING:
        messages.error(request, _("Only a scheduled call can be cancelled."))
        return _back_to(request, 'communications')

    meeting.status = Meeting.Status.CANCELLED
    meeting.cancel_reason = (request.POST.get("reason") or "").strip()[:200]
    meeting.cancelled_at = timezone.now()
    meeting.save(update_fields=["status", "cancel_reason", "cancelled_at"])
    messages.success(request, _("Call cancelled. It stays in the history."))
    return _back_to(request, 'communications')
