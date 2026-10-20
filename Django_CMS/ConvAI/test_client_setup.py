"""Setting a platform up for clients who speak for themselves (client_setup.md).

*A registration is the person, not their caregiver.* Approving one makes a
client with no caregiver, links the registration to them, and can assign a
navigator. Auto-approval does the same the moment the request is filed, with
the agent chosen in Settings, and needs one chosen.

*The wording is the installation's.* What the registration agent says, what a
navigator is called, whether caregivers and meeting types are asked about.

*Pronouns, where recorded, reach the summaries and remote agents.*

*An admin's calendar shows everybody's meetings and can be filtered by who
booked them; a failed booking says why.*

    python3 manage.py test ConvAI.test_client_setup --settings=test_settings
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI import registration
from ConvAI.forms import ClientForm, MeetingForm, PatientForm, RegistrationConfigForm
from ConvAI.models import (Agent, Caregiver, ConvAIUser, Meeting, Patient, SelfRegistration,
                           SiteConfiguration)
from ConvAI.native_agents.self_registration import _create_self_registration
from ConvAI.site_config import navigator_term
from ConvAI.utils_conversation_classification import _build_prompt


def _config(**kwargs):
    cfg = SiteConfiguration.load()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    cfg.save()
    cache.clear()


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = ConvAIUser.objects.create_superuser("root", "r@example.org", "x")
        self.lw = ConvAIUser.objects.create_user("soan", password="x", first_name="Soan")
        self.lw.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.reco = Agent.objects.create(name="RECO v2", kind="remote", langgraph_name="reco_v2",
                                         host="localhost", port=8123)
        self.sr = SelfRegistration.objects.create(
            name="Ada", lastname="Lovelace", phone_number="+447700900001",
            details={"source": "self-registration-agent", "channel": "sms"})


class Approval(Base):
    def test_the_client_is_not_filed_as_their_own_caregiver(self):
        with patch("ConvAI.utils.send_sms_text", return_value=True):
            patient, sent = registration.approve(self.sr, agent=self.reco, navigator=self.lw)
        self.assertIsNone(patient.caregiver)
        self.assertEqual(Caregiver.objects.count(), 0)
        self.assertEqual((patient.agent, patient.navigator), (self.reco, self.lw))
        self.sr.refresh_from_db()
        self.assertEqual((self.sr.state, self.sr.patient), (SelfRegistration.State.APPROVED, patient))
        self.assertTrue(sent)

    def test_the_welcome_goes_on_the_channel_they_used(self):
        _config(self_reg_welcome_message="You're in.")
        with patch("ConvAI.utils.send_sms_text", return_value=True) as sms, \
                patch("ConvAI.utils.send_whatsapp_text") as wa:
            registration.approve(self.sr)
        sms.assert_called_once_with("+447700900001", "You're in.")
        wa.assert_not_called()

    def test_approving_twice_makes_one_client(self):
        with patch("ConvAI.utils.send_sms_text", return_value=True) as sms:
            first, _ = registration.approve(self.sr)
            again, sent = registration.approve(self.sr)
        self.assertEqual(first, again)
        self.assertFalse(sent)
        self.assertEqual(Patient.objects.count(), 1)
        self.assertEqual(sms.call_count, 1)

    def test_the_button_assigns_the_navigator(self):
        self.client.force_login(self.admin)
        with patch("ConvAI.utils.send_sms_text", return_value=True):
            resp = self.client.post(reverse("approve_self_registration", args=[self.sr.pk]),
                                    {"agent_id": self.reco.pk, "navigator_id": self.lw.pk})
        self.assertRedirects(resp, f"{reverse('config')}?tab=registrations#registrations",
                             fetch_redirect_response=False)
        patient = Patient.objects.get()
        self.assertEqual((patient.agent, patient.navigator), (self.reco, self.lw))

    def test_only_a_navigator_can_be_assigned(self):
        self.client.force_login(self.admin)
        with patch("ConvAI.utils.send_sms_text", return_value=True):
            self.client.post(reverse("approve_self_registration", args=[self.sr.pk]),
                             {"navigator_id": self.admin.pk})
        self.assertIsNone(Patient.objects.get().navigator)


class AutoApproval(Base):
    def test_off_it_waits_for_an_admin(self):
        out = _create_self_registration("Grace", "Hopper", "+447700900002", channel="whatsapp")
        self.assertFalse(out["approved"])
        self.assertFalse(Patient.objects.exists())

    def test_on_they_are_a_client_straight_away_with_no_second_message(self):
        _config(self_reg_auto_approve="1", self_reg_client_agent=self.reco)
        with patch("ConvAI.utils.send_whatsapp_text") as wa:
            out = _create_self_registration("Grace", "Hopper", "+447700900002", channel="whatsapp")
        self.assertTrue(out["approved"])
        patient = Patient.objects.get(name="Grace")
        self.assertEqual(patient.agent, self.reco)
        self.assertIsNone(patient.navigator)
        wa.assert_not_called()

    def test_it_cannot_be_switched_on_without_an_agent(self):
        cfg = SiteConfiguration.load()
        form = RegistrationConfigForm({"self_reg_auto_approve": "1"}, instance=cfg)
        self.assertFalse(form.is_valid())
        self.assertIn("self_reg_client_agent", form.errors)
        form = RegistrationConfigForm({"self_reg_auto_approve": "1",
                                       "self_reg_client_agent": self.reco.pk}, instance=cfg)
        self.assertTrue(form.is_valid(), form.errors)


class Messages(Base):
    def test_defaults_until_set(self):
        self.assertIn("in touch", registration.pending_message())
        self.assertNotIn("notified", registration.pending_message())
        _config(self_reg_pending_message="Our research team will be in touch with you soon.")
        self.assertEqual(registration.pending_message(),
                         "Our research team will be in touch with you soon.")

    def test_the_agent_is_told_what_to_say(self):
        _config(self_reg_pending_message="Our research team will be in touch with you soon.")
        from ConvAI.native_agents import self_registration
        captured = {}

        def fake_react(model, tools, state_modifier, checkpointer):
            captured["prompt"] = state_modifier
            return object()

        with patch("langgraph.prebuilt.create_react_agent", side_effect=fake_react), \
                patch("ConvAI.llm_factory.make_llm"):
            self_registration.build(None)
        system = captured["prompt"]({"messages": []}, {"configurable": {}})[0]["content"]
        self.assertIn("Our research team will be in touch with you soon.", system)


class Wording(Base):
    def test_navigator_by_default_and_the_installations_term_once_set(self):
        self.assertEqual(navigator_term(), "Navigator")
        _config(navigator_term="Link worker")
        self.assertEqual((navigator_term(), navigator_term(plural=True)),
                         ("Link worker", "Link workers"))

    def test_the_forms_and_pages_use_it(self):
        _config(navigator_term="Link worker")
        self.assertEqual(ClientForm(is_admin=True).fields["navigator"].label, "Link worker")
        self.client.force_login(self.admin)
        patient = Patient.objects.create(name="Ada", lastname="L", navigator=self.lw)
        self.assertContains(self.client.get(reverse("patient_detail", args=[patient.pk])),
                            "<dt>Link worker</dt>", html=False)
        self.assertContains(self.client.get(reverse("patients")), "Link worker")

    def test_the_navigator_list_is_the_navigator_group(self):
        choices = list(PatientForm(is_admin=True).fields["navigator"].queryset)
        self.assertEqual(choices, [self.lw])


class Caregivers(Base):
    def test_asked_for_by_default(self):
        self.assertIn("caregiver_name", ClientForm(is_admin=True).fields)

    def test_switched_off_they_are_not(self):
        _config(caregivers_enabled="0")
        for form in (ClientForm(is_admin=True), PatientForm(is_admin=True)):
            self.assertFalse(any(name.startswith("caregiver_") for name in form.fields))
        self.client.force_login(self.admin)
        plain = Patient.objects.create(name="Ada", lastname="L")
        self.assertNotContains(self.client.get(reverse("patient_detail", args=[plain.pk])),
                               "Caregiver phone")

    def test_one_already_on_file_is_still_shown(self):
        _config(caregivers_enabled="0")
        self.client.force_login(self.admin)
        with_one = Patient.objects.create(
            name="Ada", lastname="L", caregiver=Caregiver.objects.create(name="Mum", lastname="L"))
        self.assertContains(self.client.get(reverse("patient_detail", args=[with_one.pk])),
                            "Caregiver phone")


class Pronouns(Base):
    def test_the_review_uses_them(self):
        system = _build_prompt(None, "Patient: hi", "she/her")[0].content
        self.assertIn("pronouns are she/her", system)

    def test_and_says_nothing_when_unknown(self):
        self.assertNotIn("pronouns", _build_prompt(None, "Patient: hi", "")[0].content)

    def test_the_review_is_given_the_clients(self):
        from ConvAI import conversation_alerts
        from ConvAI.models import Conversation, Message
        patient = Patient.objects.create(name="Ada", lastname="L", pronouns="she/her")
        conv = Conversation.objects.create(patient=patient)
        Message.objects.create(conversation_id=str(conv.id), user="x", patient=patient,
                               user_message="hi", response_message="hello")
        with patch("ConvAI.conversation_alerts.classify_conversation_with_llm",
                   return_value={"abstract": "", "classification": "Other", "important": False,
                                 "detectors": {}, "triggers": {}}) as classify:
            conversation_alerts.review_conversation(conv.id)
        self.assertEqual(classify.call_args.kwargs["pronouns"], "she/her")

    def test_remote_agents_get_them_when_recorded(self):
        from ConvAI.utils import _run_identity
        patient = Patient.objects.create(name="Ada", lastname="L", pronouns="he/they")
        self.assertEqual(_run_identity(self.reco, patient, "t")["client_pronouns"], "he/they")
        patient.pronouns = ""
        self.assertNotIn("client_pronouns", _run_identity(self.reco, patient, "t"))


class MeetingTypes(Base):
    def test_asked_by_default(self):
        self.assertIn("type", MeetingForm().fields)

    def test_switched_off_a_new_meeting_is_a_protocol_one(self):
        _config(meeting_types_enabled="0", phone_calls_enabled="0")
        patient = Patient.objects.create(name="Ada", lastname="L")
        form = MeetingForm({"patient": patient.pk, "modality": Meeting.Modality.IN_PERSON,
                            "location": "Clinic", "scheduled_time": "2026-11-02T10:00"})
        self.assertNotIn("type", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().type, Meeting.MeetingType.REGULAR)


class Calendar(Base):
    def setUp(self):
        super().setUp()
        _config(phone_calls_enabled="0")
        self.ada = Patient.objects.create(name="Ada", lastname="L", navigator=self.lw)
        self.client.force_login(self.admin)

    def book(self, **extra):
        data = {"patient": self.ada.pk, "modality": Meeting.Modality.IN_PERSON,
                "location": "Clinic", "type": Meeting.MeetingType.REGULAR,
                "scheduled_time": "2026-11-02T10:00", "view": "week", "offset": 0}
        data.update(extra)
        return self.client.post(reverse("calendar_create_meeting"), data, follow=True)

    def test_a_booking_records_who_made_it(self):
        self.book()
        self.assertEqual(Meeting.objects.get().created_by, self.admin)

    def test_a_failed_booking_says_why(self):
        resp = self.book(location="")
        self.assertFalse(Meeting.objects.exists())
        self.assertContains(resp, "Say where the meeting takes place.")

    def test_unassigned_clients_do_not_clash_with_each_other(self):
        other = Patient.objects.create(name="Bea", lastname="M")
        when = timezone.make_aware(timezone.datetime(2026, 11, 2, 10, 0))
        Meeting.objects.create(patient=other, scheduled_time=when,
                               modality=Meeting.Modality.IN_PERSON, location="x")
        loner = Patient.objects.create(name="Cy", lastname="N")
        self.book(patient=loner.pk)
        self.assertTrue(Meeting.objects.filter(patient=loner).exists())

    def test_admins_can_filter_by_who_booked(self):
        # Midday today, local time: inside the day view's 07:00-21:00 grid
        # whatever time the suite runs (now + 1h fell off it late at night).
        when = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
        mine = Meeting.objects.create(patient=self.ada, scheduled_time=when, created_by=self.admin,
                                      modality=Meeting.Modality.IN_PERSON, location="x")
        theirs = Meeting.objects.create(patient=self.ada, scheduled_time=when, created_by=self.lw,
                                        modality=Meeting.Modality.IN_PERSON, location="x")
        older = Meeting.objects.create(patient=self.ada, scheduled_time=when,
                                       modality=Meeting.Modality.IN_PERSON, location="x")

        def shown(**params):
            ctx = self.client.get(reverse("calendar"), {"view": "day", **params}).context
            return {m.pk for row in ctx["grid_rows"] for c in row["cells"] for m in c["meetings"]}

        self.assertEqual(shown(), {mine.pk, theirs.pk, older.pk})
        self.assertEqual(shown(by=self.lw.pk), {theirs.pk})
        self.assertEqual(shown(by="none"), {older.pk})

    def test_a_navigator_sees_their_own_clients_and_no_filter(self):
        self.client.force_login(self.lw)
        resp = self.client.get(reverse("calendar"))
        self.assertEqual(resp.context["by_options"], [])


class HomeAlert(Base):
    """An auto-approved client with nobody assigned is on the admin's Home until
    a navigator is set — and only while self-registration is on."""

    def setUp(self):
        super().setUp()
        self.sr.delete()  # only the auto-approved client below, nobody waiting
        _config(self_registration_enabled="1", self_reg_auto_approve="1",
                self_reg_client_agent=self.reco, navigator_term="Link worker")
        _create_self_registration("Grace", "Hopper", "+447400123402", channel="sms")
        self.grace = Patient.objects.get(name="Grace")
        self.client.force_login(self.admin)

    def home(self):
        return self.client.get(reverse("dashboard"))

    def test_it_is_on_home(self):
        resp = self.home()
        self.assertContains(resp, "1 new client needs a link worker")
        self.assertContains(resp, "Assign link worker")
        self.assertContains(resp, reverse("edit_patient", args=[self.grace.pk]))
        self.assertEqual(resp.context["alert_counts"]["registrations"], 1)

    def test_it_goes_once_someone_is_assigned(self):
        self.grace.navigator = self.lw
        self.grace.save()
        resp = self.home()
        self.assertNotContains(resp, "needs a link worker")
        self.assertEqual(resp.context["alert_counts"]["registrations"], 0)

    def test_a_link_worker_does_not_see_it(self):
        self.client.force_login(self.lw)
        self.assertNotContains(self.home(), "needs a link worker")

    def test_it_toasts_once_when_it_happens(self):
        before = int((timezone.now() - timedelta(minutes=1)).timestamp() * 1000)
        rows = self.client.get(reverse("alerts_since"), {"t": before}).json()["alerts"]
        self.assertEqual([r["title"] for r in rows], ["New client — Grace Hopper"])
        self.assertEqual(rows[0]["href"], reverse("edit_patient", args=[self.grace.pk]))
