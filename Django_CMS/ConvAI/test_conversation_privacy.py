"""Conversations a client asked their link worker not to read.

*The switch is off unless somebody turns it on*, and while it is off the API
does not exist.

*Hidden means the words, not the fact.* A navigator still sees that the
conversation happened, when it ran and how many messages it held; the content,
the classifier's summary, the topic and the review go.

*Three things the promise does not cover*: admins, the self-harm floor, and the
switch being turned off afterwards — a promise already made keeps holding.

    python3 manage.py test ConvAI.test_conversation_privacy --settings=test_settings
"""
import datetime as dt
import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from ConvAI import conversation_privacy
from ConvAI.models import (
    Agent, Caregiver, Conversation, ConvAIUser, Message, Patient, SiteConfiguration,
)
from ConvAI.utils_conversation_classification import SAFETY_LABEL

CLIENT_PHONE = "+447700900101"
CAREGIVER_PHONE = "+447700900102"
DAY = dt.date(2026, 9, 10)


def _local(*args):
    return timezone.make_aware(dt.datetime(*args))


def _message(conversation_id, user, said, answered, at):
    m = Message.objects.create(conversation_id=str(conversation_id), user=user,
                               user_message=said, response_message=answered)
    # timestamp is auto_now_add, so the only way to backdate it is after.
    Message.objects.filter(pk=m.pk).update(timestamp=at)
    return m


