"""Phone calls can be switched off (Settings → General, or PHONE_CALLS_ENABLED).

Off, an installation that only meets people in person cannot ring anybody:
nothing offers a call, new meetings default to in person, and both call
endpoints refuse before Twilio is touched. On — the default — nothing changes.

    python3 manage.py test ConvAI.test_phone_calls_switch --settings=test_settings
"""
import os
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI.forms import GeneralConfigForm, MeetingForm
from ConvAI.models import Caregiver, ConvAIUser, Meeting, Patient, SiteConfiguration
from ConvAI.site_config import phone_calls_enabled
from ConvAI.views._panel import modality_choices

CARER_PHONE = "+447222222222"


def _choices(form):
    return [v for v, _l in form.fields["modality"].choices]


class _Base(TestCase):
    def setUp(self):
        # The switch's .env fallback must not come from the machine running the tests.
        env = mock.patch.dict(os.environ, {"PHONE_CALLS_ENABLED": ""})
        env.start()
        self.addCleanup(env.stop)
        cache.clear()
        self.nav = ConvAIUser.objects.create_user(username="nav", password="x",
                                                  phone_number="+447000000001")
        self.nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.carer = Caregiver.objects.create(name="Ana", lastname="Ortega", phone_number=CARER_PHONE)
        self.p = Patient.objects.create(name="Manuel", lastname="Ortega", caregiver=self.carer,
                                        navigator=self.nav, phone_number="+447333333333")
        self.call = Meeting.objects.create(patient=self.p, scheduled_time=timezone.now(),
                                           modality=Meeting.Modality.PHONE)
        self.client.force_login(self.nav)

    def switch(self, value):
        s = SiteConfiguration.load()
        s.phone_calls_enabled = value
        s.save()
        cache.clear()


class CallsOnByDefault(_Base):
    """Nothing changes for an installation that never touched the switch."""

    def test_on_unless_switched_off(self):
        self.assertTrue(phone_calls_enabled())
        with mock.patch.dict(os.environ, {"PHONE_CALLS_ENABLED": "0"}):
            self.assertFalse(phone_calls_enabled())
        self.switch("0")
        self.assertFalse(phone_calls_enabled())
        # Settings wins over .env.
        with mock.patch.dict(os.environ, {"PHONE_CALLS_ENABLED": "0"}):
            self.switch("1")
            self.assertTrue(phone_calls_enabled())

    def test_form_offers_and_defaults_to_a_call(self):
        form = MeetingForm()
        self.assertIn(Meeting.Modality.PHONE, _choices(form))
        self.assertEqual(form["modality"].value(), Meeting.Modality.PHONE)
        self.assertEqual(Meeting.default_modality(), Meeting.Modality.PHONE)

    def test_call_is_placed(self):
        with mock.patch("ConvAI.views.calls.make_phone_conference",
                        return_value={"conference": "c1"}) as mp, \
             mock.patch("ConvAI.views.calls._record_call_legs"):
            resp = self.client.post(reverse("make_phone_call", args=[self.call.pk]))
        self.assertEqual(resp.status_code, 200)
        mp.assert_called_once()

    def test_client_page_has_the_call_button(self):
        html = self.client.get(reverse("patient_detail", args=[self.p.pk])).content.decode()
        self.assertIn('id="pdCall"', html)
        self.assertIn('id="pdCallDlg"', html)

    def test_switch_is_in_settings_general(self):
        self.assertIn("phone_calls_enabled", GeneralConfigForm().fields)


