"""Which call a recording came from, and whose it is.

These cover the two ways a recording used to be filed under the wrong name.
Both came from the same root: nothing linked a CallRecording to anything, so
ownership was worked out by matching from_number/to_number against the client's
and their caregiver's numbers.

  1. A number can belong to two clients — a caregiver who looks after one and
     is themself another — and one call was then drawn on both timelines.
  2. A conference records the navigator's own leg too, on a staff number, and
     that was filed against whichever client happened to share it.

The 148 rows made before any of this must keep rendering the old way, so the
fallback is covered as deliberately as the fix.

Run with the sqlite settings beside manage.py:

    python3 manage.py test ConvAI.test_call_attribution --settings=test_settings
"""

import datetime as dt
import importlib
from unittest import mock

from django.apps import apps as real_apps
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI.models import (
    CallLeg, CallRecording, Caregiver, ConvAIUser, Meeting, Patient,
)
from ConvAI.views._panel import _context_strip, _patient_phones
from ConvAI.views.patients import build_patient_events

backfill_mod = importlib.import_module(
    "ConvAI.migrations.0081_backfill_recording_owners"
)

# The number from the live diagnosis: one client's own, and the caregiver of a
# different client.
SHARED = "+447920363415"
SOLO = "+447111111111"
STAFF = "+447000000001"


def _navigator(username, phone=None):
    user = ConvAIUser.objects.create_user(username=username, password="x",
                                          phone_number=phone)
    user.groups.add(Group.objects.get_or_create(name="Navigator")[0])
    return user


