"""The Communications and Clients pages, built for every client at once.

Both pages used to ask the database five or six times per client. They now ask
a fixed handful of times for all of them (build_events_by_patient,
last_communications, message_attribution.per_patient_aggregates). Correctness
is held to the old code: the per-client builders as they stood before the
change are frozen below, and every client's events and last contact must come
out identical — including the awkward cases: legacy messages found by number
or by conversation, a number two clients share, recordings that name nobody,
the navigator's own leg, cancelled calls.

    python3 manage.py test ConvAI.test_comms_batching --settings=test_settings
"""
import datetime as dt
from datetime import timedelta

from django.contrib.auth.models import Group
from django.db import connection
from django.db.models import Count, Max
from django.db.models.functions import Coalesce, TruncDate
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from ConvAI.models import (Alert, CallRecording, Caregiver, ConvAIUser, Conversation, Meeting,
                           Message, Note, Patient, Protocol)
from ConvAI.utils import patient_message_q
from ConvAI.views._panel import fold_recordings
from ConvAI.views.patients import (_format_duration, build_events_by_patient, build_patient_events,
                                   last_communication, last_communications)


# ── The per-client builders as they were before batching (e11fc99) ─────────
# Frozen on purpose: the new code is correct when it agrees with these.

def _old_last_communication(patient, now=None):
    """The last time this client was actually in touch, by any channel.

    ``{'ts', 'kind'}`` with kind ``call``, ``visit`` or ``chat``, or None.

    *Actually in touch* is the rule each source is held to. A call counts once it
    connected (complete or interrupted) — not one that went unanswered, was
    cancelled or is still only in the diary, which is what the old Last call
    column showed and why a client nobody had reached for weeks could look
    current. A chat counts when the client or their caregiver wrote in (or a
    tester did, standing in for one); a reminder the platform sent on its own is
    not them using the chatbot. A recording made outside any booked call — a
    number dialled from a handset — is a call too.
    """
    now = now or timezone.now()
    found = []

    mt = (patient.meetings
          .filter(status__in=(Meeting.Status.COMPLETED, Meeting.Status.INTERRUPTED))
          .annotate(happened=Coalesce('ended_at', 'scheduled_time'))
          .filter(happened__lte=now)
          .order_by('-happened')
          .only('modality', 'ended_at', 'scheduled_time')
          .first())
    if mt:
        found.append((mt.happened, mt.kind))

    nums = [str(n) for n in (patient.phone_number,
                             patient.caregiver.phone_number if patient.caregiver else None) if n]
    rec = CallRecording.for_patient(patient, nums).filter(start_time__lte=now).first()
    if rec and rec.start_time:
        found.append((rec.start_time, 'call'))

    chat_ts = (Message.objects.filter(patient_message_q(patient), timestamp__lte=now)
               .exclude(sender_role=Message.SenderRole.PLATFORM)
               .exclude(user_message='')
               .order_by('-timestamp')
               .values_list('timestamp', flat=True)
               .first())
    if chat_ts:
        found.append((chat_ts, 'chat'))

    if not found:
        return None
    ts, kind = max(found, key=lambda f: f[0])
    return {'ts': ts, 'kind': kind}


