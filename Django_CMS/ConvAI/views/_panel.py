"""The right-hand detail panel.

One component, opened from Home, Communications and the client timeline. The
open item lives in the URL as ``?item=<kind>-<id>`` so a panel can be linked,
refreshed and opened in a new tab — a panel that only exists in JavaScript
state cannot.

Reading an item never costs a page load. Actually *working* one still does:
the panel's primary action hands off to the call console, the conversation
view or the full alert page, which are working surfaces rather than reading
ones.

Away from the client's own page the panel carries a context strip (who this
is, how much history they have, when you last spoke) so triage from a queue
never happens blind. On the client timeline that strip is redundant, so it is
dropped — same component, one less row.
"""
from ._base import *  # noqa: F401,F403

__all__ = ['resolve_panel_item', 'panel_context']


def _can_see(user, patient):
    return bool(patient) and (is_admin(user) or patient.navigator_id == user.id)


def _patient_phones(patient):
    nums = []
    if patient.phone_number:
        nums.append(str(patient.phone_number))
    if patient.caregiver and patient.caregiver.phone_number:
        nums.append(str(patient.caregiver.phone_number))
    return nums


def _context_strip(patient):
    """The one-line history summary shown when triaging away from the client."""
    nums = _patient_phones(patient)

    calls = CallRecording.objects.filter(to_number__in=nums).count() if nums else 0
    chat_days = (
        Message.objects.filter(user__in=nums)
        .annotate(day=TruncDate('timestamp')).values('day').distinct().count()
        if nums else 0
    )
    open_alerts = patient.alerts.exclude(status=Alert.AlertStatus.RESOLVED).count()

    # Last contact = the most recent thing that actually happened, whichever
    # channel it came through.
    stamps = []
    last_rec = (CallRecording.objects.filter(to_number__in=nums)
                .order_by('-start_time').values_list('start_time', flat=True).first()) if nums else None
    last_msg = (Message.objects.filter(user__in=nums)
                .order_by('-timestamp').values_list('timestamp', flat=True).first()) if nums else None
    last_meet = (patient.meetings.exclude(status=Meeting.Status.PENDING)
                 .order_by('-scheduled_time').values_list('scheduled_time', flat=True).first())
    stamps = [s for s in (last_rec, last_msg, last_meet) if s]

    return {
        'patient': patient,
        'calls': calls,
        'chats': chat_days,
        'open_alerts': open_alerts,
        'last_contact': max(stamps) if stamps else None,
    }


def _alert_panel(request, pk):
    alert = (Alert.objects
             .select_related('patient', 'patient__caregiver', 'user')
             .filter(pk=pk).first())
    if not alert:
        return None
    if not (is_admin(request.user)
            or alert.user_id == request.user.id
            or _can_see(request.user, alert.patient)):
        return None

    data = alert.data if isinstance(alert.data, dict) else {}
    # Type, priority and status moved: priority and status are the tag beside
    # the title, and the rest is the Technical section. What is left here is
    # what someone reading the alert actually needs.
    points = []
    # Only an admin sees several navigators' alerts in one queue, so only an
    # admin needs telling whose this is. It lives here rather than on the row:
    # the list stays identical for everyone, and ownership is context you want
    # when you open something, not while you scan.
    if is_admin(request.user) and alert.patient and alert.patient.navigator:
        nav = alert.patient.navigator
        points.append({'text': _("Navigator: %s") % (nav.get_full_name() or nav.username)})

    if data.get('archive_bucket'):
        points.append({'text': _("Archived as: %s") % (
            _("False alarm") if data['archive_bucket'] == 'false_alarms' else _("Resolved"))})
    if data.get('internal_note'):
        points.append({'text': _("Note: %s") % data['internal_note']})

    # The agent switch belongs to the client, so a general alert \u2014 which has no
    # client \u2014 simply does not carry it.
    bot = {}
    if alert.patient:
        bot = {
            'bot_off': not alert.patient.chatbot_enabled,
            'bot_toggle_url': reverse('toggle_patient_chatbot', args=[alert.patient.pk]),
            'raise_alert_url': reverse('raise_alert', args=[alert.patient.pk]),
        }

    # Priority is the tag, always, in the colour the rows already use — it is
    # the triage signal, and hiding it behind a status would cost more than the
    # status is worth. Status goes in the meta line, and only when it is
    # something other than "nobody has touched this yet".
    TAG = {Alert.Priority.HIGH: 'high', Alert.Priority.MEDIUM: 'med',
           Alert.Priority.LOW: 'low'}
    tag = (TAG.get(alert.priority, 'low'), alert.get_priority_display())
    state = ('' if alert.status == Alert.AlertStatus.CREATED
             else alert.get_status_display())

    return {
        **bot,
        'kind': 'alert',
        'tag_class': tag[0],
        'tag_label': tag[1],
        # Ordered pairs rather than the raw dict: a template cannot sort one and
        # the key order would otherwise change between rows.
        'alert_data': sorted(
            (k, v) for k, v in data.items()
            if k not in ('internal_note', 'archive_bucket') and not isinstance(v, (dict, list))
        ),
        # Not "Alert \u00b7 high": the tag beside the title says high, in colour.
        'kicker': _("Alert"),
        'state': state,
        'source': alert.get_alert_type_display(),
        'accent': 'crit' if alert.priority == Alert.Priority.HIGH else (
            'warn' if alert.priority == Alert.Priority.MEDIUM else 'calm'),
        'title': alert.title or _("Alert"),
        'when': alert.created_at,
        'patient': alert.patient,
        'overview_heading': _("Description"),
        'overview': alert.description,
        'points': points,
        'alert': alert,
        'detail_url': reverse('alert_detail', args=[alert.pk]),
    }