class Timelines(TestCase):
    def setUp(self):
        self.nav = _navigator("nav", STAFF)
        # Pablo is a client whose own number is SHARED.
        self.pablo = Patient.objects.create(
            name="Pablo", lastname="Fonseca", phone_number=SHARED, navigator=self.nav)
        # Hansoo is a different client whose *caregiver* uses the same number.
        cg = Caregiver.objects.create(name="C", lastname="G", phone_number=SHARED)
        self.hansoo = Patient.objects.create(
            name="Hansoo", lastname="Lee", caregiver=cg, navigator=self.nav)

        self.t = timezone.now() - dt.timedelta(hours=2)
        self.m_hansoo = Meeting.objects.create(
            patient=self.hansoo, scheduled_time=self.t, ended_at=self.t,
            status=Meeting.Status.COMPLETED)
        self.m_pablo = Meeting.objects.create(
            patient=self.pablo, scheduled_time=self.t, ended_at=self.t,
            status=Meeting.Status.COMPLETED)

    def _rec(self, **kw):
        base = dict(recording_sid="RE" + str(CallRecording.objects.count()),
                    from_number="+4499", to_number=SHARED,
                    start_time=self.t, end_time=self.t + dt.timedelta(minutes=5),
                    duration=300)
        base.update(kw)
        return CallRecording.objects.create(**base)

    def _kinds(self, patient):
        return [(e['kind'], e.get('recording') is not None if e['kind'] == 'meeting' else None)
                for e in build_patient_events(patient)]

    def test_shared_number_lands_on_one_timeline_only(self):
        """Bug 1: a number two clients share drew the same call on both."""
        rec = self._rec(patient=self.hansoo, meeting=self.m_hansoo,
                        leg=CallRecording.Leg.DYAD, call_sid="CA1")

        hansoo_recs = [e for e in build_patient_events(self.hansoo)
                       if e['kind'] == 'meeting' and e.get('recording')]
        pablo_recs = [e for e in build_patient_events(self.pablo)
                      if e['kind'] == 'meeting' and e.get('recording')]
        loose_pablo = [e for e in build_patient_events(self.pablo) if e['kind'] == 'recording']

        self.assertEqual(len(hansoo_recs), 1, "Hansoo should have the recording")
        self.assertEqual(pablo_recs, [], "Pablo must not have it folded onto his call")
        self.assertEqual(loose_pablo, [], "Pablo must not have it as a loose row either")
        self.assertEqual(_context_strip(self.pablo)['calls'], 0)
        self.assertEqual(_context_strip(self.hansoo)['calls'], 1)

    def test_navigator_leg_is_nobodys_call(self):
        """Bug 2: the navigator's own leg was filed against a client."""
        self._rec(to_number=STAFF, patient=self.hansoo, meeting=self.m_hansoo,
                  leg=CallRecording.Leg.CTN, call_sid="CA2")
        for p in (self.pablo, self.hansoo):
            evs = build_patient_events(p)
            self.assertEqual([e for e in evs if e['kind'] == 'recording'], [],
                             f"{p.name}: CTN leg must not be a timeline row")
            self.assertFalse(any(e.get('recording') for e in evs if e['kind'] == 'meeting'),
                             f"{p.name}: CTN leg must not fold onto a call")
        self.assertEqual(_context_strip(self.hansoo)['calls'], 0)

    def test_legacy_row_still_folds_by_number_and_time(self):
        """148 rows have none of this and must keep rendering."""
        self._rec()  # no patient, no meeting, no leg
        hansoo = [e for e in build_patient_events(self.hansoo)
                  if e['kind'] == 'meeting' and e.get('recording')]
        pablo = [e for e in build_patient_events(self.pablo)
                 if e['kind'] == 'meeting' and e.get('recording')]
        # Still ambiguous — both see it. That is the old behaviour, kept.
        self.assertEqual(len(hansoo), 1)
        self.assertEqual(len(pablo), 1)

    def test_exact_match_beats_a_nearer_guess(self):
        """A named meeting is not outbid by a legacy row that happens to be closer."""
        far = self.t - dt.timedelta(minutes=40)
        m_far = Meeting.objects.create(patient=self.hansoo, scheduled_time=far,
                                       ended_at=far, status=Meeting.Status.COMPLETED)
        # A legacy recording sitting right on m_far, and a named one for m_far.
        self._rec(start_time=far)                       # legacy, nearest to m_far
        self._rec(start_time=self.t, patient=self.hansoo, meeting=m_far,
                  leg=CallRecording.Leg.DYAD)           # named, further away
        evs = build_patient_events(self.hansoo)
        folded = {e['pk']: e['recording'] for e in evs if e['kind'] == 'meeting'}
        self.assertIsNotNone(folded[m_far.pk], "named recording keeps its meeting")
        self.assertIsNotNone(folded[self.m_hansoo.pk], "legacy one takes the free call")

    def test_cancelled_meeting_keeps_a_recording_that_names_it(self):
        c = self.t
        m = Meeting.objects.create(patient=self.pablo, scheduled_time=c, cancelled_at=c,
                                   status=Meeting.Status.CANCELLED)
        self._rec(patient=self.pablo, meeting=m, leg=CallRecording.Leg.DYAD)
        evs = build_patient_events(self.pablo)
        folded = [e for e in evs if e['kind'] == 'meeting' and e['pk'] == m.pk]
        self.assertIsNotNone(folded[0]['recording'])
        self.assertEqual([e for e in evs if e['kind'] == 'recording'], [])

    def test_client_with_no_number_of_their_own(self):
        p = Patient.objects.create(name="No", lastname="Phone", navigator=self.nav)
        m = Meeting.objects.create(patient=p, scheduled_time=self.t, ended_at=self.t,
                                   status=Meeting.Status.COMPLETED)
        self._rec(to_number="+44999", patient=p, meeting=m, leg=CallRecording.Leg.DYAD)
        self.assertEqual(_patient_phones(p), [])
        self.assertEqual(_context_strip(p)['calls'], 1)
        evs = build_patient_events(p)
        self.assertIsNotNone([e for e in evs if e['kind'] == 'meeting'][0]['recording'])

    def test_resolve_and_owners(self):
        legacy = self._rec()
        named = self._rec(patient=self.hansoo)
        self.assertEqual(named.resolve_patient(), self.hansoo)
        self.assertEqual(set(named.owner_patients().values_list("pk", flat=True)),
                         {self.hansoo.pk})
        self.assertEqual(set(legacy.owner_patients().values_list("pk", flat=True)),
                         {self.hansoo.pk, self.pablo.pk})


