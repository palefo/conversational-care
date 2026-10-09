"""Message export: the flag that gates it, and the shape of the file.

Three things here are worth holding still.

*The flag is off unless someone turns it on.* A download of every client's
messages is something a study needs, not a platform default, and a fresh
installation must not offer it.

*One row per message, with a speaker.* A stored exchange is a client's message
and the reply together; analysis wants them apart, and wants to know who said
each one.

*Nobody is named.* The sender's phone number or email is how the export works
out who spoke. It must not end up in the file.

    python3 manage.py test ConvAI.test_message_export --settings=test_settings
"""
import csv
import datetime as dt
import io
import os
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI import message_export
from ConvAI.models import (
    Agent, Caregiver, Conversation, ConvAIUser, Meeting, Message, Patient,
    SiteConfiguration,
)

CLIENT_PHONE = "+447700900001"
CAREGIVER_PHONE = "+447700900002"


def _turn_on(value="1"):
    cfg = SiteConfiguration.load()
    cfg.message_export_enabled = value
    cfg.save()


def _message(conversation_id, user, said, answered, at=None, **extra):
    m = Message.objects.create(
        conversation_id=str(conversation_id), user=user,
        user_message=said, response_message=answered, **extra)
    if at is not None:
        # timestamp is auto_now_add, so the only way to backdate it is after.
        Message.objects.filter(pk=m.pk).update(timestamp=at)
    return m


def _local(*args):
    return timezone.make_aware(dt.datetime(*args))


