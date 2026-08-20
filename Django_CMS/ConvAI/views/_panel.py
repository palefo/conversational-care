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

__all__ = ['resolve_panel_item', 'panel_context', 'panel_fragment']


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
    # The internal note used to be echoed here. It is a Note row now, shown in
    # the Notes tab with an author and a time, so repeating it in the summary
    # would be the same sentence twice.

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

    # The conversation behind the alert, resolved the way the original alert
    # page resolved it, in the same order of preference:
    #
    #   1. data['conversation_id'] or data['thread_id'] — the real link. The
    #      alerts API still accepts these, so an agent raising an alert through
    #      it can say exactly which exchange caused it.
    #   2. data['source'] — the panel token, written when a person presses
    #      Raise an alert from a conversation they are reading.
    #   3. Failing both, the client's messages from the day the alert appeared.
    #      This is the original's fallback too, and it is the one that can be
    #      wrong, so the panel labels it differently rather than passing it off
    #      as the linked exchange.
    #
    # An alert with none of the three shows no conversation, which is correct:
    # plenty of alerts have nothing to do with a chat.
    linked = Message.objects.none()
    link_is_exact = False

    conversation_id = str(data.get('conversation_id') or data.get('thread_id') or '').strip()
    if conversation_id:
        linked = Message.objects.filter(conversation_id=conversation_id).order_by('timestamp')
        link_is_exact = linked.exists()

    source = (data.get('source') or '')
    if not link_is_exact and source.startswith('chat-'):
        pk_str, _sep, day_str = source[len('chat-'):].partition('-')
        try:
            src_patient = Patient.objects.get(pk=int(pk_str))
            day = dt.datetime.strptime(day_str, "%Y-%m-%d").date()
        except (ValueError, Patient.DoesNotExist):
            src_patient, day = None, None
        if src_patient and day:
            nums = [n for n in (str(src_patient.phone_number or ''),
                                str(getattr(src_patient.caregiver, 'phone_number', '') or '')) if n]
            if nums:
                linked = (Message.objects
                          .filter(user__in=nums, timestamp__date=day)
                          .order_by('timestamp'))
                link_is_exact = linked.exists()

    if not link_is_exact and alert.patient:
        nums = [n for n in (str(alert.patient.phone_number or ''),
                            str(getattr(alert.patient.caregiver, 'phone_number', '') or '')) if n]
        if nums:
            linked = (Message.objects
                      .filter(user__in=nums,
                              timestamp__date=timezone.localtime(alert.created_at).date())
                      .order_by('timestamp'))

    return {
        **bot,
        'kind': 'alert',
        'messages': list(linked[:20]),
        'message_count': linked.count(),
        'link_is_exact': link_is_exact,
        'notes_list': _notes_for(alert=alert),
        'note_parent': 'alert',
        'note_parent_id': alert.pk,
        'tag_class': tag[0],
        'tag_label': tag[1],
        # Ordered pairs rather than the raw dict: a template cannot sort one and
        # the key order would otherwise change between rows.
        'alert_data': sorted(
            (k, v) for k, v in data.items()
            if k != 'archive_bucket' and not isinstance(v, (dict, list))
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


def _automation_state(meeting, protocols):
    """Which protocol, if any, is out with the caregiver right now.

    The Patient carries the running automation — which meeting and which
    protocol — so the card can say it was asked rather than offering to ask
    again, and a second navigator opening the panel does not text them twice.
    """
    patient = meeting.patient
    if not (patient and patient.automation_active):
        return None
    if patient.automation_meeting_id != meeting.pk:
        return None
    return {
        'protocol': patient.automation_protocol,
        # start_automation sets the deadline three hours out and slides it on
        # every reply, so this is "last heard from", which is the more useful
        # of the two anyway.
        'until': patient.automation_expires_at,
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

    # Answers follow the client, not the single call.
    #
    # The protocols are a programme a client works through once — Welcome and
    # orientation, then daily living, then medication — not a checklist repeated
    # every call. Reading only this meeting's answers meant a protocol finished
    # last week showed up blank and unstarted in every call after it, and the
    # same questions got asked again.
    #
    # The most recent answer wins, so an updated answer replaces the one it
    # corrects. Which call it came from travels with it: an answer given on this
    # call is editable here, one carried from an earlier call is shown as a
    # record of what they said, with the date.
    answers = {}
    for a in (Answer.objects
              .filter(meeting__patient=meeting.patient,
                      question__protocol__in=protocols)
              .select_related('meeting')
              .order_by('meeting__scheduled_time', 'pk')):
        answers[a.question_id] = {
            'text': a.response,
            'by_text': a.by_text,
            'mine': a.meeting_id == meeting.pk,
            'when': a.meeting.happened_at,
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
                'answer': answers.get(q.id, {}).get('text', ''),
                'by_text': answers.get(q.id, {}).get('by_text', False),
                # False when the answer was given on an earlier call: shown as a
                # record rather than a field, so saving this call cannot quietly
                # copy someone else's call into it.
                'mine': answers.get(q.id, {}).get('mine', True),
                'when': answers.get(q.id, {}).get('when'),
            })
        # Answered at all, by anyone, on any of this client's calls — which is
        # what makes a protocol read as done everywhere once it is done once.
        answered = sum(1 for q in questions if q['answer'])
        carried = sum(1 for q in questions if q['answer'] and not q['mine'])
        out.append({
            'by_text_count': sum(1 for q in questions if q['by_text'] and q['answer']),
            'carried_count': carried,
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
        'automation': _automation_state(meeting, protocols),
        # The three ways a call ends, in the model's own words, so the buttons
        # and the line confirming what was recorded can never drift apart. The
        # dot class travels with each one; cancelling is deliberately not here,
        # since calling an appointment off is not an outcome of it happening.
        # Placed but never closed. retries is bumped by make_phone_call, so a
        # meeting still Pending with one on it is a call that went out and was
        # never recorded — the case no prompt at the moment of ending can catch,
        # because the person was interrupted and never came back to the panel.
        'outcome_missing': meeting.retries > 0 and meeting.status == Meeting.Status.PENDING,
        'outcomes': [
            (Meeting.Status.COMPLETED, Meeting.Status.COMPLETED.label, 'done'),
            (Meeting.Status.NOT_ANSWERED, Meeting.Status.NOT_ANSWERED.label, 'miss'),
            (Meeting.Status.INTERRUPTED, Meeting.Status.INTERRUPTED.label, 'part'),
        ],
        # Meeting.Protocol labels are placeholders ("2. Protocol 2"); the real
        # name lives on the Protocol record. Prefer it where one exists, so the
        # header and the card below it do not disagree.
        'scheduled_title': next(
            (f"{p['number']}. {p['title']}" for p in protocols if p['is_scheduled']),
            meeting.get_scheduled_protocol_display()
        ),
        'answered_total': sum(p['answered'] for p in protocols),
        # Answers given on this call, as opposed to ones carried in from earlier
        # calls. Summarising reads this meeting's own answers, so offering it on
        # the strength of answers that belong to another call would write a
        # summary of the wrong conversation.
        'answered_here_total': sum(
            1 for p in protocols for q in p['questions'] if q['answer'] and q['mine']
        ),
        'question_total': sum(p['total'] for p in protocols),
        # The Protocols tab counts protocols, not questions, because protocols
        # are what the tab holds: a "12/12" over two cards, one of them
        # unfinished, was reporting on the wrong thing. The question totals stay
        # for the per-card pills, which do describe questions.
        'protocol_done_total': sum(1 for p in protocols if p['total'] and p['answered'] == p['total']),
        'protocol_total': len(protocols),
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
        # The written-up notes themselves, now that a note is a row with an
        # author and a time rather than one overwritten blob.
        'notes_list': _notes_for(meeting=meeting),
        'note_parent': 'meeting',
        'note_parent_id': meeting.pk,
        # Key moments and the segmented transcript belong to the recording
        # attached to this meeting, if there is one.
        'moments': _stamped(recording.transcript_moments) if recording else [],
        'segments': _stamped(recording.transcript_segments) if recording else [],
    }


def _stamp(seconds):
    """Seconds to m:ss. Formatted here rather than in the template so the
    transcript and the key moments cannot drift into two different clocks."""
    total = int(seconds or 0)
    return "%d:%02d" % divmod(total, 60)


def _stamped(rows, key="start"):
    return [dict(r, stamp=_stamp(r.get(key))) for r in rows or []]


def _notes_for(**parent):
    """The notes on one parent, newest first, ready for the panel.

    Author is rendered here rather than in the template so a note written before
    there was an author field still reads sensibly instead of showing a blank.
    """
    rows = (Note.objects
            .filter(**parent)
            .select_related('author')
            .order_by('-created_at'))
    return [{
        'id': n.pk,
        'body': n.body,
        'author': (n.author.get_full_name() or n.author.username) if n.author else _("Unknown"),
        'when': n.created_at,
        'edited': n.updated_at and n.created_at and (n.updated_at - n.created_at).total_seconds() > 1,
    } for n in rows]


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
        # The moments pulled out of the transcript, each anchored to a segment
        # so its timestamp points at audio that exists. Falls back to the bare
        # fact of the call when nothing has been extracted yet.
        'points': [
            {'text': m.get('text', ''),
             'stamp': "%d:%02d" % divmod(int(m.get('start') or 0), 60),
             'at': m.get('start') or 0}
            for m in (rec.transcript_moments or [])
        ] or [
            {'text': _("Called %s") % rec.to_number, 'stamp': f"{mins}:{secs:02d}"},
        ],
        'segments': _stamped(rec.transcript_segments),
        'notes_list': _notes_for(recording=rec),
        'note_parent': 'recording',
        'note_parent_id': rec.recording_sid,
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
        'notes_list': _notes_for(conversation=conv) if conv else [],
        'note_parent': 'conversation',
        'note_parent_id': str(conv.id) if conv else '',
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


@login_required
def panel_fragment(request):
    """The panel on its own, so opening one costs a fetch and not a page.

    Every page that hosts a panel already builds it through ``panel_context``;
    this renders the same thing without the shell around it, for base.html to
    swap in. The querystring it is called with is the one the row's own link
    carries, so ``item`` — and any filter or sort alongside it — is read here
    exactly as a full navigation would have read it.

    ``in_client`` is the client whose page the panel is being opened on. It
    does what ``in_client_page_for`` does for the Communications view: drops the
    context strip, because a page that is already about one person does not need
    a row explaining who they are. Checked against the same permission as the
    item itself, so it cannot be used to probe for clients.

    204 rather than 404 when nothing resolves: a stale or unreadable token is
    not an error, it is a panel with nothing to show. The caller closes.
    """
    for_patient = None
    want = (request.GET.get('in_client') or '').strip()
    if want.isdigit():
        candidate = Patient.objects.filter(pk=int(want)).first()
        if _can_see(request.user, candidate):
            for_patient = candidate

    ctx = panel_context(request, in_client_page_for=for_patient)
    if not ctx:
        return HttpResponse(status=204)
    return render(request, '_detail_panel.html', ctx)