class BackfillMigration(TestCase):
    def setUp(self):
        self.nav = _navigator("nav", STAFF)
        self.pablo = Patient.objects.create(
            name="Pablo", lastname="F", phone_number=SHARED, navigator=self.nav)
        cg = Caregiver.objects.create(name="C", lastname="G", phone_number=SHARED)
        self.hansoo = Patient.objects.create(
            name="Hansoo", lastname="L", caregiver=cg, navigator=self.nav)
        self.solo = Patient.objects.create(
            name="Solo", lastname="S", phone_number=SOLO, navigator=self.nav)
        self.t = timezone.now() - dt.timedelta(days=1)

    def _rec(self, to, when, sid):
        return CallRecording.objects.create(
            recording_sid=sid, from_number="+4499", to_number=to,
            start_time=when, end_time=when + dt.timedelta(minutes=5), duration=300)

    def _run(self):
        backfill_mod.backfill(real_apps, None)

    def test_unambiguous_number_is_filed_onto_its_nearest_meeting(self):
        m = Meeting.objects.create(patient=self.solo, scheduled_time=self.t,
                                   ended_at=self.t, status=Meeting.Status.COMPLETED)
        rec = self._rec(SOLO, self.t + dt.timedelta(minutes=20), "RE1")
        self._run()
        rec.refresh_from_db()
        self.assertEqual(rec.patient_id, self.solo.pk)
        self.assertEqual(rec.meeting_id, m.pk)

    def test_shared_number_is_left_alone(self):
        Meeting.objects.create(patient=self.pablo, scheduled_time=self.t,
                               ended_at=self.t, status=Meeting.Status.COMPLETED)
        rec = self._rec(SHARED, self.t, "RE2")
        self._run()
        rec.refresh_from_db()
        self.assertIsNone(rec.patient_id, "two clients answer to it — do not guess")
        self.assertIsNone(rec.meeting_id)
        self.assertIsNone(rec.leg)

    def test_pure_staff_number_is_marked_as_the_navigator_leg(self):
        rec = self._rec(STAFF, self.t, "RE3")
        self._run()
        rec.refresh_from_db()
        self.assertEqual(rec.leg, CallRecording.Leg.CTN)
        self.assertIsNone(rec.patient_id)

    def test_two_recordings_never_share_one_meeting(self):
        m = Meeting.objects.create(patient=self.solo, scheduled_time=self.t,
                                   ended_at=self.t, status=Meeting.Status.COMPLETED)
        a = self._rec(SOLO, self.t, "RE4")
        b = self._rec(SOLO, self.t, "RE5")   # same (to_number, start_time) group
        self._run()
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual({a.patient_id, b.patient_id}, {self.solo.pk})
        self.assertEqual(sorted(x for x in (a.meeting_id, b.meeting_id) if x), [m.pk])
        self.assertIsNone([x for x in (a, b) if x.meeting_id is None][0].meeting_id)

    def test_a_recording_outside_the_window_gets_a_client_but_no_meeting(self):
        Meeting.objects.create(patient=self.solo, scheduled_time=self.t,
                               ended_at=self.t, status=Meeting.Status.COMPLETED)
        rec = self._rec(SOLO, self.t + dt.timedelta(hours=4), "RE6")
        self._run()
        rec.refresh_from_db()
        self.assertEqual(rec.patient_id, self.solo.pk)
        self.assertIsNone(rec.meeting_id)

    def test_cancelled_meetings_are_not_guessed_onto(self):
        Meeting.objects.create(patient=self.solo, scheduled_time=self.t,
                               cancelled_at=self.t, status=Meeting.Status.CANCELLED)
        rec = self._rec(SOLO, self.t, "RE7")
        self._run()
        rec.refresh_from_db()
        self.assertEqual(rec.patient_id, self.solo.pk)
        self.assertIsNone(rec.meeting_id)

    def test_rerunning_changes_nothing(self):
        Meeting.objects.create(patient=self.solo, scheduled_time=self.t,
                               ended_at=self.t, status=Meeting.Status.COMPLETED)
        rec = self._rec(SOLO, self.t, "RE8")
        self._run()
        rec.refresh_from_db()
        before = (rec.patient_id, rec.meeting_id)
        self._run()
        rec.refresh_from_db()
        self.assertEqual((rec.patient_id, rec.meeting_id), before)


