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
from django.core.exceptions import ObjectDoesNotExist

__all__ = ['resolve_panel_item', 'panel_context', 'panel_fragment']


# How many Message rows a conversation pane will render. High enough that a
# normal day of chat is never clipped, low enough that a runaway thread cannot
# put thousands of bubbles in the DOM. When it does clip, the pane says so —
# the previous limit of 20 dropped the rest silently, which read as the
# conversation simply stopping mid-thread.
PANEL_MSG_LIMIT = 200


# How far from a meeting a recording can start and still be that meeting's
# recording. An hour and a half either side: calls start late and run long,
# but a recording further out belongs to a different conversation.
#
# Measured from Meeting.happened_at, never from scheduled_time. The two differ
# exactly when a call did not happen when it was booked — placed early from
# the panel, or retried a day late — and that is precisely the case where the
# recording was being missed: the gap was measured against a booking the call
# had already departed from, so the audio was ruled unrelated to the call it
# came from. build_patient_events matches on the same anchor and window.
RECORDING_MATCH_WINDOW = dt.timedelta(seconds=5400)


def fold_recordings(meetings, recordings):
    """Which recordings belong to which call. One answer, used by both surfaces.

    The timeline row and the panel it opens have to agree about this, and while
    each worked it out for itself they did not: the row claimed the nearest
    free call, the panel took the earliest recording in a window and knew
    nothing about what another call had already claimed. The same audio could
    be a loose row and a call's recording at once, which is the duplicate
    everyone was actually looking at.

    A call holds *every* recording it produced, not one. A number that rings
    out and is redialled leaves a few seconds of ringing tone behind on each
    attempt, and all of them belong to the call that was being attempted —
    keeping one and orphaning the rest is what filled the list with loose
    "Call recording" rows that had a perfectly good call to sit under.

    Returns ``({meeting_pk: [recording, ...]}, [orphan, ...])``, each call's
    list longest first.
    """
    by_pk = {mt.pk: mt for mt in meetings}
    # A cancelled call is one that deliberately did not happen, so it is not
    # somewhere a recording can be *guessed* onto. One that names a cancelled
    # meeting outright is still folded onto it: that is not a guess, it is a
    # call that was placed and then called off afterwards.
    claimable = [mt for mt in meetings if mt.status != Meeting.Status.CANCELLED]

    claimed, orphans, guesses = {}, [], []

    # What the call wrote down when it was placed beats anything measured, so
    # these are settled first and a guess can never take a slot an exact answer
    # is about to fill.
    for rec in recordings:
        mt = by_pk.get(rec.meeting_id) if rec.meeting_id else None
        if mt is None:
            guesses.append(rec)
        else:
            claimed.setdefault(mt.pk, []).append(rec)

    def _nearest(rec, pool):
        """The closest call in `pool`, or None if the closest is still too far.

        See RECORDING_MATCH_WINDOW for why happened_at is the anchor and not
        the booking. The pk breaks ties so the fold is stable rather than
        depending on which call the database handed over first.
        """
        if not pool:
            return None
        gap, _tie, mt = min(
            (abs((m.happened_at - rec.start_time).total_seconds()), m.pk, m) for m in pool
        )
        return mt if gap <= RECORDING_MATCH_WINDOW.total_seconds() else None

    # Then legacy rows, and calls placed outside the platform, matched on time.
    for rec in guesses:
        # A call with nothing on it is the better home for a guess: one call
        # usually means one recording, so spreading guesses out beats piling
        # them onto a call that already has a definite answer while the call
        # next to it has none. Only when every nearby call is spoken for does a
        # guess join one — and that is the redialled call, whose attempts all
        # belong to the same booking however many of them there are.
        mt = (_nearest(rec, [m for m in claimable if m.pk not in claimed])
              or _nearest(rec, claimable))
        if mt is None:
            orphans.append(rec)
        else:
            claimed.setdefault(mt.pk, []).append(rec)

    # Longest first, because that is the one someone came to hear. Leading with
    # a four-second recording of a ringing tone buries the conversation under
    # the attempts that failed to reach it.
    for rows in claimed.values():
        rows.sort(key=lambda r: (-(r.duration or 0), r.start_time))
    return claimed, orphans


