"""Calling someone from the client page, without an appointment for it.

Two things had never been possible before this: placing a call from the one
page that is entirely about a person, and ringing the client themself. Every
call the platform had ever placed went to the caregiver, and refused outright
when there was not one — so a client with a phone of their own could not be
reached at all.

What these cover is mostly the seams. The Twilio round trip, the leg
bookkeeping and the refusals are shared with the booked path and tested there
(test_call_attribution.PlacingACall); what is new is which number is dialled,
the meeting created to hold the call, and what is left behind when the call
does not go out.

Run with the sqlite settings beside manage.py:

    python3 manage.py test ConvAI.test_client_call --settings=test_settings
"""

from unittest import mock

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI.models import (
    CallLeg, CallRecording, Caregiver, ConvAIUser, Meeting, Patient,
)

CLIENT_PHONE = "+447111111111"
CARER_PHONE = "+447222222222"
STAFF = "+447000000001"
PLATFORM = "+447999999999"

CONFERENCE = {"conference": "conf-1", "legs": [
    {"leg": int(CallRecording.Leg.CTN), "call_sid": "CA_ctn",
     "to_number": STAFF, "recorded": True},
    {"leg": int(CallRecording.Leg.DYAD), "call_sid": "CA_dyad",
     "to_number": CLIENT_PHONE, "recorded": True},
]}


def _navigator(username, phone=STAFF):
    user = ConvAIUser.objects.create_user(username=username, password="x",
                                          phone_number=phone)
    user.groups.add(Group.objects.get_or_create(name="Navigator")[0])
    return user


class CallingFromTheClientPage(TestCase):
    def setUp(self):
        self.nav = _navigator("nav")
        self.carer = Caregiver.objects.create(
            name="Ana", lastname="Ortega", phone_number=CARER_PHONE,
            relationship="daughter")
        self.p = Patient.objects.create(
            name="Manuel", lastname="Ortega", phone_number=CLIENT_PHONE,
            caregiver=self.carer, navigator=self.nav)
        self.client.force_login(self.nav)

    def _call(self, to="caregiver", conference=CONFERENCE, patient=None, boom=False):
        target = patient or self.p
        kw = {"side_effect": RuntimeError("twilio")} if boom else {"return_value": conference}
        with mock.patch("ConvAI.views.calls.make_phone_conference", **kw) as mp, \
             mock.patch("ConvAI.views.calls.get_platform_phone", return_value=PLATFORM):
            resp = self.client.post(
                reverse("start_client_call", args=[target.pk]), {"to": to})
        return resp, mp

    # ── which phone actually rings ───────────────────────────────────────

    def test_calling_the_caregiver_rings_the_caregivers_number(self):
        resp, mp = self._call("caregiver")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mp.call_args.args[0], {
            "CTN": STAFF, "Dyad": CARER_PHONE, "Platform": PLATFORM})
        m = Meeting.objects.get()
        self.assertEqual(m.dial_target, Meeting.DialTarget.CAREGIVER)
        self.assertEqual(resp.json()["who"], "Ana Ortega")

    def test_calling_the_client_rings_the_clients_own_number(self):
        """The whole point of the picker: this number was unreachable before."""
        resp, mp = self._call("client")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mp.call_args.args[0]["Dyad"], CLIENT_PHONE)
        m = Meeting.objects.get()
        self.assertEqual(m.dial_target, Meeting.DialTarget.CLIENT)
        self.assertEqual(resp.json()["who"], "Manuel Ortega")

    def test_a_client_with_no_caregiver_can_still_be_called(self):
        solo = Patient.objects.create(name="Solo", lastname="S",
                                      phone_number=CLIENT_PHONE, navigator=self.nav)
        resp, mp = self._call("client", patient=solo)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mp.call_args.args[0]["Dyad"], CLIENT_PHONE)

    def test_an_unrecognised_target_falls_back_to_the_caregiver(self):
        """The platform's long-standing default, not an error page."""
        _, mp = self._call("the-postman")
        self.assertEqual(mp.call_args.args[0]["Dyad"], CARER_PHONE)

    # ── the meeting the call is held in ──────────────────────────────────

    def test_the_call_becomes_an_unscheduled_pending_meeting(self):
        before = timezone.now()
        resp, _ = self._call("caregiver")
        m = Meeting.objects.get()
        self.assertTrue(m.unscheduled)
        self.assertEqual(m.status, Meeting.Status.PENDING)
        self.assertEqual(m.modality, Meeting.Modality.PHONE)
        self.assertEqual(m.patient_id, self.p.pk)
        self.assertGreaterEqual(m.scheduled_time, before)
        self.assertEqual(m.retries, 1, "a call that went out counts as an attempt")
        self.assertEqual(resp.json()["item"], m.panel_token)

    def test_the_legs_are_recorded_against_it(self):
        """Without these the recording arriving hours later belongs to nobody."""
        self._call("client")
        m = Meeting.objects.get()
        legs = {l.call_sid: l for l in CallLeg.objects.all()}
        self.assertEqual(set(legs), {"CA_ctn", "CA_dyad"})
        self.assertEqual(legs["CA_dyad"].meeting_id, m.pk)
        self.assertEqual(legs["CA_dyad"].patient_id, self.p.pk)
        self.assertEqual(legs["CA_dyad"].placed_by_id, self.nav.pk)

    def test_a_booked_call_is_never_folded_into(self):
        """Reusing it would record the booked call as made when it was not."""
        booked = Meeting.objects.create(patient=self.p,
                                        scheduled_time=timezone.now())
        self._call("caregiver")
        self.assertEqual(Meeting.objects.count(), 2)
        booked.refresh_from_db()
        self.assertEqual(booked.retries, 0)
        self.assertFalse(booked.unscheduled)

    # ── when the call does not go out ────────────────────────────────────

    def test_a_refused_call_leaves_no_meeting_behind(self):
        """Otherwise every failure adds a call to the queue that never happened."""
        self.nav.phone_number = None
        self.nav.save()
        resp, mp = self._call("caregiver")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("fix", resp.json())
        self.assertEqual(resp.json()["fix"]["href"], reverse("profile"))
        mp.assert_not_called()
        self.assertFalse(Meeting.objects.exists())

    def test_a_twilio_failure_leaves_no_meeting_behind(self):
        resp, _ = self._call("caregiver", boom=True)
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(Meeting.objects.exists())
        self.assertFalse(CallLeg.objects.exists())

    def test_calling_someone_with_no_number_says_which_of_them(self):
        self.p.phone_number = None
        self.p.save()
        resp, mp = self._call("client")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no phone number of their own", resp.json()["error"])
        mp.assert_not_called()
        self.assertFalse(Meeting.objects.exists())

    # ── who may place one ────────────────────────────────────────────────

    def test_another_navigators_client_cannot_be_called(self):
        other = _navigator("other")
        theirs = Patient.objects.create(name="X", lastname="Y",
                                        phone_number=CLIENT_PHONE, navigator=other)
        resp, mp = self._call("client", patient=theirs)
        self.assertEqual(resp.status_code, 403)
        mp.assert_not_called()
        self.assertFalse(Meeting.objects.exists())

    def test_a_get_does_not_place_a_call(self):
        with mock.patch("ConvAI.views.calls.make_phone_conference") as mp:
            resp = self.client.get(reverse("start_client_call", args=[self.p.pk]))
        self.assertEqual(resp.status_code, 405)
        mp.assert_not_called()
        self.assertFalse(Meeting.objects.exists())