class PlacingACall(TestCase):
    def setUp(self):
        self.nav = _navigator("nav", STAFF)
        cg = Caregiver.objects.create(name="C", lastname="G", phone_number=SOLO)
        self.p = Patient.objects.create(name="P", lastname="P", caregiver=cg,
                                        navigator=self.nav)
        self.m = Meeting.objects.create(patient=self.p,
                                        scheduled_time=timezone.now())
        self.client.force_login(self.nav)

    def _place(self, conference):
        with mock.patch("ConvAI.views.calls.make_phone_conference",
                        return_value=conference) as mp, \
             mock.patch("ConvAI.views.calls.get_platform_phone",
                        return_value="+4400"):
            resp = self.client.get(reverse("make_phone_call", args=[self.m.pk]))
        return resp, mp

    def test_legs_are_written_and_sids_stay_server_side(self):
        conf = {"conference": "abc", "legs": [
            {"leg": 1, "call_sid": "CA_ctn", "to_number": STAFF, "recorded": True},
            {"leg": 0, "call_sid": "CA_dyad", "to_number": SOLO, "recorded": True},
        ]}
        resp, _ = self._place(conf)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok", "conference": "abc"})
        self.assertNotIn("CA_dyad", resp.content.decode())
        legs = {l.call_sid: l for l in CallLeg.objects.all()}
        self.assertEqual(set(legs), {"CA_ctn", "CA_dyad"})
        self.assertEqual(legs["CA_dyad"].leg, CallRecording.Leg.DYAD)
        self.assertEqual(legs["CA_dyad"].meeting_id, self.m.pk)
        self.assertEqual(legs["CA_dyad"].patient_id, self.p.pk)
        self.assertEqual(legs["CA_dyad"].placed_by_id, self.nav.pk)
        self.m.refresh_from_db()
        self.assertEqual(self.m.retries, 1)

    def test_a_leg_that_cannot_be_written_does_not_fail_the_call(self):
        conf = {"conference": "abc", "legs": [
            {"leg": 0, "call_sid": None, "to_number": SOLO},
            {"leg": 1, "call_sid": "x" * 200, "to_number": STAFF},  # too long
        ]}
        resp, _ = self._place(conf)
        self.assertEqual(resp.status_code, 200, "the phones are already ringing")
        self.m.refresh_from_db()
        self.assertEqual(self.m.retries, 1)

    def test_placing_the_same_conference_twice_is_not_an_error(self):
        conf = {"conference": "abc", "legs": [
            {"leg": 0, "call_sid": "CA_dyad", "to_number": SOLO}]}
        self._place(conf)
        self._place(conf)
        self.assertEqual(CallLeg.objects.filter(call_sid="CA_dyad").count(), 1)


class TwilioSync(TestCase):
    """get_recordings_from_twilio joins incoming audio to the leg that placed it."""

    def setUp(self):
        self.nav = _navigator("n")
        cg = Caregiver.objects.create(name="C", lastname="G", phone_number=SOLO)
        self.p = Patient.objects.create(name="P", lastname="P", caregiver=cg,
                                        navigator=self.nav)
        self.m = Meeting.objects.create(patient=self.p, scheduled_time=timezone.now())
        CallLeg.objects.create(call_sid="CA_dyad", meeting=self.m, patient=self.p,
                               leg=CallRecording.Leg.DYAD, to_number=SOLO)
        CallLeg.objects.create(call_sid="CA_ctn", meeting=self.m, patient=self.p,
                               leg=CallRecording.Leg.CTN, to_number=STAFF)

    def _sync(self, recordings, calls):
        twilio = mock.MagicMock()
        twilio.recordings.list.return_value = recordings
        twilio.calls.list.return_value = calls
        with mock.patch("ConvAI.utils.Client", return_value=twilio), \
             mock.patch("ConvAI.utils.get_setting", return_value="x"), \
             mock.patch("ConvAI.utils.download_recording_mp3", return_value="/tmp/a.mp3"):
            from ConvAI.utils import get_recordings_from_twilio
            get_recordings_from_twilio()

    def _rec_obj(self, sid, call_sid, when):
        r = mock.MagicMock()
        r.sid = sid
        r.call_sid = call_sid
        r.date_created = when
        r.date_updated = when + dt.timedelta(minutes=1)
        r.duration = 120
        return r

    def _call_obj(self, sid, to):
        c = mock.MagicMock()
        c.sid = sid
        c.from_formatted = "+4400"
        c.to_formatted = to
        return c

    def test_incoming_recording_takes_the_relation_from_its_leg(self):
        now = timezone.now()
        self._sync(
            [self._rec_obj("RE_a", "CA_dyad", now), self._rec_obj("RE_b", "CA_ctn", now)],
            [self._call_obj("CA_dyad", SOLO), self._call_obj("CA_ctn", STAFF)],
        )
        a = CallRecording.objects.get(recording_sid="RE_a")
        b = CallRecording.objects.get(recording_sid="RE_b")
        self.assertEqual((a.patient_id, a.meeting_id, a.leg, a.call_sid),
                         (self.p.pk, self.m.pk, CallRecording.Leg.DYAD, "CA_dyad"))
        self.assertEqual(b.leg, CallRecording.Leg.CTN)

    def test_a_late_arrival_is_not_skipped_forever(self):
        """The old end_time high-water mark dropped these permanently."""
        now = timezone.now()
        # A newer recording syncs first and would have moved the mark past the
        # older one, which only finishes assembling afterwards.
        self._sync([self._rec_obj("RE_new", "CA_dyad", now)],
                   [self._call_obj("CA_dyad", SOLO)])
        self._sync([self._rec_obj("RE_new", "CA_dyad", now),
                    self._rec_obj("RE_late", "CA_ctn", now - dt.timedelta(hours=3))],
                   [self._call_obj("CA_dyad", SOLO), self._call_obj("CA_ctn", STAFF)])
        self.assertTrue(CallRecording.objects.filter(recording_sid="RE_late").exists())
        self.assertEqual(CallRecording.objects.filter(recording_sid="RE_new").count(), 1)

    def test_a_recording_stored_before_its_leg_is_filled_in_later(self):
        now = timezone.now()
        CallRecording.objects.create(recording_sid="RE_old", from_number="+4400",
                                     to_number=SOLO, start_time=now,
                                     end_time=now, duration=10)
        self._sync([self._rec_obj("RE_old", "CA_dyad", now)],
                   [self._call_obj("CA_dyad", SOLO)])
        r = CallRecording.objects.get(recording_sid="RE_old")
        self.assertEqual((r.patient_id, r.meeting_id, r.leg), 
                         (self.p.pk, self.m.pk, CallRecording.Leg.DYAD))