def _overview_meta(kind, obj, pk, generated_at):
    """Where an edit of this overview posts to, and whose words it now is.

    The overview is the one block a panel draws in violet, and the violet means
    "the model wrote this". A navigator who was on the call often knows better
    than the model what the call was about, so the text is editable — but once
    they rewrite it the claim stops being true, and the panel has to stop
    making it. SummaryEdit is what the byline and the styling read.
    """
    if obj is None:
        return {}
    try:
        edit = obj.summary_edit
    except ObjectDoesNotExist:
        edit = None
    return {
        'overview_edit_url': reverse('edit_overview', args=[kind, pk]),
        'overview_edited': edit is not None,
        'overview_by': ((edit.author.get_full_name() or edit.author.username)
                        if edit and edit.author else ''),
        'overview_when': edit.edited_at if edit else generated_at,
    }


def _can_see(user, patient):
    return bool(patient) and (is_admin(user) or patient.navigator_id == user.id)


def _patient_phones(patient):
    nums = []
    if patient.phone_number:
        nums.append(str(patient.phone_number))
    if patient.caregiver and patient.caregiver.phone_number:
        nums.append(str(patient.caregiver.phone_number))
    return nums


# The kind a row is, in the list's own vocabulary, and the glyph that goes with
# it. A panel carries the type of the row that opened it — same word, same
# tint, same icon — so colour means "what kind of thing this is" in both places
# and opening something never changes its colour. Urgency is the tag beside the
# title and nothing else; it used to be the kicker as well, which left an
# overdue call blue in the list and red in the panel.
PANEL_TYPE_ICON = {
    'call': 'call',
    'visit': 'event',
    'chat': 'forum',
    'alert': 'notification_important',
    'other': 'graphic_eq',
}


def _caregiver_role(caregiver):
    """How to name a caregiver's part in a sentence.

    The relationship is free text — "daughter", "neighbour", "paid carer" — and
    it is what a navigator actually recognises, so it is said alongside the
    role rather than instead of it.
    """
    rel = (getattr(caregiver, 'relationship', '') or '').strip()
    return _("caregiver, %(rel)s") % {'rel': rel} if rel else _("caregiver")


def _context_strip(patient):
    """The one-line history summary shown when triaging away from the client."""
    nums = _patient_phones(patient)

    # By relation where there is one, by number where there is not, and never
    # the navigator's own leg of a conference — see CallRecording.for_patient.
    # Counting by number alone both over- and under-counted: a shared number
    # added someone else's calls, and a client with no number of their own
    # counted none of their own.
    recs = CallRecording.for_patient(patient, nums)
    calls = recs.count()
    chat_days = (
        Message.objects.filter(user__in=nums)
        .annotate(day=TruncDate('timestamp')).values('day').distinct().count()
        if nums else 0
    )
    open_alerts = patient.alerts.exclude(status=Alert.AlertStatus.RESOLVED).count()

    # Last contact = the most recent thing that actually happened, whichever
    # channel it came through.
    stamps = []
    # for_patient is already newest-first.
    last_rec = recs.values_list('start_time', flat=True).first()
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

    linked_total = linked.count()

    # An alert the classifier raised carries two different pieces of writing,
    # and the panel must not run them together. The trigger is why somebody is
    # being interrupted right now — it goes above the fold, in the alert's own
    # colour. The description is the summary of the exchange, which is the
    # generated overview every other panel already knows how to draw and edit.
    #
    # An alert a person raised has neither: its description is the note they
    # typed, and offering to "edit the generated summary" of something they
    # wrote by hand would be nonsense. So the edit URL is withheld and the
    # block stays exactly as read-only as it was.
    from_classifier = (alert.alert_type == Alert.AlertType.CONVERSATION
                       and bool(data.get('detector')))
    trigger_at = None
    if from_classifier:
        raw_at = data.get('trigger_at') or ''
        if raw_at:
            try:
                trigger_at = dt.datetime.fromisoformat(raw_at)
            except (TypeError, ValueError):
                trigger_at = None
    overview_meta = (_overview_meta('alert', alert, alert.pk, alert.created_at)
                     if from_classifier and alert.description else {})

    return {
        **bot,
        **overview_meta,
        'kind': 'alert',
        'messages': list(linked[:PANEL_MSG_LIMIT]),
        'message_count': linked_total,
        'messages_clipped': linked_total > PANEL_MSG_LIMIT,
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
            # trigger and trigger_at are the block at the top of the Summary
            # tab. Repeating a sentence in Technical is how a panel teaches
            # people to stop reading it.
            if k not in ('archive_bucket', 'trigger', 'trigger_at')
            and not isinstance(v, (dict, list))
        ),
        'trigger': data.get('trigger') or '',
        'trigger_at': trigger_at,
        'detector': data.get('detector') or '',
        # Not "Alert \u00b7 high": the tag beside the title says high, in colour.
        'kicker': _("Alert"),
        'type': 'alert',
        'type_icon': PANEL_TYPE_ICON['alert'],
        'state': state,
        'source': alert.get_alert_type_display(),
        'title': display_label(alert.title) or _("Alert"),
        'when': alert.created_at,
        'patient': alert.patient,
        'overview_heading': _("Summary") if from_classifier else _("Description"),
        'overview': alert.description,
        'points': points,
        'alert': alert,
        'detail_url': reverse('alert_detail', args=[alert.pk]),
    }


