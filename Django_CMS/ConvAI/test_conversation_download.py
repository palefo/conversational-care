"""Conversation downloads, and the chat pane cut into its conversations.

*The switch is off unless someone turns it on*, and it is its own switch: an
installation can let navigators take one conversation without letting admins
take everything, and the other way round.

*Only your own client's conversation, and only its messages.* The id in the URL
is typed by whoever is asking, so it is checked against the client, and the
file carries only what the panel itself would have shown.

*A day of chat is split where each conversation starts and ends*, in the order
things happened.

    python3 manage.py test ConvAI.test_conversation_download --settings=test_settings
"""
import csv
import datetime as dt
import io
import os
import uuid
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI import message_export
from ConvAI.models import (
    Agent, Caregiver, Conversation, ConvAIUser, Message, Patient, SiteConfiguration,
)

CLIENT_PHONE = "+447700900001"
CAREGIVER_PHONE = "+447700900002"
OTHER_PHONE = "+447700900003"
DAY = dt.date(2026, 9, 10)


def _turn_on(value="1"):
    cfg = SiteConfiguration.load()
    cfg.conversation_download_enabled = value
    cfg.save()


def _local(*args):
    return timezone.make_aware(dt.datetime(*args))


def _message(conversation_id, user, said, answered, at):
    m = Message.objects.create(conversation_id=str(conversation_id), user=user,
                               user_message=said, response_message=answered)
    # timestamp is auto_now_add, so the only way to backdate it is after.
    Message.objects.filter(pk=m.pk).update(timestamp=at)
    return m