class Surfaces(TestCase):
    def setUp(self):
        self.nav = _navigator("nav", STAFF)
        cg = Caregiver.objects.create(name="C", lastname="G", phone_number=SOLO)
        self.p = Patient.objects.create(name="P", lastname="P", caregiver=cg,
                                        navigator=self.nav)
        t = timezone.now() - dt.timedelta(hours=1)
        self.m = Meeting.objects.create(patient=self.p, scheduled_time=t, ended_at=t,
                                        status=Meeting.Status.COMPLETED)
        self.attributed = CallRecording.objects.create(
            recording_sid="RE_ok", from_number="+4400", to_number=SOLO,
            start_time=t, end_time=t + dt.timedelta(minutes=5), duration=300,
            meeting=self.m, patient=self.p, leg=CallRecording.Leg.DYAD,
            call_sid="CA_dyad")
        self.ctn = CallRecording.objects.create(
            recording_sid="RE_ctn", from_number="+4400", to_number=STAFF,
            start_time=t, end_time=t + dt.timedelta(minutes=5), duration=300,
            meeting=self.m, patient=self.p, leg=CallRecording.Leg.CTN,
            call_sid="CA_ctn")
        self.legacy = CallRecording.objects.create(
            recording_sid="RE_old", from_number="+4400", to_number=SOLO,
            start_time=t - dt.timedelta(days=30),
            end_time=t - dt.timedelta(days=30), duration=60)
        self.client.force_login(self.nav)

    def test_client_page(self):
        r = self.client.get(reverse("patient_detail", args=[self.p.pk]))
        self.assertEqual(r.status_code, 200)

    def test_communications(self):
        self.assertEqual(self.client.get(reverse("communications")).status_code, 200)

    def test_meeting_panel_finds_the_attributed_recording(self):
        r = self.client.get(reverse("communications"), {"item": self.m.panel_token})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "RE_ok")
        self.assertNotContains(r, "RE_ctn")

    def test_recording_panel(self):
        for rec in (self.attributed, self.legacy):
            r = self.client.get(reverse("communications"),
                                {"item": f"recording-{rec.pk}"})
            self.assertEqual(r.status_code, 200, rec.recording_sid)

    def test_audio_and_transcribe_permissions(self):
        self.assertEqual(
            self.client.get(reverse("serve_protected_file",
                                    args=[self.attributed.recording_sid])).status_code,
            400,  # authorised; fails only because the file is not on disk
        )
        self.client.force_login(_navigator("other"))
        self.assertEqual(
            self.client.get(reverse("serve_protected_file",
                                    args=[self.attributed.recording_sid])).status_code,
            403,
        )

    def test_note_on_a_legacy_recording(self):
        r = self.client.post(
            reverse("add_note", args=["recording", self.legacy.recording_sid]),
            data='{"body": "hello"}', content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["ok"])