def _sms_offer(request, meeting):
    """The pending SMS offer for this meeting, or None.

    ``start_protocol_automation`` parks one here when WhatsApp refuses the
    opening message — most often because the caregiver has not replied inside
    WhatsApp's 24-hour window, which no amount of retrying fixes. The automation
    has already been reverted at that point, so this is an offer to start again
    over SMS, not a half-sent state.
    """
    offer = (getattr(request, 'session', None) or {}).get('ask_sms_offer')
    if not offer or offer.get('meeting') != meeting.pk:
        return None
    return offer


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
    """This client's protocols, split into the ones this call is for and the rest.

    Two lists rather than one. Which protocols apply to a client is now recorded
    on the client (`Patient.protocols`) instead of being "every protocol that
    exists", and which of those a call is for is recorded on the call
    (`Meeting.scheduled_protocols`) instead of being flagged and left in place.

    A protocol booked for this call but no longer on the client's programme —
    a one-off, picked at scheduling — still appears above. It just does not
    appear below, and it will not be back on the next call unless someone ticks
    it on the profile.
    """
    programme = list(
        meeting.patient.protocols
        .filter(questions__isnull=False)
        .prefetch_related('questions')
        .distinct()
        .order_by('number')
    )
    booked = list(
        meeting.scheduled_protocols
        .filter(questions__isnull=False)
        .prefetch_related('questions')
        .distinct()
        .order_by('number')
    )

    seen = {p.pk for p in programme}
    relevant = programme + [p for p in booked if p.pk not in seen]
    if not relevant:
        return [], []

    booked_ids = {p.pk for p in booked}

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
    #
    # A repeatable protocol is the exception: it asks again rather than carrying
    # forward, so `rounds` below keeps the earlier calls' answers separately.
    # See _protocol_card.
    answers = {}
    rounds = {}
    for a in (Answer.objects
              .filter(meeting__patient=meeting.patient,
                      question__protocol__in=relevant)
              .select_related('meeting')
              .order_by('meeting__scheduled_time', 'pk')):
        answers[a.question_id] = {
            'text': a.response,
            'by_text': a.by_text,
            'mine': a.meeting_id == meeting.pk,
            'when': a.meeting.happened_at,
        }
        # Earlier calls' answers, kept per question in the order they were given.
        # Only read for repeatable protocols; gathered for everything so the
        # queryset above stays a single pass.
        if a.meeting_id != meeting.pk:
            rounds.setdefault(a.question_id, []).append({
                'when': a.meeting.happened_at,
                'text': a.response,
            })

    session, others = [], []
    for p in relevant:
        card = _protocol_card(p, answers, rounds, p.pk in booked_ids)
        (session if p.pk in booked_ids else others).append(card)

    return session, others