class DownloadTestCase(TestCase):
    """Every test starts from a cold settings cache — see SenseiTestCase."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        navigators = Group.objects.get_or_create(name="Navigator")[0]
        self.nav = ConvAIUser.objects.create_user(username="nav", password="x")
        self.nav.groups.add(navigators)
        self.other_nav = ConvAIUser.objects.create_user(username="other", password="x")
        self.other_nav.groups.add(navigators)
        self.client.force_login(self.nav)

        caregiver = Caregiver.objects.create(name="Grace", lastname="Hopper",
                                             phone_number=CAREGIVER_PHONE)
        self.patient = Patient.objects.create(name="Ada", lastname="Lovelace",
                                              phone_number=CLIENT_PHONE,
                                              caregiver=caregiver, navigator=self.nav)
        self.stranger = Patient.objects.create(name="Bob", lastname="Babbage",
                                               phone_number=OTHER_PHONE,
                                               navigator=self.other_nav)
        agent = Agent.objects.create(name="Helper", kind="prompt")
        self.morning = Conversation.objects.create(patient=self.patient, agent=agent)
        self.evening = Conversation.objects.create(patient=self.patient, agent=agent)

        _message(self.morning.id, CLIENT_PHONE, "good morning", "hello Ada", _local(2026, 9, 10, 9, 0))
        _message(self.morning.id, CLIENT_PHONE, "slept badly", "sorry to hear", _local(2026, 9, 10, 9, 5))
        _message(self.evening.id, CAREGIVER_PHONE, "she is tired", "thanks", _local(2026, 9, 10, 20, 0))

    def url(self, conversation_id, patient=None):
        return reverse("download_conversation",
                       args=[(patient or self.patient).pk, str(conversation_id)])

    def download(self, conversation_id, patient=None):
        response = self.client.get(self.url(conversation_id, patient))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertTrue(body.startswith("﻿"), "Excel needs the BOM to read UTF-8")
        return list(csv.DictReader(io.StringIO(body[1:])))

    def pane(self, day=DAY):
        response = self.client.get(reverse("panel_fragment"),
                                   {"item": f"chat-{self.patient.pk}-{day}"})
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")


class TheSwitch(DownloadTestCase):
    def test_it_is_off_on_a_fresh_installation(self):
        self.assertFalse(message_export.conversation_download_enabled())

    def test_while_it_is_off_the_download_does_not_exist(self):
        self.assertEqual(self.client.get(self.url(self.morning.id)).status_code, 404)

    def test_the_env_can_turn_it_on(self):
        with patch.dict(os.environ, {"CONVERSATION_DOWNLOAD_ENABLED": "1"}):
            self.assertTrue(message_export.conversation_download_enabled())

    def test_it_is_not_the_full_export_switch(self):
        cfg = SiteConfiguration.load()
        cfg.message_export_enabled = "1"
        cfg.save()
        self.assertFalse(message_export.conversation_download_enabled())
        self.assertEqual(self.client.get(self.url(self.morning.id)).status_code, 404)

    def test_saving_the_export_tab_keeps_both_switches(self):
        admin = ConvAIUser.objects.create_user(username="admin", password="x",
                                               is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        self.client.post(reverse("config_save"), {
            "section": "export", "message_export_enabled": "1",
            "conversation_download_enabled": "1"})
        cache.clear()
        self.assertTrue(message_export.enabled())
        self.assertTrue(message_export.conversation_download_enabled())


class WhoMayDownload(DownloadTestCase):
    def setUp(self):
        super().setUp()
        _turn_on()

    def test_the_clients_navigator_may(self):
        self.assertEqual(len(self.download(self.morning.id)), 4)

    def test_an_admin_may(self):
        admin = ConvAIUser.objects.create_user(username="admin", password="x",
                                               is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        self.assertEqual(len(self.download(self.morning.id)), 4)

    def test_another_navigator_may_not(self):
        self.client.force_login(self.other_nav)
        self.assertEqual(self.client.get(self.url(self.morning.id)).status_code, 404)

    def test_your_client_cannot_be_used_to_reach_someone_elses_conversation(self):
        theirs = uuid.uuid4()
        _message(theirs, OTHER_PHONE, "private", "reply", _local(2026, 9, 10, 10))
        self.assertEqual(self.client.get(self.url(theirs)).status_code, 404)

    def test_a_conversation_that_does_not_exist_is_not_found(self):
        self.assertEqual(self.client.get(self.url(uuid.uuid4())).status_code, 404)


class TheFile(DownloadTestCase):
    def setUp(self):
        super().setUp()
        _turn_on()

    def test_it_is_that_conversation_and_nothing_else(self):
        rows = self.download(self.morning.id)
        self.assertEqual({r["conversation_id"] for r in rows}, {str(self.morning.id)})
        self.assertEqual([r["text"] for r in rows],
                         ["good morning", "hello Ada", "slept badly", "sorry to hear"])

    def test_it_has_the_full_exports_columns(self):
        response = self.client.get(self.url(self.morning.id))
        body = b"".join(response.streaming_content).decode("utf-8")[1:]
        self.assertEqual(next(csv.reader(io.StringIO(body))), message_export.COLUMNS)

    def test_it_is_the_whole_conversation_not_just_the_day(self):
        _message(self.evening.id, CAREGIVER_PHONE, "still awake", "ok", _local(2026, 9, 11, 0, 30))
        rows = self.download(self.evening.id)
        self.assertEqual([r["turn"] for r in rows], ["1", "1", "2", "2"])

    def test_it_is_named_by_when_it_started_and_not_by_who(self):
        response = self.client.get(self.url(self.morning.id))
        disposition = response["Content-Disposition"]
        self.assertIn("conversation_20260910_0900_", disposition)
        self.assertNotIn("Ada", disposition)


class ThePane(DownloadTestCase):
    def test_a_day_is_split_into_its_conversations(self):
        html = self.pane()
        self.assertEqual(html.count('class="dp-conv"'), 2)
        self.assertIn("Conversation 1", html)
        self.assertIn("09:00 – 09:05", html)
        self.assertIn("Conversation 2", html)
        self.assertIn("20:00", html)

    def test_one_conversation_is_not_numbered(self):
        Message.objects.filter(conversation_id=str(self.evening.id)).delete()
        html = self.pane()
        self.assertEqual(html.count('class="dp-conv"'), 1)
        self.assertNotIn("Conversation 1", html)

    def test_no_download_while_the_switch_is_off(self):
        self.assertNotIn("dp-conv-dl", self.pane())

    def test_each_conversation_has_its_own_download_when_it_is_on(self):
        _turn_on()
        html = self.pane()
        self.assertIn(self.url(self.morning.id), html)
        self.assertIn(self.url(self.evening.id), html)

    def test_order_is_kept_and_an_interrupted_conversation_says_it_carried_on(self):
        _message("reminder-1", CAREGIVER_PHONE, "", "Reminder: call at 10", _local(2026, 9, 10, 9, 2))
        html = self.pane()
        self.assertEqual(html.count('class="dp-conv"'), 4)
        self.assertLess(html.index("Reminder"), html.index("Conversation 1, continued"))

    def test_a_conversation_from_the_night_before_says_when_it_began(self):
        _message(self.morning.id, CLIENT_PHONE, "cannot sleep", "try to rest", _local(2026, 9, 9, 23, 50))
        self.assertIn("Began earlier, on 9 Sep, 23:50.", self.pane())

    def test_a_conversation_that_runs_past_midnight_says_so(self):
        _message(self.evening.id, CAREGIVER_PHONE, "still awake", "ok", _local(2026, 9, 11, 0, 30))
        self.assertIn("Carries on until 11 Sep, 00:30.", self.pane())