class RedialledCall(TestCase):
    """A call is not one recording.

    A number that rings out and is redialled leaves a few seconds of ringing
    tone behind on every attempt. Each of those belongs to the call that was
    being attempted, but the fold kept one and orphaned the rest into loose
    "Call recording" rows — which is what made the timeline look like it was
    listing every call twice. It was not: those rows were the leftovers.
    """

    def setUp(self):
        self.nav = _navigator("nav", STAFF)
        cg = Caregiver.objects.create(name="C", lastname="G", phone_number=SOLO)
        self.p = Patient.objects.create(name="P", lastname="P", caregiver=cg,
                                        navigator=self.nav)
        self.t = timezone.now() - dt.timedelta(hours=1)
        self.m = Meeting.objects.create(patient=self.p, scheduled_time=self.t,
                                        ended_at=self.t,
                                        status=Meeting.Status.COMPLETED)

    def _rec(self, sid, minutes, duration, **kw):
        at = self.t + dt.timedelta(minutes=minutes)
        return CallRecording.objects.create(
            recording_sid=sid, from_number="+4400", to_number=SOLO,
            start_time=at, end_time=at + dt.timedelta(seconds=duration),
            duration=duration, **kw)

    def _events(self):
        return build_patient_events(self.p)

    def test_every_attempt_folds_onto_the_one_call(self):
        for i, secs in enumerate((4, 12, 300)):
            self._rec(f"RE{i}", i, secs, meeting=self.m, patient=self.p,
                      leg=CallRecording.Leg.DYAD)
        evs = self._events()
        self.assertEqual([e for e in evs if e['kind'] == 'recording'], [],
                         "attempts must not be loose rows")
        meeting = next(e for e in evs if e['kind'] == 'meeting')
        self.assertEqual(meeting['recording_count'], 3)

    def test_the_row_names_the_conversation_not_the_first_attempt(self):
        # Chronologically the four-second ringing tone comes first; it is not
        # what anybody means by "the recording of this call".
        self._rec("RE_ring", 0, 4, meeting=self.m, patient=self.p,
                  leg=CallRecording.Leg.DYAD)
        self._rec("RE_talk", 3, 300, meeting=self.m, patient=self.p,
                  leg=CallRecording.Leg.DYAD)
        meeting = next(e for e in self._events() if e['kind'] == 'meeting')
        self.assertEqual(meeting['recording']['recording_sid'], "RE_talk")

    def test_legacy_attempts_fold_too(self):
        """Rows with nothing written down still stop being loose."""
        for i, secs in enumerate((4, 12, 300)):
            self._rec(f"RE{i}", i, secs)
        evs = self._events()
        self.assertEqual([e for e in evs if e['kind'] == 'recording'], [],
                         "nearby legacy attempts belong to the call")
        self.assertEqual(next(e for e in evs
                              if e['kind'] == 'meeting')['recording_count'], 3)

    def test_a_recording_with_no_call_is_still_a_row(self):
        """The fold must not become a way to lose audio."""
        far = self._rec("RE_far", 60 * 24 * 30, 90)
        loose = [e for e in self._events() if e['kind'] == 'recording']
        self.assertEqual([e['recording_sid'] for e in loose], ["RE_far"])
        self.assertEqual(loose[0]['panel_token'], f"recording-{far.pk}")

    def test_panel_and_row_name_the_same_recording(self):
        """The bug behind the apparent duplicates: two matchers, two answers."""
        self._rec("RE_ring", 0, 4, meeting=self.m, patient=self.p,
                  leg=CallRecording.Leg.DYAD)
        self._rec("RE_talk", 3, 300, meeting=self.m, patient=self.p,
                  leg=CallRecording.Leg.DYAD)
        self.client.force_login(self.nav)
        r = self.client.get(reverse("communications"), {"item": self.m.panel_token})
        self.assertEqual(r.status_code, 200)
        row = next(e for e in self._events() if e['kind'] == 'meeting')
        self.assertEqual(r.context['panel_item']['recording'].recording_sid,
                         row['recording']['recording_sid'])
        # And the attempt it is not leading with is still reachable from here.
        self.assertEqual(len(r.context['panel_item']['other_recordings']), 1)
