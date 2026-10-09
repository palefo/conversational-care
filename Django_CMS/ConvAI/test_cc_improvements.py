"""Three changes asked for together (October 2026).

*Last communication, not last call.* The Patients list says when a client was
last actually in touch — a call that connected, a meeting, or a chat message
they wrote — so someone who only uses the chatbot reads as in touch.

*Communications opens on Happened.* That is where chats, alerts and finished
calls turn up; Coming up is one click away.

*New registrations are hard to miss.* An admin's home page says at the top how
many people are waiting to be approved, and new sign-ups pop up live, like new
alerts do.

    python3 manage.py test ConvAI.test_cc_improvements --settings=test_settings
"""
import datetime as dt
import json

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI.models import (
    CallRecording, Caregiver, ConvAIUser, Meeting, Message, Patient, SelfRegistration,
    SiteConfiguration,
)
from ConvAI.views.patients import last_communication


def _message(patient, said, at, role=Message.SenderRole.CLIENT):
    m = Message.objects.create(conversation_id="c1", user=str(patient.phone_number or "x"),
                               patient=patient, sender_role=role,
                               user_message=said, response_message="ok")
    # timestamp is auto_now_add, so the only way to backdate it is after.
    Message.objects.filter(pk=m.pk).update(timestamp=at)
    return m


def _admin():
    return ConvAIUser.objects.create_user(username="admin", password="x",
                                          is_staff=True, is_superuser=True)


class LastCommunicationTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.p = Patient.objects.create(name="Ana", lastname="Lima", phone_number="+5511900000001",
                                        caregiver=Caregiver.objects.create(name="Rui", lastname="Lima"))

    def ago(self, **kw):
        return self.now - dt.timedelta(**kw)

    def test_nothing_yet(self):
        self.assertIsNone(last_communication(self.p, self.now))

    def test_a_chat_counts_and_is_newer_than_the_call(self):
        Meeting.objects.create(patient=self.p, scheduled_time=self.ago(days=5), ended_at=self.ago(days=5),
                               status=Meeting.Status.COMPLETED)
        _message(self.p, "Olá", self.ago(days=1))
        self.assertEqual(last_communication(self.p, self.now),
                         {"ts": self.ago(days=1), "kind": "chat"})

    def test_a_connected_call_is_newer_than_the_chat(self):
        _message(self.p, "Olá", self.ago(days=3))
        Meeting.objects.create(patient=self.p, scheduled_time=self.ago(days=2), ended_at=self.ago(hours=40),
                               status=Meeting.Status.INTERRUPTED)
        self.assertEqual(last_communication(self.p, self.now),
                         {"ts": self.ago(hours=40), "kind": "call"})

    def test_an_in_person_meeting_is_a_meeting(self):
        Meeting.objects.create(patient=self.p, scheduled_time=self.ago(days=1), ended_at=self.ago(days=1),
                               status=Meeting.Status.COMPLETED, modality=Meeting.Modality.IN_PERSON)
        self.assertEqual(last_communication(self.p, self.now)["kind"], "visit")

    def test_calls_that_did_not_connect_do_not_count(self):
        for status in (Meeting.Status.PENDING, Meeting.Status.NOT_ANSWERED, Meeting.Status.CANCELLED):
            Meeting.objects.create(patient=self.p, scheduled_time=self.ago(hours=2), status=status)
        Meeting.objects.create(patient=self.p, scheduled_time=self.now + dt.timedelta(days=1))
        self.assertIsNone(last_communication(self.p, self.now))

    def test_a_reminder_the_platform_sent_is_not_the_client_writing(self):
        _message(self.p, "", self.ago(hours=1), role=Message.SenderRole.PLATFORM)
        _message(self.p, "", self.ago(hours=1))
        self.assertIsNone(last_communication(self.p, self.now))

    def test_a_call_dialled_outside_the_diary_counts(self):
        CallRecording.objects.create(recording_sid="RE1", from_number="+4499", to_number="+5511900000001",
                                     patient=self.p, start_time=self.ago(hours=3),
                                     end_time=self.ago(hours=2), duration=600)
        self.assertEqual(last_communication(self.p, self.now),
                         {"ts": self.ago(hours=3), "kind": "call"})

    def test_the_patients_list_shows_it(self):
        _message(self.p, "Olá", self.ago(days=1))
        self.client.force_login(_admin())
        resp = self.client.get(reverse("patients"))
        self.assertContains(resp, "Last communication")
        self.assertNotContains(resp, "Last call")
        row = json.loads(resp.context["rows_json"])[0]
        self.assertEqual((row["last_comm_kind"], row["last_comm_label"]), ("chat", "Chat"))
        self.assertEqual(row["last_comm_iso"], self.ago(days=1).isoformat())