def _meeting_protocols(meeting):
    """Every protocol that has questions, with this meeting's answers folded in.

    Deliberately not filtered by the meeting: `pending_call` has always listed
    all protocols carrying questions rather than a per-meeting subset, and the
    panel keeps that behaviour so the two surfaces agree. The scheduled one is
    flagged rather than isolated.
    """
    protocols = (Protocol.objects
                 .filter(questions__isnull=False)
                 .prefetch_related('questions')
                 .distinct()
                 .order_by('number'))

    answers = {
        a.question_id: a.response
        for a in Answer.objects.filter(meeting=meeting,
                                       question__protocol__in=protocols)
    }

    out = []
    for p in protocols:
        questions = []
        for q in p.questions.all():
            questions.append({
                'id': q.id,
                # ProtocolAnswerForm names its fields q_<id>; autosave posts the
                # whole protocol back to protocol_view, so these must match.
                'field': f'q_{q.id}',
                'prompt_md': q.prompt_md,
                'answer': answers.get(q.id, ''),
            })
        answered = sum(1 for q in questions if q['answer'])
        out.append({
            'number': p.number,
            'title': p.title,
            'description': p.description,
            'questions': questions,
            'answered': answered,
            'total': len(questions),
            'is_scheduled': p.number == meeting.scheduled_protocol,
            'started': answered > 0,
            'complete': answered == len(questions) and questions,
        })
    return out