class WhatTheClientPageOffers(TestCase):
    def setUp(self):
        self.nav = _navigator("nav")
        self.p = Patient.objects.create(name="Manuel", lastname="Ortega",
                                        phone_number=CLIENT_PHONE, navigator=self.nav)
        self.client.force_login(self.nav)

    def _page(self, **params):
        return self.client.get(reverse("patient_detail", args=[self.p.pk]), params)

    def test_both_people_are_offered_even_when_one_has_no_number(self):
        self.p.caregiver = Caregiver.objects.create(name="Ana", lastname="O")
        self.p.save()
        opts = {o["value"]: o for o in self._page().context["call_options"]}
        self.assertEqual(list(opts), ["caregiver", "client"])
        self.assertEqual(opts["caregiver"]["phone"], "")
        self.assertEqual(opts["client"]["phone"], CLIENT_PHONE)

    def test_the_caregiver_is_preselected_when_they_can_be_rung(self):
        self.p.caregiver = Caregiver.objects.create(name="Ana", lastname="O",
                                                    phone_number=CARER_PHONE)
        self.p.save()
        self.assertEqual(self._page().context["call_default"], "caregiver")

    def test_the_client_is_preselected_when_they_are_the_only_number(self):
        self.assertEqual(self._page().context["call_default"], "client")

    def test_no_numbers_at_all_means_no_call_button(self):
        self.p.phone_number = None
        self.p.save()
        ctx = self._page().context
        self.assertFalse(ctx["can_call"])
        self.assertEqual(ctx["call_default"], "")

    def test_the_panel_names_whoever_the_call_rang(self):
        """The panel said the caregiver for every call, including this one."""
        m = Meeting.objects.create(patient=self.p, scheduled_time=timezone.now(),
                                   dial_target=Meeting.DialTarget.CLIENT,
                                   unscheduled=True)
        item = self._page(item=m.panel_token).context["panel_item"]
        self.assertEqual(item["to"]["who"], "Manuel Ortega")
        self.assertEqual(item["dial_who"], "Manuel Ortega")
        self.assertTrue(item["can_call"])
        self.assertEqual(item["kicker"], "Unscheduled call")
        self.assertNotIn("not on this call", item["to"]["note"])

    def test_an_unscheduled_call_is_not_listed_as_a_scheduled_one(self):
        Meeting.objects.create(patient=self.p, scheduled_time=timezone.now(),
                               dial_target=Meeting.DialTarget.CLIENT,
                               unscheduled=True)
        rows = [r for group in self._page(tab="up").context["groups"]
                for r in group["rows"]]
        self.assertEqual([r["line2"] for r in rows], ["Unscheduled call"])
        self.assertEqual([r["detail"] for r in rows], ["with Manuel Ortega"])
