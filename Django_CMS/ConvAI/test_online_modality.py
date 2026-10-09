"""The online modality in the core: it never dials, it is named for what it
is, it is only offered while available, and an add-on cannot break a page.

    python3 manage.py test ConvAI.test_online_modality --settings=test_settings
"""
from unittest import mock

from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ConvAI import extensions
from ConvAI.forms import MeetingForm
from ConvAI.models import Caregiver, ConvAIUser, Meeting, Patient

CARER_PHONE = "+447222222222"


def _navigator(username="nav"):
    user = ConvAIUser.objects.create_user(username=username, password="x", phone_number="+447000000001")
    user.groups.add(Group.objects.get_or_create(name="Navigator")[0])
    return user


class _Provider:
    def __init__(self, on=True):
        self.on = on

    def available(self):
        return self.on

    def reminder_extras(self, meeting):
        return {"join_url": "https://care.example/m/abc/def"}

    def panel_extras(self, request, meeting):
        raise RuntimeError("an add-on bug")


class OnlineModality(TestCase):
    def setUp(self):
        self.nav = _navigator()
        self.carer = Caregiver.objects.create(name="Ana", lastname="Ortega", phone_number=CARER_PHONE,
                                              email="ana@example.org")
        self.p = Patient.objects.create(name="Manuel", lastname="Ortega", caregiver=self.carer,
                                        navigator=self.nav)
        self.m = Meeting.objects.create(patient=self.p, scheduled_time=timezone.now(),
                                        modality=Meeting.Modality.ONLINE)
        self._saved = extensions._provider

    def tearDown(self):
        extensions._provider = self._saved

    def test_never_dials(self):
        self.assertIsNone(self.m.dial_recipient)
        self.assertEqual(self.m.kind, "online")
        phone = Meeting.objects.create(patient=self.p, scheduled_time=timezone.now())
        self.assertEqual(phone.kind, "call")
        self.assertIsNotNone(phone.dial_recipient)

    def test_make_phone_call_refuses_an_online_meeting(self):
        self.client.force_login(self.nav)
        with mock.patch("ConvAI.views.calls.make_phone_conference") as mp:
            resp = self.client.post(reverse("make_phone_call", args=[self.m.pk]))
        mp.assert_not_called()
        self.assertGreaterEqual(resp.status_code, 400)

    def test_form_offers_online_only_when_available(self):
        extensions._provider = None
        values = [v for v, _l in MeetingForm().fields["modality"].choices]
        self.assertNotIn(Meeting.Modality.ONLINE, values)
        # …but keeps it for a meeting that already is one.
        values = [v for v, _l in MeetingForm(instance=self.m).fields["modality"].choices]
        self.assertIn(Meeting.Modality.ONLINE, values)
        extensions._provider = _Provider(on=True)
        values = [v for v, _l in MeetingForm().fields["modality"].choices]
        self.assertIn(Meeting.Modality.ONLINE, values)

    def test_cannot_book_online_by_posting_the_value_when_off(self):
        extensions._provider = None
        form = MeetingForm(data={"patient": self.p.pk, "modality": Meeting.Modality.ONLINE,
                                 "type": Meeting.MeetingType.REGULAR,
                                 "scheduled_time": "2030-01-01T10:00"})
        form.fields["patient"].queryset = Patient.objects.all()
        self.assertFalse(form.is_valid())
        self.assertIn("modality", form.errors)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_email_reminder_carries_the_link(self):
        from ConvAI.mailer import send_meeting_reminder_email
        extensions._provider = _Provider(on=True)
        with mock.patch("ConvAI.mailer.send_email") as send:
            send_meeting_reminder_email(self.m)
        kwargs = send.call_args.kwargs
        self.assertIn("https://care.example/m/abc/def", kwargs["text"])
        self.assertIn("online", kwargs["text"].lower())

    def test_whatsapp_reminder_is_not_offered(self):
        from ConvAI.utils import reminder_recipient_missing, send_meeting_reminder
        with mock.patch("ConvAI.mailer.reminder_channel", return_value="whatsapp"):
            self.assertEqual(reminder_recipient_missing(self.m), "online")
            with self.assertRaises(ValueError):
                send_meeting_reminder(self.m)

    def test_a_failing_add_on_cannot_break_the_panel(self):
        extensions._provider = _Provider(on=True)
        self.client.force_login(self.nav)
        resp = self.client.get(reverse("panel_fragment"), {"item": self.m.panel_token})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Online meeting")

    def test_panel_without_the_app_still_reads(self):
        extensions._provider = None
        self.client.force_login(self.nav)
        resp = self.client.get(reverse("panel_fragment"), {"item": self.m.panel_token})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Online meetings are off")
        # No way to place a phone call from an online meeting's panel.
        self.assertNotContains(resp, 'id="dpcStart"')
        self.assertNotContains(resp, reverse("make_phone_call", args=[self.m.pk]))

    def test_lists_name_it_online(self):
        self.client.force_login(self.nav)
        resp = self.client.get(reverse("communications"), {"tab": "up"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "cm-ic-online")
        self.assertContains(resp, "Online meeting")

    def test_signals_fire_on_cancel(self):
        got = []
        handler = lambda sender, meeting, by=None, **kw: got.append(meeting.pk)  # noqa: E731
        extensions.meeting_cancelled.connect(handler, dispatch_uid="t-cancel")
        try:
            self.client.force_login(self.nav)
            self.client.post(reverse("cancel_meeting", args=[self.m.pk]), {"reason": "x"})
        finally:
            extensions.meeting_cancelled.disconnect(dispatch_uid="t-cancel")
        self.assertEqual(got, [self.m.pk])