class CallsOff(_Base):
    def setUp(self):
        super().setUp()
        self.switch("0")

    # ── Nothing can ring ──────────────────────────────────────────────────

    def test_booked_call_is_refused_before_twilio(self):
        with mock.patch("ConvAI.views.calls.make_phone_conference") as mp:
            resp = self.client.post(reverse("make_phone_call", args=[self.call.pk]))
        mp.assert_not_called()
        self.assertEqual(resp.status_code, 403)
        self.assertIn("switched off", resp.json()["error"])
        self.call.refresh_from_db()
        self.assertEqual(self.call.retries, 0)

    def test_call_now_is_refused_and_leaves_no_meeting(self):
        before = Meeting.objects.count()
        with mock.patch("ConvAI.views.calls.make_phone_conference") as mp:
            resp = self.client.post(reverse("start_client_call", args=[self.p.pk]), {"to": "client"})
        mp.assert_not_called()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Meeting.objects.count(), before)

    def test_admins_are_refused_too(self):
        admin = ConvAIUser.objects.create_superuser(username="root", password="x",
                                                    phone_number="+447000000009")
        self.client.force_login(admin)
        with mock.patch("ConvAI.views.calls.make_phone_conference") as mp:
            resp = self.client.post(reverse("make_phone_call", args=[self.call.pk]))
        mp.assert_not_called()
        self.assertEqual(resp.status_code, 403)

    # ── Nothing offers a call ─────────────────────────────────────────────

    def test_client_page_has_no_call_button_or_dialog(self):
        html = self.client.get(reverse("patient_detail", args=[self.p.pk])).content.decode()
        self.assertNotIn('id="pdCall"', html)
        self.assertNotIn('id="pdCallDlg"', html)
        self.assertNotIn(reverse("start_client_call", args=[self.p.pk]), html)

    def test_panel_says_calls_are_off_instead_of_offering_one(self):
        html = self.client.get(reverse("panel_fragment"), {"item": f"meeting-{self.call.pk}"}).content.decode()
        self.assertNotIn('id="dpcStart"', html)
        self.assertNotIn(reverse("make_phone_call", args=[self.call.pk]), html)
        self.assertIn("Phone calls are off", html)

    def test_new_meeting_form_drops_phone_and_starts_in_person(self):
        form = MeetingForm()
        self.assertNotIn(Meeting.Modality.PHONE, _choices(form))
        self.assertIn(Meeting.Modality.IN_PERSON, _choices(form))
        self.assertEqual(form["modality"].value(), Meeting.Modality.IN_PERSON)

    def test_cannot_book_a_call_by_posting_the_value(self):
        form = MeetingForm(data={"patient": self.p.pk, "modality": Meeting.Modality.PHONE,
                                 "type": Meeting.MeetingType.REGULAR,
                                 "scheduled_time": "2026-11-01T10:00"})
        self.assertFalse(form.is_valid())
        self.assertIn("modality", form.errors)

    def test_existing_call_stays_editable_as_a_call(self):
        # Editing a call made before the switch must not silently change it.
        self.assertIn(Meeting.Modality.PHONE, _choices(MeetingForm(instance=self.call)))
        self.assertIn(Meeting.Modality.PHONE, [v for v, _l in modality_choices(self.call)])
        visit = Meeting.objects.create(patient=self.p, scheduled_time=timezone.now(),
                                       modality=Meeting.Modality.IN_PERSON)
        self.assertNotIn(Meeting.Modality.PHONE, [v for v, _l in modality_choices(visit)])

    def test_meetings_booked_through_the_api_are_in_person(self):
        from rest_framework.test import APIClient
        api = APIClient()
        api.force_authenticate(self.nav)
        when = timezone.now() + timezone.timedelta(days=3)
        resp = api.post(reverse("api_meeting_create"),
                        {"patient_id": self.p.pk, "scheduled_time": when.isoformat()}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        booked = Meeting.objects.latest("pk")
        self.assertEqual(booked.modality, Meeting.Modality.IN_PERSON)
        self.assertIsNone(booked.dial_recipient)

    def test_model_default_is_unchanged(self):
        # Rows written by old code and by migrations keep their meaning; only
        # the places that book meetings ask Meeting.default_modality().
        self.assertEqual(Meeting.default_modality(), Meeting.Modality.IN_PERSON)
        self.assertEqual(Meeting._meta.get_field("modality").default, Meeting.Modality.PHONE)

    def test_existing_calls_stay_readable(self):
        resp = self.client.get(reverse("panel_fragment"), {"item": f"meeting-{self.call.pk}"})
        self.assertEqual(resp.status_code, 200)