def _old_build_patient_events(patient, protocol_numbers=None):
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

    # Phone-call recordings belonging to this client.
    #
    # A recording placed through this platform names its client outright; the
    # numbers are consulted only for rows made before it could. That distinction
    # is the whole point — a number shared between two clients used to draw the
    # same call on both their timelines, and the navigator's own leg of a
    # conference, which carries a staff number, was filed against whichever
    # client shared it. CallRecording.for_patient is where both rules live, and
    # it is also what leaves the navigator's leg out.
    #
    # A recording is not an event in its own right: it is something attached to
    # the meeting it came from, so it is folded onto that meeting below rather
    # than listed beside it. Anything that cannot be matched still gets its own
    # row, because a recording nobody can reach is worse than a duplicate.
    recordings = list(
        CallRecording.for_patient(patient, nums).select_related('meeting')
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
    # Both protocol relations are prefetched: the loop below reads them on
    # every meeting, and without this a client with fifty calls costs a hundred
    # queries to draw one timeline.
    meetings = list(
        patient.meetings
        .annotate(note_count=Count('notes_list'))
        .prefetch_related('executed_protocols', 'scheduled_protocols')
    )
    claimed, orphans = fold_recordings(meetings, recordings)

    # Whatever belongs to no call at all — a number dialled from a handset, an
    # inbound call nobody booked. Still a row, because a recording nobody can
    # reach is worse than a loose entry, and this is the only way to open one.
    for rec in orphans:
        events.append({
            'kind': 'recording',
            'ts': rec.start_time,
            'panel_token': f'recording-{rec.pk}',
            'patient': patient,
            **_rec_dict(rec),
        })

    # Scheduled / executed meetings — a phone call or an in-person visit.
    for mt in meetings:
        # What the call covered, or failing that what it was booked to cover.
        # A call can carry more than one now, so the row names them all rather
        # than picking the first and calling it the protocol.
        covered = list(mt.executed_protocols.all()) or list(mt.scheduled_protocols.all())
        proto_num = covered[0].number if len(covered) == 1 else None
        # Every recording this call produced, the substantive one first; the
        # row names that one and counts the rest.
        recs = claimed.get(mt.pk, [])
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
            'online': mt.modality == Meeting.Modality.ONLINE,
            # Placed on the spot rather than booked, and who it rang. The list
            # calls every pending call a "Scheduled call", which is the one
            # thing an unscheduled one is not — and names the caregiver on every
            # row, which is the wrong person once a call can go to the client.
            'unscheduled': mt.unscheduled,
            'dial_who': str(mt.dial_recipient or ''),
            'location': mt.location,
            'protocol': ", ".join(f"{p.number}. {p.title}" for p in covered),
            # Only linkable when the row names exactly one — a link has to go
            # somewhere, and two protocols have two somewheres.
            'protocol_num': proto_num,
            'protocol_summary': mt.protocol_summary,
            'has_notes': bool(mt.note_count),
            'recording': _rec_dict(recs[0]) if recs else None,
            'recording_count': len(recs),
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


# ── Fixtures ───────────────────────────────────────────────────────────────

SHARED = "+447700900001"   # a caregiver who looks after two clients
P3_PHONE = "+447700900003"


def _msg(ts, **kw):
    m = Message.objects.create(**{"conversation_id": "x", "user": "x", "user_message": "hi",
                                  "response_message": "hello", **kw})
    Message.objects.filter(pk=m.pk).update(timestamp=ts)
    return m


def _rec(start, **kw):
    import uuid
    return CallRecording.objects.create(recording_sid=f"RE{uuid.uuid4().hex}", start_time=start,
                                        end_time=start + timedelta(minutes=10), duration=600, **kw)


class _Data(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        now = self.now
        self.nav = ConvAIUser.objects.create_user(username="nav", password="x", phone_number="+447000000001")
        self.nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.admin = ConvAIUser.objects.create_superuser("root", "r@example.org", "x")
        pr1 = Protocol.objects.create(number=1, title="Check-in")
        pr2 = Protocol.objects.create(number=2, title="Follow-up")

        carer = Caregiver.objects.create(name="Ana", lastname="Ruiz", phone_number=SHARED)
        self.p1 = Patient.objects.create(name="Manuel", lastname="Ortega", caregiver=carer, navigator=self.nav)
        self.p2 = Patient.objects.create(name="Lucía", lastname="Ortega", caregiver=carer, navigator=self.nav)
        self.p3 = Patient.objects.create(name="Elena", lastname="Marchetti", phone_number=P3_PHONE,
                                         navigator=self.nav)
        self.p4 = Patient.objects.create(name="Tomás", lastname="Vidal", navigator=self.nav)
        self.p5 = Patient.objects.create(name="Empty", lastname="Client", navigator=self.nav)
        conv1 = Conversation.objects.create(patient=self.p1)
        conv4 = Conversation.objects.create(patient=self.p4)

        # Messages: attributed, platform-only, empty, legacy by number, by
        # conversation, by both (two different clients), not legacy, future.
        _msg(now - timedelta(days=3), patient=self.p1, conversation_id=str(conv1.id),
             sender_role=Message.SenderRole.CLIENT)
        _msg(now - timedelta(days=3, hours=2), patient=self.p1, conversation_id=str(conv1.id),
             sender_role=Message.SenderRole.CAREGIVER)
        _msg(now - timedelta(days=1), patient=self.p1, sender_role=Message.SenderRole.PLATFORM, user_message="")
        _msg(now - timedelta(hours=5), patient=self.p1, sender_role=Message.SenderRole.CLIENT, user_message="")
        _msg(now - timedelta(days=2), user=SHARED)                                    # P1 and P2
        _msg(now - timedelta(days=6), conversation_id=str(conv4.id))                  # P4
        _msg(now - timedelta(days=4), user=P3_PHONE, conversation_id=str(conv1.id))   # P3 and P1
        _msg(now - timedelta(days=4, hours=1), user=P3_PHONE)                         # P3, same day
        _msg(now - timedelta(days=1), user=SHARED, sender_role=Message.SenderRole.PROSPECT)  # nobody's
        _msg(now + timedelta(days=1), patient=self.p3, sender_role=Message.SenderRole.CLIENT)  # future

        # Meetings for P1 across every modality and status.
        def meeting(when, status, **kw):
            mt = Meeting.objects.create(patient=self.p1, scheduled_time=when, status=status, **kw)
            return mt
        self.m_phone = meeting(now - timedelta(days=10), Meeting.Status.COMPLETED,
                               ended_at=now - timedelta(days=10) + timedelta(minutes=25))
        self.m_phone.executed_protocols.add(pr1)
        self.m_phone.scheduled_protocols.add(pr1, pr2)
        Note.objects.create(meeting=self.m_phone, body="went well")
        self.m_visit = meeting(now - timedelta(days=7), Meeting.Status.COMPLETED,
                               modality=Meeting.Modality.IN_PERSON, location="Clinic",
                               ended_at=now - timedelta(days=7) + timedelta(hours=1))
        self.m_online = meeting(now - timedelta(days=5), Meeting.Status.INTERRUPTED,
                                modality=Meeting.Modality.ONLINE)
        self.m_cancel = meeting(now - timedelta(days=9), Meeting.Status.CANCELLED,
                                cancelled_at=now - timedelta(days=9, hours=2))
        self.m_late = meeting(now - timedelta(days=1), Meeting.Status.PENDING, retries=1)
        self.m_next = meeting(now + timedelta(days=2), Meeting.Status.PENDING)
        self.m_next.scheduled_protocols.add(pr2)
        Meeting.objects.create(patient=self.p3, scheduled_time=now + timedelta(days=1),
                               status=Meeting.Status.CANCELLED)

        # Recordings: attributed to a call, a guess by shared number near P1's
        # call (and a loose row for P2, who has no calls), the navigator's own
        # leg, one naming a cancelled call, and one belonging to no call.
        _rec(self.m_phone.scheduled_time + timedelta(minutes=1), patient=self.p1, meeting=self.m_phone)
        _rec(self.m_phone.scheduled_time + timedelta(minutes=3), patient=None, to_number=SHARED)
        _rec(self.m_phone.scheduled_time + timedelta(minutes=1), patient=self.p1, meeting=self.m_phone,
             leg=CallRecording.Leg.CTN)
        _rec(self.m_cancel.scheduled_time, patient=self.p1, meeting=self.m_cancel)
        _rec(now - timedelta(days=40), patient=self.p3)
        _rec(now - timedelta(hours=1), patient=None, to_number=P3_PHONE)

        a = Alert.objects.create(patient=self.p1, title="Low mood", description="d", priority=1,
                                 data={"archive_bucket": "resolved"})
        Alert.objects.filter(pk=a.pk).update(created_at=now - timedelta(days=2))
        Alert.objects.create(patient=self.p1, title="", description="no title", priority=3)
        Alert.objects.create(patient=self.p3, title="Missed", description="d", priority=2)

    def patients(self):
        return list(Patient.objects.select_related("caregiver", "navigator").order_by("pk"))


class SameAsBefore(_Data):
    def test_every_clients_events_are_unchanged(self):
        # Compared as sets of events: the old meetings query aggregated, so its
        # order was the database's choice (Meta.ordering does not apply there),
        # and every surface sorts the events by time before showing them.
        def same(a, b):
            key = lambda e: e["panel_token"]
            self.assertEqual(sorted(a, key=key), sorted(b, key=key))
        batched = build_events_by_patient(self.patients())
        for p in self.patients():
            with self.subTest(client=p.name):
                old = _old_build_patient_events(p)
                same(batched[p.pk], old)
                same(build_patient_events(p), old)
        # The data really does exercise the awkward cases.
        p2 = batched[self.p2.pk]
        self.assertTrue(any(e["kind"] == "recording" for e in p2))        # shared-number guess
        self.assertTrue(any(e["kind"] == "conversation" for e in batched[self.p4.pk]))  # by conversation
        self.assertEqual(batched[self.p5.pk], [])

    def test_every_clients_last_contact_is_unchanged(self):
        batched = last_communications(self.patients(), self.now)
        for p in self.patients():
            with self.subTest(client=p.name):
                old = _old_last_communication(p, self.now)
                self.assertEqual(batched[p.pk], old)
                self.assertEqual(last_communication(p, self.now), old)
        self.assertEqual(batched[self.p3.pk]["kind"], "call")    # the legacy recording, an hour ago
        self.assertIsNone(batched[self.p5.pk])


class QueriesDoNotGrowWithClients(_Data):
    def _more_clients(self, n):
        for i in range(n):
            carer = Caregiver.objects.create(name=f"C{i}", lastname="X", phone_number=f"+44770090{1000 + i}")
            p = Patient.objects.create(name=f"Extra{i}", lastname="X", caregiver=carer, navigator=self.nav,
                                       phone_number=f"+44770091{1000 + i}")
            conv = Conversation.objects.create(patient=p)
            _msg(self.now - timedelta(days=i % 5), patient=p, conversation_id=str(conv.id),
                 sender_role=Message.SenderRole.CLIENT)
            _msg(self.now - timedelta(days=1), user=f"+44770091{1000 + i}")
            mt = Meeting.objects.create(patient=p, scheduled_time=self.now - timedelta(days=3),
                                        status=Meeting.Status.COMPLETED)
            _rec(mt.scheduled_time, patient=p, meeting=mt)
            Alert.objects.create(patient=p, title="a", description="d")

    def _count(self, fn):
        with CaptureQueriesContext(connection) as q:
            fn()
        return len(q.captured_queries)

    def test_builders(self):
        before = (self._count(lambda: build_events_by_patient(self.patients())),
                  self._count(lambda: last_communications(self.patients(), self.now)))
        self._more_clients(8)
        after = (self._count(lambda: build_events_by_patient(self.patients())),
                 self._count(lambda: last_communications(self.patients(), self.now)))
        self.assertEqual(before, after)
        self.assertLessEqual(before[0], 10)
        self.assertLessEqual(before[1], 8)

    def test_pages(self):
        self.client.force_login(self.admin)
        pages = (reverse("communications"), reverse("patients"))

        def counts():
            out = []
            for url in pages:
                with CaptureQueriesContext(connection) as q:
                    resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                out.append(len(q.captured_queries))
            return out
        counts()   # the first request also warms per-process caches (settings and the like)
        before = counts()
        self._more_clients(8)
        self.assertEqual(counts(), before)

    def test_pages_show_the_clients(self):
        self.client.force_login(self.nav)
        html = self.client.get(reverse("communications") + "?kind=chat").content.decode()
        self.assertIn("Manuel Ortega", html)
        html = self.client.get(reverse("patients")).content.decode()
        self.assertIn("Elena Marchetti", html)


class PagedInTheDatabase(_Data):
    """Communications pages its chat days in the database (ChatDays). Every
    count, row, page and badge must be what the all-in-memory list gives."""

    def setUp(self):
        super().setUp()
        from ConvAI.models import SeenMark
        # Enough chat days to need several pages, at distinct times.
        conv = Conversation.objects.create(patient=self.p3)
        for i in range(70):
            _msg(self.now - timedelta(days=i, minutes=7 * i + 1), patient=self.p3,
                 conversation_id=str(conv.id), sender_role=Message.SenderRole.CLIENT)
        # Some read, some not — including a legacy day and an alert.
        for token in (f"chat-{self.p3.pk}-{(timezone.localdate(self.now - timedelta(days=2)))}",
                      f"chat-{self.p3.pk}-{(timezone.localdate(self.now - timedelta(days=40)))}",
                      f"chat-{self.p1.pk}-{timezone.localdate(self.now - timedelta(days=4))}"):
            SeenMark.objects.create(user=self.admin, token=token)
        SeenMark.objects.create(user=self.admin, token=f"alert-{Alert.objects.filter(patient=self.p1).first().pk}")

    def _contexts(self, params):
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import RequestFactory
        from ConvAI.views.communications import ChatDays, comms_list_context

        def request():
            r = RequestFactory().get("/communications/", params)
            r.user = self.admin
            r.session = SessionStore()
            return r
        patients = self.patients()
        everything = [e for evs in build_events_by_patient(patients).values() for e in evs]
        old = comms_list_context(request(), everything)
        chat_days = ChatDays(patients)
        events = [e for evs in build_events_by_patient(patients, chats=False).values() for e in evs]
        new = comms_list_context(request(), events + chat_days.loose, chat_days=chat_days)
        return old, new

    KEYS = ("tab", "kind", "groups", "chips", "when", "when_chips", "total", "page", "pages",
            "first_shown", "last_shown", "shown_count", "past_unread", "up_unread", "past_count",
            "up_count", "overdue_count")

    def test_same_list_for_every_view(self):
        tok = f"chat-{self.p3.pk}-{timezone.localdate(self.now - timedelta(days=55))}"
        cases = [{}, {"page": "2"}, {"page": "3"}, {"page": "99"}, {"kind": "chat"},
                 {"kind": "chat", "page": "2"}, {"kind": "alert"}, {"kind": "call"}, {"kind": "visit"},
                 {"when": "7"}, {"when": "30", "kind": "chat"}, {"when": "90", "page": "2"},
                 {"q": "elena"}, {"q": "ortega"}, {"q": "ana ruiz"}, {"q": "nobody"},
                 {"q": "elena", "when": "30", "page": "2"}, {"tab": "up"}, {"tab": "up", "kind": "late"},
                 {"item": tok}, {"item": f"meeting-{self.m_phone.pk}"}, {"item": tok, "kind": "chat"},
                 {"item": tok, "q": "elena"}]
        for params in cases:
            with self.subTest(params=params):
                old, new = self._contexts(params)
                for key in self.KEYS:
                    self.assertEqual(new[key], old[key], key)
        # The cases mean something: several pages, and the item lands on a later one.
        old, _new = self._contexts({"item": tok})
        self.assertGreater(old["pages"], 2)
        self.assertGreater(old["page"], 1)

    def test_page_does_not_build_every_chat_day(self):
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as q:
            self.client.get(reverse("communications"))
        sql = " ".join(x["sql"] for x in q.captured_queries)
        self.assertIn("LIMIT", sql.upper())