def _protocol_card(p, answers, rounds, is_scheduled):
    """One protocol as the panel needs it, in whichever list it lands in."""
    repeatable = p.repeatable

    questions = []
    for q in p.questions.all():
        answer = answers.get(q.id, {})
        # A repeatable protocol asks again rather than carrying forward: this
        # call gets an empty field, and what was said before sits under it as a
        # record. Reusing `mine` would have shown last round's words in the box
        # you are about to type today's into.
        mine = answer.get('mine', True)
        text = answer.get('text', '')
        if repeatable and not mine:
            text = ''
        questions.append({
            'id': q.id,
            # ProtocolAnswerForm names its fields q_<id>; autosave posts the
            # whole protocol back to protocol_view, so these must match.
            'field': f'q_{q.id}',
            'prompt_md': q.prompt_md,
            'answer': text,
            'by_text': answer.get('by_text', False) and (mine or not repeatable),
            # False when the answer was given on an earlier call: shown as a
            # record rather than a field, so saving this call cannot quietly
            # copy someone else's call into it. Always True on a repeatable
            # protocol — this call always writes its own round.
            'mine': True if repeatable else mine,
            'when': answer.get('when'),
            'rounds': rounds.get(q.id, []) if repeatable else [],
        })

    # What counts as answered depends on what the protocol is.
    #
    # A one-off is done once, by anyone, on any of this client's calls — which
    # is what makes it read as done everywhere once it is done once. A
    # repeatable one is only ever done *for this round*, so earlier rounds must
    # not fill the bar; otherwise the IQCODE would arrive at every future call
    # already complete and never be asked again.
    answered = sum(1 for q in questions if q['answer'])
    carried = sum(1 for q in questions if q['answer'] and not q['mine'])

    prior = [r for q in questions for r in q['rounds']]
    last_round = max((r['when'] for r in prior if r['when']), default=None)
    round_no = len({r['when'] for r in prior if r['when']}) + 1 if repeatable else 0

    return {
        'by_text_count': sum(1 for q in questions if q['by_text'] and q['answer']),
        'carried_count': carried,
        'number': p.number,
        'title': p.title,
        'description': p.description,
        'questions': questions,
        'answered': answered,
        'total': len(questions),
        'is_scheduled': is_scheduled,
        'started': answered > 0,
        'complete': answered == len(questions) and bool(questions),
        # Repeatable protocols say something different on the pill: "Complete"
        # is the wrong word for a questionnaire designed to be re-taken.
        'repeatable': repeatable,
        'round_no': round_no,
        'last_round': last_round,
        'prior_rounds': len({r['when'] for r in prior if r['when']}),
    }