def _meeting_panel(request, pk):
    meeting = (Meeting.objects
               .select_related('patient', 'patient__caregiver', 'patient__navigator')
               .filter(pk=pk).first())
    if not meeting or not _can_see(request.user, meeting.patient):
        return None

    last_call = (Meeting.objects
                 .filter(patient=meeting.patient, scheduled_time__lt=meeting.scheduled_time)
                 .exclude(pk=meeting.pk)
                 .order_by('-scheduled_time')
                 .first())

    caregiver = meeting.patient.caregiver
    protocols = _meeting_protocols(meeting)
    is_pending = meeting.status == Meeting.Status.PENDING
    in_person = meeting.modality == Meeting.Modality.IN_PERSON

    # The recording belongs to this meeting rather than sitting beside it in the
    # list, so the panel is where it is played. Matched on time because nothing
    # links the two tables — see build_patient_events, which folds them the same
    # way for the same reason.
    recording = None
    nums = _patient_phones(meeting.patient)
    if nums:
        window = dt.timedelta(seconds=5400)
        recording = (CallRecording.objects
                     .filter(to_number__in=nums,
                             start_time__gte=meeting.scheduled_time - window,
                             start_time__lte=meeting.scheduled_time + window)
                     .order_by('start_time')
                     .first())

    return {
        'kind': 'meeting',
        'tag_class': ('todo' if is_pending else {
            Meeting.Status.COMPLETED: 'done',
            Meeting.Status.INTERRUPTED: 'int',
            Meeting.Status.NOT_ANSWERED: 'int',
            Meeting.Status.CANCELLED: 'off',
        }.get(meeting.status, 'int')),
        'is_cancelled': meeting.status == Meeting.Status.CANCELLED,
        'cancel_url': reverse('cancel_meeting', args=[meeting.pk]),
        'tag_label': (_("To do") if is_pending else meeting.get_status_display()),
        'kicker': (
            (_("In-person meeting") if is_pending else _("Meeting ended")) if in_person
            else (_("Scheduled call") if is_pending else _("Call ended"))
        ),
        'accent': 'crit' if (is_pending and meeting.scheduled_time < timezone.now()) else 'calm',
        'title': (
            ((_("Meeting with %s") if in_person else _("Call with %s")) % caregiver)
            if caregiver else meeting.get_type_display()
        ),
        'when': meeting.scheduled_time,
        'patient': meeting.patient,
        'meeting': meeting,
        'is_pending': is_pending,
        'in_person': in_person,
        'location': meeting.location,

        # An in-person meeting has somewhere to be, not a number to ring, so
        # neither the bridge nor the reminder applies to it.
        'recording': recording,
        'recording_url': (reverse('serve_protected_file', args=[recording.recording_sid])
                          if recording else ''),
        # Both were only reachable from a page nothing links to any more.
        'transcribe_url': (reverse('transcribe_recording', args=[recording.recording_sid])
                           if recording else ''),
        'summarize_url': reverse('summarize_meeting', args=[meeting.pk]),
        'recording_len': (
            "%d:%02d" % divmod(recording.duration or 0, 60) if recording else ''
        ),

        # Start call bridges the navigator's own phone to the caregiver's, so
        # both numbers have to exist before the button means anything.
        'can_call': bool(not in_person and caregiver and caregiver.phone_number
                         and request.user.phone_number),
        'can_remind': bool(caregiver and caregiver.phone_number),
        'last_call': last_call,
        'protocols': protocols,
        # Meeting.Protocol labels are placeholders ("2. Protocol 2"); the real
        # name lives on the Protocol record. Prefer it where one exists, so the
        # header and the card below it do not disagree.
        'scheduled_title': next(
            (f"{p['number']}. {p['title']}" for p in protocols if p['is_scheduled']),
            meeting.get_scheduled_protocol_display()
        ),
        'answered_total': sum(p['answered'] for p in protocols),
        'question_total': sum(p['total'] for p in protocols),
        # executed_protocol draws from Meeting.Protocol, a longer list than the
        # Protocol records above — the two vocabularies are not interchangeable.
        'protocol_choices': Meeting.Protocol.choices,
        # For the reschedule dialog: the datetime-local input wants this exact
        # shape, and a localtime value so it does not shift by the tz offset.
        'scheduled_local': timezone.localtime(meeting.scheduled_time).strftime("%Y-%m-%dT%H:%M"),
        'edit_url': reverse('edit_meeting', args=[meeting.pk]),
        'modality_choices': Meeting.Modality.choices,
        'type_choices': Meeting.MeetingType.choices,
        'completed_status': Meeting.Status.COMPLETED,
        # Answers and notes stay editable after the call ends. A call is often
        # written up afterwards, and both endpoints already allow it — they
        # check who you are, never the meeting's status. Only the old
        # protocol_form page hid the fields, and that was the rule this panel
        # used to copy.
        'can_edit_answers': True,
        'automations_enabled': get_bool("ENABLE_AUTOMATIONS"),
        'notes': meeting.notes,
    }


def _recording_panel(request, pk):
    rec = CallRecording.objects.filter(pk=pk).first()
    if not rec:
        return None
    patient = (Patient.objects
               .select_related('caregiver', 'navigator')
               .filter(Q(phone_number=rec.to_number) | Q(caregiver__phone_number=rec.to_number))
               .first())
    if not _can_see(request.user, patient):
        return None

    mins, secs = divmod(rec.duration or 0, 60)
    return {
        'kind': 'recording',
        'kicker': _("Phone call"),
        'accent': 'calm',
        'title': _("Call recording"),
        'when': rec.start_time,
        'patient': patient,
        'overview_heading': _("Transcript summary") if rec.transcript_summary else '',
        'overview': rec.transcript_summary,
        'points': [
            {'text': _("Called %s") % rec.to_number, 'stamp': f"{mins}:{secs:02d}"},
        ],
        'duration_label': f"{mins}:{secs:02d}",
        'recording': rec,
        'audio_url': reverse('serve_protected_file', args=[rec.recording_sid]),
        'transcribe_url': reverse('transcribe_recording', args=[rec.recording_sid]),
    }


