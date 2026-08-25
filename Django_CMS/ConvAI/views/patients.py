from ._base import *  # noqa: F401,F403
from ._panel import panel_context
from django.db.models import Max
from ..forms import ClientForm, PatientForm

__all__ = ['build_patient_events', 'save_client_note', 'update_client_terms', 'toggle_patient_chatbot', 'raise_alert', 'create_client', 'download_care_plan', 'edit_care_plan', 'edit_patient', 'edit_patient_details', 'extract_study_id', 'patient_list', 'patient_conversation_detail', 'patient_detail', 'view_care_plan']


def _format_duration(seconds):
    """Human-friendly call duration: `h:mm:ss` for calls over an hour, else `m:ss`."""
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "—"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _week_label(start, end):
    """Compact week range label, e.g. 'Jul 7 – 13' or 'Jun 30 – Jul 6'."""
    if start.month == end.month:
        return f"{formats.date_format(start, 'M j')} – {formats.date_format(end, 'j')}"
    return f"{formats.date_format(start, 'M j')} – {formats.date_format(end, 'M j')}"


def _group_timeline(timeline, focus_token=None):
    """Group a reverse-chronological event list into months → weeks.

    Returns a list of month dicts (newest first), each with per-kind counts and
    a list of week dicts. The newest month and its newest week are flagged
    ``open`` so the page loads compact but with the latest activity visible.

    ``focus_token`` is the panel item the page was opened on. Its month and week
    are opened instead, so arriving from "Open timeline" lands on the entry you
    came for rather than on the most recent one — which for anything older than
    this week would otherwise be collapsed out of sight.
    """
    from collections import OrderedDict

    def _counts():
        return {"alert": 0, "recording": 0, "meeting": 0, "conversation": 0, "total": 0}

    today = timezone.localdate()
    cur_month = (today.year, today.month)
    cur_week_start = today - dt.timedelta(days=today.weekday())

    months = OrderedDict()
    focus_mkey = focus_wkey = None
    for e in timeline:
        local = timezone.localtime(e["ts"])
        d = local.date()
        mkey = (d.year, d.month)
        wk_start = d - dt.timedelta(days=d.weekday())
        wkey = wk_start.isoformat()
        if focus_token and e.get("panel_token") == focus_token:
            focus_mkey, focus_wkey = mkey, wkey

        month = months.get(mkey)
        if month is None:
            month = months[mkey] = {
                "label": formats.date_format(local, "F Y"),
                "counts": _counts(),
                "is_current": mkey == cur_month,
                "weeks": OrderedDict(),
            }
        week = month["weeks"].get(wkey)
        if week is None:
            wk_end = wk_start + dt.timedelta(days=6)
            week = month["weeks"][wkey] = {
                "label": _week_label(wk_start, wk_end),
                "counts": _counts(),
                "is_current": wk_start == cur_week_start,
                "events": [],
            }
        week["events"].append(e)
        for c in (month["counts"], week["counts"]):
            c[e["kind"]] += 1
            c["total"] += 1

    groups = []
    for i, (mkey, month) in enumerate(months.items()):
        weeks = list(month["weeks"].items())
        for j, (wkey, week) in enumerate(weeks):
            week["open"] = ((mkey, wkey) == (focus_mkey, focus_wkey)) if focus_mkey \
                else (i == 0 and j == 0)
        month["weeks"] = [w for _k, w in weeks]
        month["open"] = (mkey == focus_mkey) if focus_mkey else (i == 0)
        groups.append(month)
    return groups