def _protocol_options(meeting):
    """Every protocol in the platform, this client's own first.

    The pickers used to be built from Meeting.Protocol — ten placeholder labels
    that were never anybody's protocol. They are built from the Protocol table
    now, so nothing is offered that does not exist and nothing that exists is
    missing.

    Protocols outside the client's programme stay pickable rather than being
    hidden: running a one-off is a real thing to want, and having to edit a
    profile first would mean editing it back afterwards. They are marked, so
    picking one is a choice rather than an accident.
    """
    programme = set(meeting.patient.protocols.values_list('pk', flat=True))
    booked = set(meeting.scheduled_protocols.values_list('pk', flat=True))
    done = set(meeting.executed_protocols.values_list('pk', flat=True))

    options = [
        {
            'pk': p.pk,
            'number': p.number,
            'title': p.title,
            'on_programme': p.pk in programme,
            'scheduled': p.pk in booked,
            # What to tick when recording the outcome: what was actually
            # covered if that has been recorded already, otherwise what the
            # call was booked for.
            'executed': p.pk in (done or booked),
        }
        for p in Protocol.objects.order_by('number')
    ]
    options.sort(key=lambda o: (not o['on_programme'], o['number']))
    return options


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
    protocols_session, protocols_other = _meeting_protocols(meeting)
    # The aggregates below count what is on screen, which is both lists.
    protocols = protocols_session + protocols_other
    is_pending = meeting.status == Meeting.Status.PENDING
    in_person = meeting.modality == Meeting.Modality.IN_PERSON

    # The recording belongs to this meeting rather than sitting beside it in the
    # list, so the panel is where it is played. Worked out by the same fold the
    # list runs, over the same set of this client's recordings, so the panel and
    # the row that opened it cannot disagree about which audio is whose.
    meeting_recordings = fold_recordings(
        list(meeting.patient.meetings.all()),
        list(CallRecording
             .for_patient(meeting.patient, _patient_phones(meeting.patient))
             .select_related('meeting')),
    )[0].get(meeting.pk, [])
    recording = meeting_recordings[0] if meeting_recordings else None

    # Who the phone actually rings, said before the press rather than after it.
    #
    # make_phone_call bridges the navigator and the caregiver: the client whose
    # name is at the top of this panel is not on the call. The panel admitted
    # that only in the dialling label, which appears once it is already ringing,
    # and the two reasons a call cannot be placed lived in a title= on a
    # disabled button — discoverable by hovering something that looks dead. Both
    # reasons are the ones make_phone_call itself would return, so the panel now
    # says up front what the POST would have said.
    # Which of the client's two numbers this call rings, and whether there is
    # one to ring. Both come off the meeting rather than being assumed to be the
    # caregiver: since the Call button on the client page, a call can be to the
    # client themself, and a panel still saying "To: Ana" over a call that rang
    # Manuel would be describing a different call.
    rings_client = meeting.dial_target == Meeting.DialTarget.CLIENT
    recipient = meeting.dial_recipient

    if in_person:
        # Nobody is dialled, so this is not a recipient — it is who the meeting
        # was arranged with, which is the same person the reminder goes to. Left
        # unsaid entirely when there is no caregiver: nothing records who turns
        # up to a visit, and guessing is worse than a quiet header.
        to = {
            'icon': 'group',
            'warn': False,
            'lead': _("Arranged with"),
            'who': str(caregiver),
            'role': _caregiver_role(caregiver),
            'phone': '',
            'note': _("Nobody is dialled — you turn up"),
        } if caregiver else None
    elif recipient is None:
        to = {
            'icon': 'phone_disabled',
            'warn': True,
            'lead': (_("This client has no number of their own, so this call cannot be placed.")
                     if rings_client
                     else _("No caregiver with a phone number, so this call cannot be placed.")),
            'who': '', 'role': '', 'phone': '',
            'note': _("Add one on the client"),
        }
    elif not request.user.phone_number:
        to = {
            'icon': 'phone_disabled',
            'warn': True,
            'lead': _("Your own phone number is not set, so the call cannot be placed."),
            'who': '', 'role': '', 'phone': '',
            'note': _("Add it on your profile"),
        }
    else:
        to = {
            'icon': 'phone_forwarded',
            'warn': False,
            'lead': _("To"),
            'who': str(recipient),
            'role': _("the client") if rings_client else _caregiver_role(caregiver),
            'phone': str(recipient.phone_number),
            # Nothing to add when the client is the one being rung: the panel is
            # headed with their name and the line above now names them again.
            # The note exists for the other case, where the name at the top of
            # the panel belongs to someone who is not on the call.
            'note': ('' if rings_client
                     else _("%(c)s is not on this call") % {'c': meeting.patient.name}),
        }

    return {
        'kind': 'meeting',
        'type': 'visit' if in_person else 'call',
        'type_icon': PANEL_TYPE_ICON['visit' if in_person else 'call'],
        'to': to,
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
            # "Scheduled call" is the one thing an unscheduled one is not, and
            # the distinction is worth keeping after it has ended too: a call
            # nobody booked is a different account of the day than one that was.
            else ((_("Unscheduled call") if meeting.unscheduled else _("Scheduled call"))
                  if is_pending else _("Call ended"))
        ),
        # Named after whoever is actually on it. This said the caregiver even
        # for a call placed to the client, which is the wrong name on the one
        # line the panel leads with.
        'title': (
            ((_("Meeting with %s") % caregiver) if in_person and caregiver
             else (_("Call with %s") % recipient) if not in_person and recipient
             else (_("Meeting with %s") % caregiver) if caregiver
             else meeting.get_type_display())
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
        # The attempts that came before it. Folding every recording onto the
        # call it came from is only an improvement if they are all reachable
        # here — hiding the extra ones behind the longest would lose exactly
        # what the loose rows were keeping alive. The first is the one the
        # player above is already holding, so only the rest are listed.
        'other_recordings': [{
            'url': reverse('serve_protected_file', args=[r.recording_sid]),
            'len': _stamp(r.duration),
            'when': r.start_time,
        } for r in meeting_recordings[1:]],
        # Both were only reachable from a page nothing links to any more.
        'transcribe_url': (reverse('transcribe_recording', args=[recording.recording_sid])
                           if recording else ''),
        'summarize_url': reverse('summarize_meeting', args=[meeting.pk]),
        'overview_heading': _("Overview"),
        'overview': meeting.protocol_summary,
        **_overview_meta('meeting', meeting, meeting.pk, meeting.protocol_summarized_at),
        'recording_len': (
            "%d:%02d" % divmod(recording.duration or 0, 60) if recording else ''
        ),

        # Start call bridges the navigator's own phone to whoever this call is
        # with, so both numbers have to exist before the button means anything.
        'can_call': bool(not in_person and recipient and request.user.phone_number),
        # Who the dialling label and the app-wide call bar name. Empty when
        # there is nobody to ring, which is also when there is no button.
        'dial_who': str(recipient) if recipient else '',
        # Whether a reminder has anywhere to go depends on the channel the
        # platform is configured for, so the check lives with the sending code
        # rather than being a phone-number test repeated here.
        'can_remind': can_send_reminder(meeting),
        'last_call': last_call,
        'protocols': protocols,
        # The two headings the panel renders: what this call is for, then the
        # rest of the client's programme.
        'protocols_session': protocols_session,
        'protocols_other': protocols_other,
        'automation': _automation_state(meeting, protocols),
        # The "WhatsApp could not reach them — send it by SMS instead?" offer,
        # if the navigator looking at this panel is the one who just hit that
        # failure on this meeting. Read from the session rather than stored on
        # the meeting: it is one person's unfinished decision, not a fact about
        # the call, so it must not appear in a colleague's panel.
        'sms_offer': _sms_offer(request, meeting),
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
        # What the call is booked to cover, in one line, for the sentence that
        # confirms what is being recorded as done.
        'scheduled_title': ", ".join(
            f"{p['number']}. {p['title']}" for p in protocols_session
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
        # Every protocol in the platform, this client's own first, each
        # carrying whether it is on their programme and whether this call is
        # already booked for it. One list, used by both the reschedule dialog
        # and the picker that records what was covered.
        'protocol_options': _protocol_options(meeting),
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
        'segments': _readable_transcript(recording.transcript_segments) if recording else [],
    }


def _stamp(seconds):
    """Seconds to m:ss. Formatted here rather than in the template so the
    transcript and the key moments cannot drift into two different clocks."""
    total = int(seconds or 0)
    return "%d:%02d" % divmod(total, 60)


def _stamped(rows, key="start"):
    return [dict(r, stamp=_stamp(r.get(key))) for r in rows or []]


# A gap this long between segments reads as a change of turn rather than a
# breath. Under it, two segments are the same thought and belong together.
TRANSCRIPT_PAUSE = 2.0
# Past this a block is a wall of text even if nobody paused, so it breaks at
# the next sentence end rather than mid-thought.
TRANSCRIPT_SOFT_CHARS = 320
# And past this it breaks regardless: audio with no clear sentence endings —
# a bad line, a language Whisper is unsure of — must not produce one endless
# paragraph with a single timestamp on it.
TRANSCRIPT_HARD_CHARS = 900

_SENTENCE_END = ('.', '!', '?', '…', '。', '！', '？', '."', ".'", '?"', '!"')


def _readable_transcript(rows):
    """Whisper's time windows, rejoined into something a person can read.

    Whisper returns fixed-length windows, not sentences, so a segment routinely
    ends mid-word and the next one carries the rest. Rendered one-per-row that
    is what produced "I have some to ask some basic information" and "request."
    as two entries with two timestamps — a transcript that does not follow the
    call because it is not divided where the call is.

    Segments are joined until the text actually finishes a sentence *and* there
    is a reason to break: the speaker changed, they paused, or the block has
    grown into a paragraph. A change of speaker breaks wherever it lands, since
    that is a real boundary regardless of punctuation.

    Done here rather than at transcription time on purpose: every transcript
    already stored reads better immediately, with nothing re-transcribed and no
    audio fetched again.
    """
    blocks = []
    for row in rows or []:
        text = (row.get('text') or '').strip()
        if not text:
            continue
        start = float(row.get('start') or 0.0)
        end = float(row.get('end') or start)
        speaker = row.get('speaker')

        cur = blocks[-1] if blocks else None
        if cur is not None and cur['speaker'] == speaker:
            grown = len(cur['text'])
            ends_sentence = cur['text'].endswith(_SENTENCE_END)
            paused = (start - cur['end']) >= TRANSCRIPT_PAUSE
            enough = grown >= TRANSCRIPT_SOFT_CHARS
            if not ((ends_sentence and (paused or enough)) or grown >= TRANSCRIPT_HARD_CHARS):
                cur['text'] = "%s %s" % (cur['text'], text)
                cur['end'] = end
                continue

        blocks.append({'text': text, 'start': start, 'end': end, 'speaker': speaker})

    return [dict(b, stamp=_stamp(b['start'])) for b in blocks]


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
    rec = (CallRecording.objects
           .select_related('patient__caregiver', 'patient__navigator', 'meeting')
           .filter(pk=pk).first())
    if not rec:
        return None
    patient = rec.resolve_patient()
    # Who may reach the audio is a wider question than whose it is: a legacy
    # recording on a number two clients share has no single owner, and the
    # navigator of either could reach it before this existed. owner_patients
    # keeps that, while resolve_patient above names one for the header.
    if not (is_admin(request.user)
            or rec.owner_patients().filter(navigator=request.user).exists()):
        return None

    mins, secs = divmod(rec.duration or 0, 60)
    # Which of the client's two numbers this was. The recording stores a bare
    # to_number and the query above already matched it to a person, so the
    # panel can name them instead of leaving a phone number to be recognised.
    # There is nobody to name when the recording has no client: the navigator's
    # own leg has a staff number on it, and a legacy row can match nobody at
    # all, so resolve_patient returns None for both.
    cg = patient.caregiver if patient else None
    if cg and cg.phone_number and str(cg.phone_number) == rec.to_number:
        to = {'who': str(cg), 'role': _caregiver_role(cg)}
    elif patient and patient.phone_number and str(patient.phone_number) == rec.to_number:
        to = {'who': f"{patient.name} {patient.lastname}".strip(), 'role': _("the client")}
    else:
        to = None
    if to:
        to.update({'icon': 'phone_in_talk', 'warn': False,
                   'lead': _("Called"), 'phone': rec.to_number, 'note': ''})

    # The navigator's own leg never appears in a list, so this is only reached
    # by opening it directly. Saying which side it is beats letting it look
    # like a call with the client, which is the confusion this whole change is
    # about.
    return {
        'kind': 'recording',
        'kicker': _("Your own line") if rec.is_navigator_leg else _("Phone call"),
        'type': 'other',
        'type_icon': PANEL_TYPE_ICON['other'],
        'to': to,
        'title': _("Call recording"),
        'when': rec.start_time,
        'patient': patient,
        'overview_heading': _("Transcript summary") if rec.transcript_summary else '',
        'overview': rec.transcript_summary,
        **_overview_meta('recording', rec, rec.recording_sid, rec.transcribed_at),
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
        'segments': _readable_transcript(rec.transcript_segments),
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

    # The same predicate the timeline counts with: phone number OR one of this
    # client's own conversations. Matching on phone alone made the pane
    # disagree with the row that opened it — a client reached through the web
    # tester or voice chat has a username in Message.user rather than a number,
    # and a client with no phone at all fell into the `else` and got an empty
    # queryset by construction. The row said "76 messages" and the pane said
    # none were recorded that day; both were reading the same day of the same
    # client through different filters.
    msgs = (Message.objects
            .filter(patient_message_q(patient), timestamp__date=day)
            .order_by('timestamp'))
    conv = (Conversation.objects.filter(patient=patient, last_message_at__date=day)
            .order_by('-last_message_at').first())

    # Failing that, the id the day's own messages carry.
    #
    # The two tables are reached by different routes — messages by phone
    # number, conversations by client FK — and they disagree often: most
    # Conversation rows have no patient set at all, so filtering by client
    # alone misses the very thread the messages name. That left the panel with
    # no conversation, an empty note_parent_id, and a NoReverseMatch that took
    # the page down rather than the notes box.
    #
    # A thread already owned by a *different* client is deliberately not
    # accepted: writing this client's notes onto another client's conversation
    # is worse than having nowhere to write them. Those days render read-only.
    if conv is None:
        ids = set()
        for raw in msgs.values_list('conversation_id', flat=True).distinct():
            try:
                ids.add(UUID(str(raw)))
            except (TypeError, ValueError):
                continue
        if ids:
            conv = (Conversation.objects
                    .filter(id__in=ids)
                    .filter(Q(patient=patient) | Q(patient__isnull=True))
                    .order_by('-last_message_at')
                    .first())

    # What the agent watches for on this client, so the navigator's yes/no has
    # something to answer. Empty until an agent defines detectors, and the
    # review renders without the section rather than with an empty one.
    labels = []
    if conv and conv.agent and isinstance(getattr(conv.agent, 'detectors', None), dict):
        labels = list(conv.agent.detectors.keys())
    human = conv.human_flags if conv and isinstance(conv.human_flags, dict) else {}
    auto = conv.auto_flags if conv and isinstance(conv.auto_flags, dict) else {}
    flags = [{'label': l, 'checked': bool(human.get(l, auto.get(l)))} for l in labels]

    # "Started" is the first message of the day. It used to be
    # ``datetime.combine(day, time.min)``, which is midnight by construction,
    # so every conversation claimed to have started at 00:00 no matter when it
    # actually did.
    #
    # conv.started_at is only the fallback, and deliberately so: the pane is
    # one *day* of a thread, and a conversation that ran through midnight
    # started before the day being shown. Preferring the first message keeps
    # the header agreeing with the first timestamp underneath it.
    first_msg = msgs.first()
    last_msg = msgs.last()
    total = msgs.count()
    started = (first_msg.timestamp if first_msg
               else conv.started_at if conv
               else timezone.make_aware(dt.datetime.combine(day, dt.time.min)))

    # Who wrote in. The panel is headed with the client's name, but a thread on
    # a client's file is as often the caregiver's — and nothing on screen said
    # which.
    #
    # Only the two known numbers can answer it: Message.user holds a phone
    # number for WhatsApp and SMS traffic, but a username for the web tester
    # and the voice page (see patient_message_q). A sender that matches neither
    # leaves the line off rather than guessing, and a day both of them wrote on
    # says so instead of picking one.
    senders = {s for s in msgs.values_list('user', flat=True).distinct() if s}
    cg = patient.caregiver
    client_name = f"{patient.name} {patient.lastname}".strip()
    wrote_client = bool(patient.phone_number and str(patient.phone_number) in senders)
    wrote_cg = bool(cg and cg.phone_number and str(cg.phone_number) in senders)
    if wrote_client and wrote_cg:
        to = {'who': _("%(client)s and %(cg)s") % {'client': client_name, 'cg': cg},
              'role': _("the client and the caregiver")}
    elif wrote_cg:
        to = {'who': str(cg), 'role': _caregiver_role(cg)}
    elif wrote_client:
        to = {'who': client_name, 'role': _("the client")}
    else:
        to = None
    if to:
        to.update({'icon': 'chat_bubble_outline', 'warn': False,
                   'lead': _("Written by"), 'phone': '', 'note': ''})

    return {
        'kind': 'chat',
        'type': 'chat',
        'type_icon': PANEL_TYPE_ICON['chat'],
        'to': to,
        'notes_list': _notes_for(conversation=conv) if conv else [],
        'note_parent': 'conversation',
        'note_parent_id': str(conv.id) if conv else '',
        'kicker': _("Chatbot"),
        'title': conv.topic if conv and conv.topic else _("Conversation"),
        'when': started,
        'patient': patient,
        # The summary the conversation itself carries. It is written by the
        # classifier onto Conversation.summary and was already being drawn here
        # — but only when it existed, so a thread nobody has analysed yet drew
        # no block at all, and the Summary tab looked broken rather than empty.
        # The heading stands either way now, and the absence is said out loud;
        # the Edit inside the block is what lets a navigator write one by hand,
        # which was unreachable while the block itself was conditional.
        'overview_heading': _("Summary") if conv else '',
        'overview': conv.summary if conv else '',
        'overview_empty': (_("Not summarised yet.")
                           if conv and not conv.summary else ''),
        **_overview_meta('conversation', conv, str(conv.id) if conv else '',
                         conv.analyzed_at if conv else None),
        'points': [],
        'message_count': total,
        'messages': msgs[:PANEL_MSG_LIMIT],
        'messages_clipped': total > PANEL_MSG_LIMIT,
        # The Summary tab is otherwise one paragraph. This is the same detail
        # the old conversation page carried in its header, as a list.
        'facts': [
            (_("Messages"), total),
            (_("Between"), "%s – %s" % (
                timezone.localtime(first_msg.timestamp).strftime("%H:%M"),
                timezone.localtime(last_msg.timestamp).strftime("%H:%M"),
            ) if first_msg else _("—")),
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
