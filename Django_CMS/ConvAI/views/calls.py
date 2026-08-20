from ._base import *  # noqa: F401,F403
import logging

from ._panel import panel_context

logger = logging.getLogger(__name__)

__all__ = ['scoped_meeting_form', 'save_scheduled_meeting', 'calendar_view', 'calendar_create_meeting', 'complete_meeting', 'cancel_meeting', 'edit_meeting', 'pending_call', 'make_phone_call', 'schedule_call', 'send_whatsapp_reminder_view']


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

    caregiver = meeting.patient.caregiver
    if not caregiver or not caregiver.phone_number:
        return _call_error(
            _("This client has no caregiver with a phone number to call."), 400,
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
    dyad_phone = str(caregiver.phone_number)
    ctn_phone  = str(request.user.phone_number)
    platform_phone = get_platform_phone()

    try:
        # Llamada al helper que inicia la conferencia
        conference_result = make_phone_conference({
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

    # Incrementar retries en 1
    Meeting.objects.filter(pk=meeting.pk).update(retries=F('retries') + 1)

    # Puedes devolver detalles de la conferencia si quieres
    return JsonResponse({
        'status': 'ok',
        'conference': conference_result
    })


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

    # Si se marcó Completed, debemos capturar el protocolo ejecutado
    if new_status == Meeting.Status.COMPLETED:
        try:
            ep = int(request.POST.get('executed_protocol', ''))
            meeting.executed_protocol = ep
        except (ValueError, TypeError):
            messages.error(request, _("You must choose an executed protocol."))
            return _back_to(request, 'pending_call', call_id=meeting_id)
    else:
        meeting.executed_protocol = None

    meeting.save()
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
def send_whatsapp_reminder_view(request, meeting_id):
    """
    View que dispara el envío de un WhatsApp recordatorio al cuidador.
    Solo el CTN asignado o staff pueden usarla.
    """
    meeting = get_object_or_404(
        Meeting.objects.select_related('patient__caregiver'),
        pk=meeting_id
    )

    # Permisos: debe ser el CTN (navigator) o staff
    if not (is_admin(request.user) or meeting.patient.navigator == request.user):
        return HttpResponseForbidden(_("You do not have permission to send reminders."))

    try:
        send_whatsapp_reminder(meeting)
    except ValueError as e:
        logger.warning("Could not send WhatsApp reminder for meeting %s: %s", meeting_id, e)
        messages.error(request, _("The reminder could not be sent."))
        return _back_to(request, 'pending_call', call_id=meeting_id)
    except Exception as e:
        # Cualquier otro error de Twilio
        logger.warning("Twilio error sending WhatsApp reminder for meeting %s: %s", meeting_id, e)
        messages.error(request, _("The reminder could not be sent."))
        return _back_to(request, 'pending_call', call_id=meeting_id)

    messages.success(request, _("Reminder sent via WhatsApp."))
    return _back_to(request, 'pending_call', call_id=meeting_id)


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