class PrivacyTestCase(TestCase):
    """Every test starts from a cold settings cache — see SenseiTestCase."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

        navigators = Group.objects.get_or_create(name="Navigator")[0]
        self.nav = ConvAIUser.objects.create_user(username="nav", password="x")
        self.nav.groups.add(navigators)
        self.admin = ConvAIUser.objects.create_user(username="boss", password="x")
        self.admin.user_permissions.add(
            Permission.objects.get(codename="access_configuration"))

        caregiver = Caregiver.objects.create(name="Grace", lastname="Hopper",
                                             phone_number=CAREGIVER_PHONE)
        self.patient = Patient.objects.create(name="Ada", lastname="Lovelace",
                                              phone_number=CLIENT_PHONE,
                                              caregiver=caregiver, navigator=self.nav)
        agent = Agent.objects.create(name="Helper", kind="prompt")
        self.morning = Conversation.objects.create(patient=self.patient, agent=agent,
                                                   topic="Sleep", summary="Ada slept badly.")
        self.evening = Conversation.objects.create(patient=self.patient, agent=agent,
                                                   topic="Meals", summary="Ada ate well.")

        _message(self.morning.id, CLIENT_PHONE, "I slept badly", "sorry to hear",
                 _local(2026, 9, 10, 9, 0))
        _message(self.morning.id, CLIENT_PHONE, "very badly", "tell me more",
                 _local(2026, 9, 10, 9, 5))
        _message(self.evening.id, CAREGIVER_PHONE, "she ate well", "good news",
                 _local(2026, 9, 10, 20, 0))

        self.client.force_login(self.nav)

    # --- helpers ---

    def turn_on(self, value="1"):
        cfg = SiteConfiguration.load()
        cfg.conversation_privacy_enabled = value
        cfg.save()
        cache.clear()

    def hide(self, conversation):
        conversation.hidden = True
        conversation.save(update_fields=["hidden"])
        return conversation

    def pane(self, day=DAY):
        response = self.client.get(reverse("panel_fragment"),
                                   {"item": f"chat-{self.patient.pk}-{day}"})
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def history(self, day=DAY):
        response = self.client.get(
            reverse("patient_conversation_detail",
                    kwargs={"pk": self.patient.pk, "day": day.isoformat()}))
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")


class TheSwitch(PrivacyTestCase):
    def test_it_is_off_on_a_fresh_installation(self):
        self.assertFalse(conversation_privacy.enabled())

    def test_env_turns_it_on(self):
        with patch.dict(os.environ, {"CONVERSATION_PRIVACY_ENABLED": "1"}):
            self.assertTrue(conversation_privacy.enabled())

    def test_the_database_wins_over_env(self):
        with patch.dict(os.environ, {"CONVERSATION_PRIVACY_ENABLED": "1"}):
            self.turn_on("0")
            self.assertFalse(conversation_privacy.enabled())

    def test_turning_it_off_does_not_unhide_anything(self):
        """The switch gates taking the promise, never keeping one already made."""
        self.turn_on("1")
        self.hide(self.morning)
        self.turn_on("0")
        self.assertTrue(conversation_privacy.is_withheld(self.morning, self.nav))
        self.assertNotIn("I slept badly", self.pane())


class WhatIsWithheld(PrivacyTestCase):
    def setUp(self):
        super().setUp()
        self.turn_on("1")
        self.hide(self.morning)

    def test_the_navigator_cannot_read_the_messages(self):
        pane = self.pane()
        self.assertNotIn("I slept badly", pane)
        self.assertNotIn("very badly", pane)

    def test_the_navigator_still_knows_it_happened(self):
        pane = self.pane()
        self.assertIn("Hidden at the client", pane)
        # Three messages that day, and the count is not part of the secret.
        self.assertIn("3", pane)

    def test_the_summary_and_the_topic_go_with_the_content(self):
        pane = self.pane()
        self.assertNotIn("Ada slept badly.", pane)
        self.assertNotIn("Sleep", pane)

    def test_the_other_conversation_that_day_is_untouched(self):
        pane = self.pane()
        self.assertIn("she ate well", pane)
        self.assertIn("good news", pane)

    def test_the_history_page_agrees_with_the_pane(self):
        page = self.history()
        self.assertNotIn("I slept badly", page)
        self.assertNotIn("Ada slept badly.", page)
        self.assertIn("hidden at the client", page.lower())
        self.assertIn("she ate well", page)

    def test_nothing_is_withheld_when_nothing_is_hidden(self):
        self.morning.hidden = False
        self.morning.save(update_fields=["hidden"])
        self.assertIn("I slept badly", self.pane())


class WhoItIsNotHiddenFrom(PrivacyTestCase):
    def setUp(self):
        super().setUp()
        self.turn_on("1")
        self.hide(self.morning)

    def test_an_admin_reads_it(self):
        self.client.force_login(self.admin)
        pane = self.pane()
        self.assertIn("I slept badly", pane)
        self.assertNotIn("Hidden at the client", pane)

    def test_the_self_harm_floor_overrides_the_hide(self):
        """A disclosure is exactly what nobody may switch off."""
        self.morning.auto_flags = {SAFETY_LABEL: True}
        self.morning.save(update_fields=["auto_flags"])
        self.assertFalse(conversation_privacy.is_withheld(self.morning, self.nav))
        self.assertIn("I slept badly", self.pane())

    def test_a_human_no_on_the_safety_label_restores_the_hide(self):
        """A navigator who reviewed it and said it was not that has said so."""
        self.morning.auto_flags = {SAFETY_LABEL: True}
        self.morning.human_flags = {SAFETY_LABEL: False}
        self.morning.save(update_fields=["auto_flags", "human_flags"])
        self.assertTrue(conversation_privacy.is_withheld(self.morning, self.nav))

    def test_another_detector_firing_does_not_override_anything(self):
        self.morning.auto_flags = {"Missed medication": True}
        self.morning.save(update_fields=["auto_flags"])
        self.assertTrue(conversation_privacy.is_withheld(self.morning, self.nav))


class TheDownload(PrivacyTestCase):
    """The CSV is the content by another route, and the URL is a plain GET."""

    def setUp(self):
        super().setUp()
        self.turn_on("1")
        cfg = SiteConfiguration.load()
        cfg.conversation_download_enabled = "1"
        cfg.save()
        cache.clear()

    def url(self, conversation):
        return reverse("download_conversation",
                       args=[self.patient.pk, str(conversation.id)])

    def test_a_visible_conversation_downloads(self):
        self.assertEqual(self.client.get(self.url(self.evening)).status_code, 200)

    def test_a_hidden_one_is_refused_even_with_the_url_in_hand(self):
        self.hide(self.morning)
        self.assertEqual(self.client.get(self.url(self.morning)).status_code, 404)

    def test_the_button_is_not_offered_either(self):
        self.hide(self.morning)
        pane = self.pane()
        self.assertNotIn(self.url(self.morning), pane)
        self.assertIn(self.url(self.evening), pane)

    def test_an_admin_may_still_take_it(self):
        self.hide(self.morning)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url(self.morning)).status_code, 200)


class TheApi(PrivacyTestCase):
    def setUp(self):
        super().setUp()
        self.owner = ConvAIUser.objects.create_user(username="ada", password="x")
        self.token = Token.objects.create(user=self.owner)
        self.morning.user = self.owner
        self.morning.save(update_fields=["user"])

    def auth(self, token=None):
        """The scheme the project's API actually runs on.

        ConvAI.api.views prefers a custom Bearer class and falls back to DRF's
        TokenAuthentication when it is absent, which is the case here — so the
        header says Token.
        """
        return f"Token {token.key if token else self.token.key}"

    def url(self, conversation=None):
        return reverse("api_conversation_visibility",
                       args=[str((conversation or self.morning).id)])

    def post(self, hidden, token=None):
        return self.client.post(
            self.url(), data=json.dumps({"hidden": hidden}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth(token))

    def get(self, token=None):
        return self.client.get(
            self.url(), HTTP_AUTHORIZATION=self.auth(token))

    def test_it_does_not_exist_while_the_switch_is_off(self):
        self.assertEqual(self.post(True).status_code, 404)

    def test_hiding_and_unhiding(self):
        self.turn_on("1")
        response = self.post(True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["hidden"])
        self.assertEqual(response.json()["message_count"], 2)
        self.morning.refresh_from_db()
        self.assertTrue(self.morning.hidden)
        self.assertIsNotNone(self.morning.hidden_at)

        response = self.post(False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["hidden"])
        self.morning.refresh_from_db()
        self.assertFalse(self.morning.hidden)

    def test_saying_it_twice_is_not_an_error(self):
        self.turn_on("1")
        self.assertEqual(self.post(True).status_code, 200)
        self.assertEqual(self.post(True).status_code, 200)

    def test_a_body_that_does_not_answer_is_refused(self):
        self.turn_on("1")
        response = self.client.post(
            self.url(), data=json.dumps({}), content_type="application/json",
            HTTP_AUTHORIZATION=self.auth())
        self.assertEqual(response.status_code, 400)

    def test_somebody_elses_conversation_is_not_there(self):
        self.turn_on("1")
        stranger = ConvAIUser.objects.create_user(username="eve", password="x")
        token = Token.objects.create(user=stranger)
        self.assertEqual(self.post(True, token=token).status_code, 404)

    def test_a_navigator_cannot_answer_for_their_client(self):
        """It is the client's own answer or it is worth nothing."""
        self.turn_on("1")
        token = Token.objects.create(user=self.nav)
        self.assertEqual(self.post(True, token=token).status_code, 404)

    def test_reading_the_current_state(self):
        self.turn_on("1")
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["hidden"])

    def test_a_conversation_that_is_not_a_uuid(self):
        self.turn_on("1")
        response = self.client.get(
            reverse("api_conversation_visibility", args=["not-a-uuid"]),
            HTTP_AUTHORIZATION=self.auth())
        self.assertEqual(response.status_code, 404)


class TheHelpers(PrivacyTestCase):
    def test_withheld_ids_ignores_keys_that_are_not_conversations(self):
        """Reminders and care-plan rows share the column and are not chats."""
        self.turn_on("1")
        self.hide(self.morning)
        found = conversation_privacy.withheld_ids(
            [str(self.morning.id), "reminder-12", "", None], self.nav)
        self.assertEqual(found, {str(self.morning.id)})

    def test_withheld_ids_is_empty_for_an_admin(self):
        self.turn_on("1")
        self.hide(self.morning)
        self.assertEqual(
            conversation_privacy.withheld_ids([str(self.morning.id)], self.admin), set())

    def test_set_hidden_keeps_the_stamp_when_it_is_undone(self):
        conversation_privacy.set_hidden(self.morning, True)
        stamped = self.morning.hidden_at
        self.assertIsNotNone(stamped)
        conversation_privacy.set_hidden(self.morning, False)
        self.assertEqual(self.morning.hidden_at, stamped)