class ExportTestCase(TestCase):
    """Every test starts from a cold settings cache — see SenseiTestCase."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = ConvAIUser.objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.caregiver = Caregiver.objects.create(
            name="Grace", lastname="Hopper", phone_number=CAREGIVER_PHONE,
            email="grace@example.org")
        self.patient = Patient.objects.create(
            name="Ada", lastname="Lovelace", phone_number=CLIENT_PHONE,
            caregiver=self.caregiver)
        self.agent = Agent.objects.create(name="Helper", kind="prompt")
        self.conv = Conversation.objects.create(patient=self.patient, agent=self.agent)

    def export(self, **params):
        response = self.client.get(reverse("export_messages"), params)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertTrue(body.startswith("\ufeff"), "Excel needs the BOM to read UTF-8")
        return list(csv.DictReader(io.StringIO(body[1:])))


class TheFlag(ExportTestCase):
    def test_it_is_off_on_a_fresh_installation(self):
        self.assertFalse(message_export.enabled())

    def test_while_it_is_off_the_download_does_not_exist(self):
        response = self.client.get(reverse("export_messages"))
        self.assertEqual(response.status_code, 404)

    def test_the_env_can_turn_it_on(self):
        with patch.dict(os.environ, {"MESSAGE_EXPORT_ENABLED": "1"}):
            self.assertTrue(message_export.enabled())

    def test_off_in_settings_beats_on_in_the_env(self):
        _turn_on("0")
        with patch.dict(os.environ, {"MESSAGE_EXPORT_ENABLED": "1"}):
            self.assertFalse(message_export.enabled())

    def test_saving_the_export_tab_turns_it_on(self):
        response = self.client.post(reverse("config_save"), {
            "section": "export", "message_export_enabled": "1"})
        self.assertEqual(response.status_code, 302)
        cache.clear()
        self.assertTrue(message_export.enabled())

    def test_the_settings_page_offers_the_download_only_when_it_is_on(self):
        url = reverse("export_messages")
        self.assertNotContains(self.client.get(reverse("config")), url)
        _turn_on()
        self.assertContains(self.client.get(reverse("config")), url)


class WhoMayDownload(ExportTestCase):
    def test_a_navigator_may_not_even_with_it_on(self):
        _turn_on()
        nav = ConvAIUser.objects.create_user(username="nav", password="x")
        nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.client.force_login(nav)
        response = self.client.get(reverse("export_messages"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))


class TheFile(ExportTestCase):
    def setUp(self):
        super().setUp()
        _turn_on()

    def test_the_header_is_the_documented_columns(self):
        self.assertEqual(
            list(csv.reader(io.StringIO(
                b"".join(self.client.get(reverse("export_messages")).streaming_content)
                .decode("utf-8")[1:])))[0],
            message_export.COLUMNS)

    def test_an_exchange_becomes_a_client_row_and_an_agent_row(self):
        m = _message(self.conv.id, CLIENT_PHONE, "I slept badly", "I'm sorry to hear that")
        client_row, agent_row = self.export()
        self.assertEqual((client_row["speaker"], client_row["text"]), ("client", "I slept badly"))
        self.assertEqual((agent_row["speaker"], agent_row["text"]), ("agent", "I'm sorry to hear that"))
        for row in (client_row, agent_row):
            self.assertEqual(row["message_id"], str(m.pk))
            self.assertEqual(row["conversation_id"], str(self.conv.id))
            self.assertEqual(row["conversation_kind"], "chat")
            self.assertEqual(row["turn"], "1")
            self.assertEqual(row["patient_id"], str(self.patient.pk))
            self.assertEqual(row["agent_name"], "Helper")

    def test_the_caregiver_is_told_apart_from_the_client(self):
        _message(self.conv.id, CAREGIVER_PHONE, "She slept badly", "Thanks for letting me know")
        self.assertEqual(self.export()[0]["speaker"], "caregiver")

    def test_a_self_registered_client_is_the_client_not_their_own_caregiver(self):
        # Self-registration creates a Caregiver with the client's own number.
        self.caregiver.phone_number = CLIENT_PHONE
        self.caregiver.save()
        _message(self.conv.id, CLIENT_PHONE, "hello", "hi")
        self.assertEqual(self.export()[0]["speaker"], "client")

    def test_a_signed_in_tester_is_an_app_user(self):
        tester = ConvAIUser.objects.create_user(username="tester1", password="x")
        conv = Conversation.objects.create(patient=self.patient, user=tester)
        _message(conv.id, "tester1", "hello", "hi")
        row = self.export()[0]
        self.assertEqual(row["speaker"], "app_user")
        self.assertEqual(row["app_user_id"], str(tester.pk))

    def test_a_reminder_the_platform_sent_is_one_platform_row_for_that_client(self):
        meeting = Meeting.objects.create(patient=self.patient, scheduled_time=timezone.now())
        _message(f"reminder-{meeting.pk}", CAREGIVER_PHONE, "", "Reminder: call tomorrow at 10")
        (row,) = self.export()
        self.assertEqual(row["speaker"], "platform")
        self.assertEqual(row["conversation_kind"], "reminder")
        self.assertEqual(row["patient_id"], str(self.patient.pk))

    def test_a_number_shared_by_two_clients_does_not_guess_which_one(self):
        Patient.objects.create(name="Bob", lastname="Babbage", caregiver=self.caregiver)
        _message("unlinked-thread", CAREGIVER_PHONE, "hello", "hi")
        self.assertEqual(self.export()[0]["patient_id"], "")

    def test_nobody_is_named(self):
        _message(self.conv.id, CLIENT_PHONE, "hello", "hi")
        _message(self.conv.id, CAREGIVER_PHONE, "hello", "hi")
        _message("reminder-999", "grace@example.org", "", "Reminder")
        body = str(self.export())
        for secret in (CLIENT_PHONE, CAREGIVER_PHONE, "grace@example.org",
                       "Ada", "Lovelace", "Grace", "Hopper"):
            self.assertNotIn(secret, body)

    def test_a_message_cannot_run_as_a_spreadsheet_formula(self):
        _message(self.conv.id, CLIENT_PHONE, '=HYPERLINK("http://x","y")', "ok")
        self.assertEqual(self.export()[0]["text"], '\'=HYPERLINK("http://x","y")')

    def test_feedback_is_on_the_reply_not_on_the_client(self):
        _message(self.conv.id, CLIENT_PHONE, "hello", "hi", disliked=True)
        client_row, agent_row = self.export()
        self.assertEqual(client_row["disliked"], "")
        self.assertEqual((agent_row["disliked"], agent_row["liked"]), ("1", "0"))

    def test_a_voice_note_is_flagged(self):
        _message(self.conv.id, CLIENT_PHONE, "(transcript)", "hi", input_audio_file="in.webm")
        client_row, agent_row = self.export()
        self.assertEqual((client_row["has_audio"], agent_row["has_audio"]), ("1", "0"))


class TheDateRange(ExportTestCase):
    def setUp(self):
        super().setUp()
        _turn_on()
        _message(self.conv.id, CLIENT_PHONE, "day one", "a", at=_local(2026, 9, 1, 12))
        _message(self.conv.id, CLIENT_PHONE, "day two, late", "b", at=_local(2026, 9, 2, 23, 30))
        _message(self.conv.id, CLIENT_PHONE, "day three", "c", at=_local(2026, 9, 3, 9))

    def test_both_days_are_included_in_the_platform_timezone(self):
        rows = self.export(**{"from": "2026-09-02", "to": "2026-09-02"})
        self.assertEqual([r["text"] for r in rows], ["day two, late", "b"])

    def test_turns_count_from_the_real_start_of_the_conversation(self):
        rows = self.export(**{"from": "2026-09-02"})
        self.assertEqual([r["turn"] for r in rows], ["2", "2", "3", "3"])

    def test_no_range_is_everything(self):
        self.assertEqual(len(self.export()), 6)

    def test_an_unreadable_date_is_refused_rather_than_ignored(self):
        response = self.client.get(reverse("export_messages"), {"from": "1st September"})
        self.assertEqual(response.status_code, 400)

    def test_a_backwards_range_is_refused(self):
        response = self.client.get(reverse("export_messages"),
                                   {"from": "2026-09-03", "to": "2026-09-01"})
        self.assertEqual(response.status_code, 400)