class CommunicationsDefaultTests(TestCase):
    def setUp(self):
        self.client.force_login(_admin())

    def test_opens_on_happened(self):
        self.assertEqual(self.client.get(reverse("communications")).context["tab"], "past")

    def test_coming_up_is_still_there(self):
        self.assertEqual(self.client.get(reverse("communications"), {"tab": "up"}).context["tab"], "up")

    def test_an_item_still_picks_its_own_tab(self):
        p = Patient.objects.create(name="Ana", lastname="Lima")
        m = Meeting.objects.create(patient=p, scheduled_time=timezone.now() + dt.timedelta(days=1))
        resp = self.client.get(reverse("communications"), {"item": m.panel_token})
        self.assertEqual(resp.context["tab"], "up")


class RegistrationNoticeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        cfg = SiteConfiguration.load()
        cfg.self_registration_enabled = "1"
        cfg.save()
        self.admin = _admin()
        self.client.force_login(self.admin)

    def _register(self, name):
        return SelfRegistration.objects.create(name=name, lastname="Yoon", phone_number=None,
                                               details={"source": "self-registration-agent"})

    def test_the_home_page_says_how_many_are_waiting(self):
        for n in ("Cho", "Min", "Ha", "Seo"):
            self._register(n)
        resp = self.client.get(reverse("dashboard"))
        waiting = resp.context["waiting_registrations"]
        self.assertEqual((waiting["count"], waiting["more"]), (4, 1))
        self.assertContains(resp, "4 new registrations waiting to be approved")
        self.assertContains(resp, "?tab=registrations#registrations")

    def test_no_banner_when_nobody_is_waiting(self):
        resp = self.client.get(reverse("dashboard"))
        self.assertIsNone(resp.context["waiting_registrations"])
        self.assertNotContains(resp, "waiting to be approved")

    def test_no_banner_for_a_navigator(self):
        self._register("Cho")
        nav = ConvAIUser.objects.create_user(username="nav", password="x")
        nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.client.force_login(nav)
        self.assertIsNone(self.client.get(reverse("dashboard")).context["waiting_registrations"])

    def test_a_new_sign_up_pops_up_live_for_an_admin(self):
        clock = self.client.get(reverse("alerts_since")).json()["now"]
        self._register("Cho")
        alerts = self.client.get(reverse("alerts_since"), {"t": clock - 1000}).json()["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Cho Yoon", alerts[0]["title"])
        self.assertIn("?tab=registrations", alerts[0]["href"])

    def test_not_for_a_navigator_and_not_while_switched_off(self):
        clock = self.client.get(reverse("alerts_since")).json()["now"]
        self._register("Cho")
        nav = ConvAIUser.objects.create_user(username="nav", password="x")
        nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.client.force_login(nav)
        self.assertEqual(self.client.get(reverse("alerts_since"), {"t": clock - 1000}).json()["alerts"], [])
        cfg = SiteConfiguration.load()
        cfg.self_registration_enabled = "0"
        cfg.save()
        cache.clear()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("alerts_since"), {"t": clock - 1000}).json()["alerts"], [])