def _chat_panel(request, ident):
    """``chat-<patient_pk>-<YYYY-MM-DD>`` — one day of chatbot conversation."""
    patient_pk, _sep, day_str = ident.partition('-')
    try:
        patient = Patient.objects.select_related('caregiver', 'navigator').get(pk=int(patient_pk))
        day = dt.datetime.strptime(day_str, "%Y-%m-%d").date()
    except (ValueError, Patient.DoesNotExist):
        return None
    if not _can_see(request.user, patient):
        return None

    nums = _patient_phones(patient)
    msgs = (Message.objects.filter(user__in=nums, timestamp__date=day).order_by('timestamp')
            if nums else Message.objects.none())
    conv = (Conversation.objects.filter(patient=patient, last_message_at__date=day)
            .order_by('-last_message_at').first())

    # What the agent watches for on this client, so the navigator's yes/no has
    # something to answer. Empty until an agent defines detectors, and the
    # review renders without the section rather than with an empty one.
    labels = []
    if conv and conv.agent and isinstance(getattr(conv.agent, 'detectors', None), dict):
        labels = list(conv.agent.detectors.keys())
    human = conv.human_flags if conv and isinstance(conv.human_flags, dict) else {}
    auto = conv.auto_flags if conv and isinstance(conv.auto_flags, dict) else {}
    flags = [{'label': l, 'checked': bool(human.get(l, auto.get(l)))} for l in labels]

    return {
        'kind': 'chat',
        'kicker': _("Chatbot"),
        'accent': 'calm',
        'title': conv.topic if conv and conv.topic else _("Conversation"),
        'when': timezone.make_aware(dt.datetime.combine(day, dt.time.min)),
        'patient': patient,
        'overview_heading': _("Summary") if conv and conv.summary else '',
        'overview': conv.summary if conv else '',
        'points': [],
        'message_count': msgs.count(),
        'messages': msgs[:20],
        # The Summary tab is otherwise one paragraph. This is the same detail
        # the old conversation page carried in its header, as a list.
        'facts': [
            (_("Messages"), msgs.count()),
            (_("Between"), "%s – %s" % (
                timezone.localtime(msgs[0].timestamp).strftime("%H:%M"),
                timezone.localtime(msgs.reverse()[0].timestamp).strftime("%H:%M"),
            ) if msgs.count() else _("—")),
            (_("Agent"), (conv.agent.name if conv and conv.agent else _("Not recorded"))),
            (_("Topic"), (conv.topic if conv and conv.topic else _("Not classified"))),
            (_("Flagged"), (_("Needs attention") if conv and conv.is_important
                            else _("Nothing raised automatically"))),
        ],

        # The judgement is on the exchange as a whole. Per-message feedback was
        # dropped deliberately — see the review block in _detail_panel.html.
        'conv': conv,
        'feedback_url': (reverse('conversation_feedback', args=[str(conv.id)])
                         if conv else ''),
        'rating': conv.rating if conv else None,
        'feedback': conv.feedback if conv else '',
        'flags': flags,

        # Turning the agent off is stored per client, not per conversation, so
        # the same switch reads the same wherever it is shown.
        'bot_off': not patient.chatbot_enabled,
        'bot_toggle_url': reverse('toggle_patient_chatbot', args=[patient.pk]),
        'raise_alert_url': reverse('raise_alert', args=[patient.pk]),
    }


_RESOLVERS = {
    'alert': _alert_panel,
    'meeting': _meeting_panel,
    'recording': _recording_panel,
    'chat': _chat_panel,
}


def resolve_panel_item(request):
    """Turn ``?item=<kind>-<id>`` into panel context, or None.

    Returns None for anything unreadable — a bad token, a deleted row, or an
    object belonging to someone else's client. The page then renders without a
    panel rather than erroring, so a stale link degrades to a normal page.
    """
    token = (request.GET.get('item') or '').strip()
    if not token:
        return None
    kind, _sep, ident = token.partition('-')
    resolver = _RESOLVERS.get(kind)
    if not resolver or not ident:
        return None

    if kind == 'chat':
        item = resolver(request, ident)
    else:
        try:
            item = resolver(request, int(ident))
        except (TypeError, ValueError):
            return None
    if item:
        item['token'] = token
        # Opening it is what reading it means. Only alerts and chats carry the
        # mark; SeenMark.mark ignores everything else.
        SeenMark.mark(request.user, token)
    return item


def panel_context(request, in_client_page_for=None):
    """Panel context for a view.

    ``in_client_page_for`` is the patient whose page is being rendered; when
    the panel's item belongs to them, the context strip is dropped because the
    surrounding page already answers "who is this".
    """
    item = resolve_panel_item(request)
    if not item:
        return {}

    # Closing drops only `item`, so filters and sort survive the round trip.
    rest = request.GET.copy()
    rest.pop('item', None)

    ctx = {'panel_item': item, 'panel_close_query': rest.urlencode()}
    patient = item.get('patient')
    if patient and (in_client_page_for is None or in_client_page_for.pk != patient.pk):
        ctx['panel_context'] = _context_strip(patient)
    return ctx