def extract_study_id(details):
    if not details:
        return None

    # Modified regex to allow spaces and asterisks '[\s*]*' before the colon/dash
    match = re.search(
        r"Id de Estudio[\s*]*[:\-]\s*([A-Za-z0-9_-]+)",
        details,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    return None


@login_required
def patient_list(request):
    now = timezone.now()

    qs = Patient.objects.select_related('caregiver', 'navigator').order_by('name', 'lastname')

    if not is_admin(request.user):
        qs = qs.filter(navigator=request.user)

    rows_data = []
    navigator_names = set()

    for p in qs:
        next_call = (
            p.meetings
             .filter(scheduled_time__gt=now)
             .order_by('scheduled_time')
             .first()
        )

        last_call = (
            p.meetings
             .filter(scheduled_time__lte=now)
             .order_by('-scheduled_time')
             .first()
        )

        nav_name = (
            p.navigator.get_full_name().strip()
            if p.navigator and p.navigator.get_full_name().strip()
            else (p.navigator.username if p.navigator else None)
        )

        if nav_name:
            navigator_names.add(nav_name)

        rows_data.append({
            'id': p.pk,
            'study_id': extract_study_id(p.details),
            'patient_name': f"{p.name} {p.lastname}",
            'caregiver_name': str(p.caregiver) if p.caregiver else None,
            'navigator_name': nav_name,

            'next_call_time': timezone.localtime(next_call.scheduled_time).strftime('%d/%m/%Y %H:%M') if next_call else None,

            'last_call_time': timezone.localtime(last_call.scheduled_time).strftime('%d/%m/%Y %H:%M') if last_call else None,
            'last_call_status': last_call.status if last_call else None,
            'last_call_status_display': last_call.get_status_display() if last_call else None,
        })

    admin = is_admin(request.user)
    return render(request, 'patients/patient_list.html', {
        'rows_json': json.dumps(rows_data),
        'navigator_options_json': json.dumps(sorted(navigator_names)),
        'active_page': 'patients',
        'client_form': ClientForm(is_admin=admin),
        'client_admin': admin,
    })


@login_required
@navigator_required
@require_POST
def create_client(request):
    """Create a client. Navigators are auto-assigned; admins pick the navigator."""
    admin = is_admin(request.user)
    form = ClientForm(request.POST, is_admin=admin)
    if form.is_valid():
        patient = form.save(commit=False)
        if not admin:
            patient.navigator = request.user
        cd = form.cleaned_data
        cname = (cd.get("caregiver_name") or "").strip()
        clast = (cd.get("caregiver_lastname") or "").strip()
        if cname or clast:
            patient.caregiver = Caregiver.objects.create(
                name=cname, lastname=clast,
                phone_number=(cd.get("caregiver_phone") or None),
                email=(cd.get("caregiver_email") or "").strip(),
            )
        patient.save()
        messages.success(request, _("Client created."))
    else:
        errs = "; ".join(f"{f}: {', '.join(e)}" for f, e in form.errors.items())
        messages.error(request, _("Could not create client.") + " " + errs)
    return redirect('patients')


@login_required
def edit_patient(request, pk):
    """Edit a client's core fields (name, phone, navigator, agent).

    Admins may edit any client; navigators only their own assigned clients.
    """
    patient = get_object_or_404(Patient, pk=pk)
    admin = is_admin(request.user)
    if not (admin or patient.navigator_id == request.user.id):
        return HttpResponseForbidden(_("You cannot edit this client."))

    if request.method == 'POST':
        form = PatientForm(request.POST, instance=patient, is_admin=admin)
        if form.is_valid():
            obj = form.save(commit=False)
            if not admin:
                obj.navigator = request.user  # keep ownership; field is hidden

            # Create/update the linked caregiver from the caregiver_* fields.
            cd = form.cleaned_data
            cname = (cd.get("caregiver_name") or "").strip()
            clast = (cd.get("caregiver_lastname") or "").strip()
            cphone = (cd.get("caregiver_phone") or "").strip() or None
            cemail = (cd.get("caregiver_email") or "").strip()
            if cname or clast or cphone or cemail:
                caregiver = obj.caregiver or Caregiver()
                caregiver.name = cname
                caregiver.lastname = clast
                caregiver.phone_number = cphone
                caregiver.email = cemail
                caregiver.save()
                obj.caregiver = caregiver

            obj.save()
            messages.success(request, _("Client updated."))
            return redirect('patient_detail', pk=patient.pk)
    else:
        form = PatientForm(instance=patient, is_admin=admin)
    return render(request, 'patients/edit_patient.html', {
        'active_page': 'patients',
        'form': form,
        'patient': patient,
    })


def build_patient_events(patient, protocol_numbers=None):
    """Normalise every interaction with one client into a single event list.

    Alerts, phone-call recordings, scheduled meetings and chatbot-conversation
    days all become dicts with a common ``kind``/``ts`` shape, so one template
    can render them as a timeline instead of separate tables. Shared by the
    client page and the cross-client Communications page; the latter passes
    ``protocol_numbers`` in so the lookup runs once per page, not per client.
    """
    nums = []
    if patient.phone_number:
        nums.append(str(patient.phone_number))
    if patient.caregiver and patient.caregiver.phone_number:
        nums.append(str(patient.caregiver.phone_number))

    # A mapping rather than a set, so the real protocol name is available too.
    # Meeting.Protocol's labels are placeholders ("3. Protocol 3"); the name a
    # navigator recognises lives on the Protocol record.
    if protocol_numbers is None:
        protocol_numbers = dict(Protocol.objects.values_list('number', 'title'))
    elif not isinstance(protocol_numbers, dict):
        protocol_numbers = {n: '' for n in protocol_numbers}

    events = []

    # Alerts raised for this client.
    for al in patient.alerts.all():
        events.append({
            'kind': 'alert',
            'ts': al.created_at,
            'pk': al.pk,
            'panel_token': f'alert-{al.pk}',
            'patient': patient,
            'title': al.title or _("Alert"),
            'description': al.description,
            'priority': al.get_priority_display(),
            'priority_level': al.priority,
            'status': al.get_status_display(),
            'status_code': al.status,
            'alert_type': al.get_alert_type_display(),
            'archive_bucket': (al.data or {}).get('archive_bucket', '') if isinstance(al.data, dict) else '',
        })

    # Phone-call recordings, matched by callee number = patient or caregiver.
    # A recording is not an event in its own right: it is something attached to
    # the meeting it came from, so it is folded onto that meeting below rather
    # than listed beside it. Anything that cannot be matched still gets its own
    # row, because a recording nobody can reach is worse than a duplicate.
    recordings = list(
        CallRecording.objects.filter(to_number__in=nums).order_by('-start_time')
        if nums else CallRecording.objects.none()
    )

    def _rec_dict(rec):
        return {
            'pk': rec.pk,
            'duration': rec.duration,
            'duration_str': _format_duration(rec.duration),
            'recording_sid': rec.recording_sid,
            'to_number': rec.to_number,
            'from_number': rec.from_number,
            'transcript': rec.transcript,
            'transcript_summary': rec.transcript_summary,
            'transcribed': bool(rec.transcribed_at),
        }

    # Annotated rather than counted per row: `has_notes` is read once for every
    # meeting on the timeline, and a note is a row now, so asking per meeting
    # would be one query each.
    meetings = list(patient.meetings.annotate(note_count=Count('notes_list')))
    claimed = {}
    for rec in recordings:
        best, gap = None, None
        for mt in meetings:
            d = abs((mt.scheduled_time - rec.start_time).total_seconds())
            if gap is None or d < gap:
                best, gap = mt, d
        # An hour and a half either side: calls start late and run long, but a
        # recording further out than that belongs to a different conversation.
        if best is not None and gap is not None and gap <= 5400 and best.pk not in claimed:
            claimed[best.pk] = rec
        else:
            events.append({
                'kind': 'recording',
                'ts': rec.start_time,
                'panel_token': f'recording-{rec.pk}',
                'patient': patient,
                **_rec_dict(rec),
            })

    # Scheduled / executed meetings — a phone call or an in-person visit.
    for mt in meetings:
        proto_num = mt.executed_protocol or mt.scheduled_protocol
        rec = claimed.get(mt.pk)
        events.append({
            'kind': 'meeting',
            # When it happened, not when it was booked. A call recorded from a
            # diary entry days ahead used to land in Happened under a future
            # date, which is a list of what has happened containing something
            # that has not.
            'ts': mt.happened_at,
            'scheduled_ts': mt.scheduled_time,
            # A call that went out and was never closed. Carried onto the row so
            # it can be seen without opening it, and counted.
            'outcome_missing': mt.retries > 0 and mt.status == Meeting.Status.PENDING,
            'pk': mt.pk,
            'panel_token': f'meeting-{mt.pk}',
            'patient': patient,
            'status': mt.get_status_display(),
            'status_code': mt.status,
            'meeting_type': mt.get_type_display(),
            'modality': mt.modality,
            'modality_label': mt.get_modality_display(),
            'in_person': mt.modality == Meeting.Modality.IN_PERSON,
            'location': mt.location,
            'protocol': (
                f"{proto_num}. {protocol_numbers[proto_num]}"
                if protocol_numbers.get(proto_num)
                else (mt.get_executed_protocol_display()
                      or mt.get_scheduled_protocol_display())
            ),
            'protocol_num': proto_num if proto_num in protocol_numbers else None,
            'protocol_summary': mt.protocol_summary,
            'has_notes': bool(mt.note_count),
            'recording': _rec_dict(rec) if rec else None,
        })

    # Chatbot conversations, grouped by local day (one entry per active day).
    # Matched by phone (WhatsApp) OR patient-linked Conversation (tester/voice
    # chat), so web test-user sessions show up in the timeline too.
    day_rows = (
        Message.objects.filter(patient_message_q(patient))
        .annotate(day=TruncDate('timestamp'))
        .values('day')
        .annotate(last_ts=Max('timestamp'), msg_count=Count('id'))
        .order_by('-day')
    )
    for row in day_rows:
        events.append({
            'kind': 'conversation',
            'ts': row['last_ts'] or timezone.make_aware(
                dt.datetime.combine(row['day'], dt.time.min)
            ),
            'panel_token': f"chat-{patient.pk}-{row['day'].isoformat()}",
            'patient': patient,
            'day': row['day'],
            'day_str': row['day'].isoformat(),
            'msg_count': row['msg_count'],
        })

    return events


@login_required
def patient_detail(request, pk):
    patient = get_object_or_404(
        Patient.objects.select_related('caregiver', 'navigator'),
        pk=pk
    )
    # AS-08/F4 fix: enforce per-object ownership (staff or assigned navigator).
    if not (is_admin(request.user) or patient.navigator_id == request.user.id):
        return HttpResponseForbidden(_("You do not have permission to view this client."))
    now = timezone.now()

    # Próxima y última llamada
    next_call = (
        patient.meetings
               .filter(scheduled_time__gt=now)
               .order_by('scheduled_time')
               .first()
    )
    last_call = (
        patient.meetings
               .filter(scheduled_time__lte=now)
               .order_by('-scheduled_time')
               .first()
    )

    events = build_patient_events(patient)
    timeline = sorted(events, key=lambda e: e['ts'], reverse=True)
    # The newest thing that has actually happened. The timeline's first row is
    # whatever is furthest in the future once a call is scheduled, so taking it
    # made "last contact" report the next appointment — and read as 0 minutes
    # ago the moment one was booked.
    last_contact = next((e['ts'] for e in timeline if e['ts'] <= now), None)

    # Only the protocols this client has actually had. The old list walked all
    # ten of Meeting.Protocol and printed eight zeroes; which protocols exist is
    # a per-deployment question, so it comes from the Protocol records.
    counts = {
        row['executed_protocol']: row['count']
        for row in (patient.meetings
                    .filter(status=Meeting.Status.COMPLETED)
                    .values('executed_protocol')
                    .annotate(count=Count('id')))
    }
    titles = dict(Protocol.objects.values_list('number', 'title'))
    protocols_executed = [
        {'number': num, 'title': titles.get(num) or _("Protocol %s") % num,
         'count': counts[num]}
        for num in sorted(counts)
        if num and counts[num]
    ]

    # Every term the service knows about, with this client's marked. The five
    # standard ones lead; anything a colleague has added follows.
    chosen = patient.contact_terms if isinstance(patient.contact_terms, list) else []
    contact_terms = [
        {'key': t.slug, 'label': t.label, 'standard': t.is_standard,
         'on': t.slug in chosen}
        for t in ContactTerm.objects.all()
    ]

    # The same list as the Communications page, scoped to this client. Sharing
    # the builder is the point: the two surfaces answer the same question and
    # should not drift apart. The panel context comes with it, and is told this
    # is the client's own page so the "who is this" strip is dropped — you are
    # already looking at them.
    from .communications import comms_list_context
    comms = comms_list_context(request, events, for_patient=patient)

    return render(request, 'patients/patient_detail.html', {
        **comms,
        'patient': patient,
        'next_call': next_call,
        'last_call': last_call,
        'timeline': timeline,
        'timeline_count': len(timeline),
        'last_contact': last_contact,
        'protocols_executed': protocols_executed,
        # send_care_plan_whatsapp 403s when the flag is off, so the button is
        # hidden rather than shown and broken. A caregiver number is the other
        # half of it — there is nowhere to send it without one.
        'can_send_care_plan': bool(
            patient.care_plan
            and get_bool("SEND_CARE_PLAN")
            and patient.caregiver
            and patient.caregiver.phone_number
        ),
        'contact_terms': contact_terms,
        'client_since': timeline[-1]['ts'] if timeline else None,
        'agents': Agent.objects.order_by('name'),
        'SEND_CARE_PLAN' : get_bool("SEND_CARE_PLAN"),
        'active_page':'patients',
    })


@login_required
def patient_conversation_detail(request, pk, day):
    # patient + permission
    patient = get_object_or_404(
        Patient.objects.select_related('caregiver', 'navigator', 'agent'),
        pk=pk
    )
    if not (is_admin(request.user) or patient.navigator == request.user):
        return HttpResponseForbidden(_("You do not have permission to view this conversation."))

    # Parse day (YYYY-MM-DD) → date
    try:
        day_date = dt.datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        raise Http404("Fecha inválida")

    # Messages that belong to this patient: matched by phone (WhatsApp) OR by
    # patient-linked Conversation (tester/voice chat store a username instead).
    patient_q = patient_message_q(patient)

    # Optional: mark one conversation as visited (only CTN, and only if it belongs to this patient)
    visited_param = request.GET.get("visited")
    if visited_param and patient.navigator == request.user:
        try:
            conv_uuid = uuid.UUID(str(visited_param))
            belongs = Message.objects.filter(
                patient_q, conversation_id=str(conv_uuid)
            ).exists()
            if belongs:
                Conversation.objects.filter(id=conv_uuid).update(visited=True)
        except Exception:
            pass

    # Build local-time window [start, end) for the selected day
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(dt.datetime.combine(day_date, dt.time.min), tz)
    end   = start + dt.timedelta(days=1)

    # Messages for that local day
    messages = (
        Message.objects
        .filter(patient_q, timestamp__gte=start, timestamp__lt=end)
        .order_by("timestamp")
    )

    # Distinct conversation ids → UUID list
    raw_ids = (
        messages.exclude(conversation_id__isnull=True)
                .exclude(conversation_id__exact="")
                .values_list("conversation_id", flat=True)
                .distinct()
    )
    conv_uuids = []
    for cid in raw_ids:
        try:
            conv_uuids.append(uuid.UUID(str(cid)))
        except (ValueError, TypeError):
            pass

    conversations = Conversation.objects.filter(id__in=conv_uuids)
    conv_map = {str(c.id): c for c in conversations}

    # Dynamic detector labels from Agent
    detector_labels = []
    if patient.agent and isinstance(patient.agent.detectors, dict):
        detector_labels = list(patient.agent.detectors.keys())

    today_local = timezone.localdate()
    return render(request, "patients/patient_conversation_detail.html", {
        "patient": patient,
        "day": day_date,
        "prev_day": (day_date - dt.timedelta(days=1)).isoformat(),
        "next_day": (day_date + dt.timedelta(days=1)).isoformat(),
        "is_today": day_date >= today_local,
        "messages": messages,
        "conv_map": conv_map,
        "detector_labels": detector_labels,
        "active_page": "patients",
    })


@login_required
def edit_patient_details(request, pk):
    patient = get_object_or_404(Patient.objects.select_related('caregiver', 'navigator'), pk=pk)
    # AS-08/F4 fix: enforce per-object ownership for read AND write.
    if not (is_admin(request.user) or patient.navigator_id == request.user.id):
        return HttpResponseForbidden(_("You do not have permission to edit this client."))

    if request.method == 'POST':
        form = PatientDetailsForm(request.POST, instance=patient)
        if form.is_valid():
            form.save()
            return redirect('patient_detail', pk=patient.pk)
    else:
        form = PatientDetailsForm(instance=patient)

    return render(request, 'patients/edit_patient_details.html', {
        'patient': patient,
        'form': form,
        'active_page': 'patients',
    })


@login_required
def edit_care_plan(request, pk):
    """
    Permite subir o actualizar el PDF de 'care_plan' para un paciente dado.
    """
    patient = get_object_or_404(Patient, pk=pk)

    # Solo el CTN asignado o staff pueden hacerlo
    if not (is_admin(request.user) or patient.navigator == request.user):
        messages.error(request, _("You do not have permission to edit this care plan."))
        return redirect("patient_detail", pk=pk)

    if request.method == "POST":
        form = CarePlanForm(request.POST, request.FILES, instance=patient)
        if form.is_valid():
            form.save()
            messages.success(request, _("Care plan updated successfully."))
            return redirect("patient_detail", pk=pk)
        # si hay validación fallida, dejamos caer al template
    else:
        form = CarePlanForm(instance=patient)

    return render(request, "patients/edit_care_plan.html", {
        "patient": patient,
        "form": form,
        "active_page": "patients",
    })


@login_required
def view_care_plan(request, pk):
    """
    Muestra el PDF del 'care_plan' embebido en una página.
    """
    patient = get_object_or_404(Patient, pk=pk)

    # Solo el CTN o staff deberían verlo
    if not (is_admin(request.user) or patient.navigator == request.user):
        messages.error(request, _("You do not have permission to view this care plan."))
        return redirect("patient_detail", pk=pk)

    # Si no hay care_plan, redirigimos
    if not patient.care_plan:
        messages.warning(request, _("This client has no care plan uploaded."))
        return redirect("patient_detail", pk=pk)

    return render(request, "patients/view_care_plan.html", {
        "patient": patient,
        "active_page": "patients",
    })


@login_required
def download_care_plan(request, pk):
    """
    Sirve el PDF del care_plan inline (para que un <iframe> lo muestre),
    siempre que el usuario sea staff o el CTN (navigator).
    """
    patient = get_object_or_404(Patient, pk=pk)

    # Permisos: staff o CTN asignado
    if not (is_admin(request.user) or patient.navigator == request.user):
        return HttpResponseForbidden(_("You do not have permission to view this file."))

    # Asegurarnos de que exista un archivo
    if not patient.care_plan:
        raise Http404("No se encontró el plan de cuidado.")

    # Ruta física al PDF
    file_path = patient.care_plan.path
    if not os.path.exists(file_path):
        raise Http404("Archivo no encontrado en el servidor.")

    # Abrir en modo binario y servirlo como PDF inline
    response = FileResponse(
        open(file_path, 'rb'),
        content_type='application/pdf'
    )
    return response




def _may_edit(request, patient):
    return is_admin(request.user) or patient.navigator_id == request.user.id


@login_required
@require_POST
def save_client_note(request, pk):
    """Autosave for the handover note.

    Answers JSON so the page never reloads. `details_editable` has existed all
    along and is empty for every client — which is what happens to a field that
    lives behind its own page — so this is the same field, reachable where it is
    read.
    """
    patient = get_object_or_404(Patient, pk=pk)
    if not _may_edit(request, patient):
        return HttpResponseForbidden(_("You do not have permission to edit this client."))

    patient.details_editable = (request.POST.get('note') or '').strip()
    patient.save(update_fields=['details_editable'])
    return JsonResponse({'ok': True})


@login_required
@require_POST
def update_client_terms(request, pk):
    """The agent, the caregiver's part, and how the client wants to be reached.

    One endpoint because they are edited from one block and it would be strange
    for two of the three to save and the third to fail. Redirects back to
    whatever the page was showing, so filters and the open panel survive.
    """
    patient = get_object_or_404(Patient.objects.select_related('caregiver'), pk=pk)
    if not _may_edit(request, patient):
        return HttpResponseForbidden(_("You do not have permission to edit this client."))

    if 'agent' in request.POST:
        raw = (request.POST.get('agent') or '').strip()
        patient.agent = Agent.objects.filter(pk=raw).first() if raw else None

    # A term written here joins the shared list, so the next person with the
    # same situation finds it instead of inventing a near-duplicate.
    added = (request.POST.get('new_term') or '').strip()[:40]
    if added:
        slug = slugify(added)[:40]
        if slug:
            term, made = ContactTerm.objects.get_or_create(
                slug=slug,
                defaults={'label': added, 'created_by': request.user},
            )
            request.POST = request.POST.copy()
            request.POST.update({'terms': term.slug})

    valid = set(ContactTerm.objects.values_list('slug', flat=True))
    picked = [t for t in request.POST.getlist('terms') if t in valid]
    if added and slugify(added)[:40] in valid and slugify(added)[:40] not in picked:
        picked.append(slugify(added)[:40])
    patient.contact_terms = picked

    # Background is edited here too, so the page needs one edit button rather
    # than four that each opened somewhere different.
    if 'details' in request.POST:
        patient.details = (request.POST.get('details') or '').strip()

    patient.save(update_fields=['agent', 'contact_terms', 'details'])

    caregiver = patient.caregiver
    if caregiver and ('relationship' in request.POST or 'involvement' in request.POST):
        caregiver.relationship = (request.POST.get('relationship') or '').strip()[:60]
        caregiver.involvement = (request.POST.get('involvement') or '').strip()[:120]
        caregiver.save(update_fields=['relationship', 'involvement'])

    messages.success(request, _("Client updated."))
    nxt = (request.POST.get('next') or '').strip()
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect('patient_detail', pk=patient.pk)


def _safe_next(request, fallback_pk):
    """A `next` we are willing to follow: our own path, never another host."""
    nxt = (request.POST.get('next') or '').strip()
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect('patient_detail', pk=fallback_pk)


@require_POST
@navigator_required
def toggle_patient_chatbot(request, pk):
    """Turn this client's agent off, or back on.

    The one action in the panel that changes what the caregiver experiences, so
    it records who did it and why. Messages keep arriving either way; what stops
    is the agent answering them.
    """
    patient = get_object_or_404(
        Patient.objects.select_related('navigator'), pk=pk)
    if not (is_admin(request.user) or patient.navigator_id == request.user.id):
        return HttpResponseForbidden(_("You cannot change this client's agent."))

    turning_off = (request.POST.get('state') or '') == 'off'
    patient.chatbot_enabled = not turning_off
    if turning_off:
        patient.chatbot_off_reason = (request.POST.get('reason') or '').strip()[:200]
        patient.chatbot_off_at = timezone.now()
        patient.chatbot_off_by = request.user
        messages.success(request, _("The agent is off for %s. Messages still arrive; "
                                    "nothing is answered automatically.") % patient.name)
    else:
        patient.chatbot_off_reason = ''
        patient.chatbot_off_at = None
        patient.chatbot_off_by = None
        messages.success(request, _("The agent is answering %s again.") % patient.name)
    patient.save(update_fields=['chatbot_enabled', 'chatbot_off_reason',
                                'chatbot_off_at', 'chatbot_off_by'])
    return _safe_next(request, patient.pk)


@require_POST
@navigator_required
def raise_alert(request, pk):
    """An alert a person raised, rather than one the agent produced.

    Until now the only way an alert existed was the agent creating one, so a
    navigator who read a conversation and saw what the agent missed had nowhere
    to put it.
    """
    patient = get_object_or_404(
        Patient.objects.select_related('navigator'), pk=pk)
    if not (is_admin(request.user) or patient.navigator_id == request.user.id):
        return HttpResponseForbidden(_("You cannot raise an alert for this client."))

    try:
        priority = int(request.POST.get('priority', Alert.Priority.MEDIUM))
    except (TypeError, ValueError):
        priority = Alert.Priority.MEDIUM
    if priority not in dict(Alert.Priority.choices):
        priority = Alert.Priority.MEDIUM

    note = (request.POST.get('note') or '').strip()
    Alert.objects.create(
        patient=patient,
        user=patient.navigator or request.user,
        created_by=request.user,
        priority=priority,
        # Not CONVERSATION: that type means the classifier produced it. This one
        # came from a person, and the audit trail should be able to tell them
        # apart later.
        alert_type=Alert.AlertType.DEFAULT,
        title=(request.POST.get('title') or '').strip()[:200] or _("Raised by navigator"),
        description=note,
        data={'raised_by_human': True, 'source': (request.POST.get('source') or '')[:64]},
    )
    messages.success(request, _("Alert raised for %s.") % patient.name)
    return _safe_next(request, patient.pk)
