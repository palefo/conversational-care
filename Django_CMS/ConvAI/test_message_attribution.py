"""Whose a message is — decided once, when it arrives.

*A number is an address, not an identity.* It is used to decide whose a message
is on receipt, and never again: the answer is stamped on the message.

*A client who changes number keeps their history.* A stranger given their old
number does not inherit it — neither the history nor the access to it.

*One resolver for every inbound path*, with a rule for shared numbers instead of
an unordered ``.first()``.

*Legacy rows* — written before attribution, not placed by the backfill — are
the only ones still matched by number.

    python3 manage.py test ConvAI.test_message_attribution --settings=test_settings
"""
import importlib
import json
import logging
from unittest.mock import patch

from django.apps import apps as real_apps
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ConvAI import message_attribution as attribution
from ConvAI.models import (
    Agent, Caregiver, Conversation, ConvAIUser, Message, Patient,
)

Role = Message.SenderRole

CLIENT_NO = "+447700900301"
CAREGIVER_NO = "+447700900302"
NEW_NO = "+447700900399"

backfill_mod = importlib.import_module("ConvAI.migrations.0090_backfill_message_owners")


class AttributionTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        navigators = Group.objects.get_or_create(name="Navigator")[0]
        self.nav = ConvAIUser.objects.create_user(username="nav", password="x")
        self.nav.groups.add(navigators)
        self.other_nav = ConvAIUser.objects.create_user(username="nav2", password="x")
        self.other_nav.groups.add(navigators)
        self.agent = Agent.objects.create(name="Helper", kind="prompt")
        self.caregiver = Caregiver.objects.create(name="Grace", lastname="Hopper",
                                                  phone_number=CAREGIVER_NO)
        self.ada = Patient.objects.create(name="Ada", lastname="Lovelace",
                                          phone_number=CLIENT_NO, caregiver=self.caregiver,
                                          navigator=self.nav, agent=self.agent)

    def receive(self, number, text="hello"):
        """An inbound WhatsApp/SMS text, through the real entry point."""
        from ConvAI.utils import process_received_message
        with patch("ConvAI.utils.generate_response_langgraph", return_value="ok"), \
             patch("ConvAI.utils._review_after_message"):
            process_received_message(f"whatsapp:{number}", text)
        return Message.objects.order_by("-pk").first()

    def on_file(self, patient):
        return Message.objects.filter(attribution.patient_messages_q(patient))


# ─────────────────────────────── on receipt ────────────────────────────────

class OnReceipt(AttributionTestCase):
    def test_the_clients_own_number_is_the_client(self):
        msg = self.receive(CLIENT_NO)
        self.assertEqual(msg.patient, self.ada)
        self.assertEqual(msg.sender_role, Role.CLIENT)

    def test_the_caregivers_number_is_the_caregiver(self):
        msg = self.receive(CAREGIVER_NO)
        self.assertEqual(msg.patient, self.ada)
        self.assertEqual(msg.sender_role, Role.CAREGIVER)

    def test_an_unknown_number_is_nobodys(self):
        self.assertIsNone(attribution.resolve_inbound(NEW_NO).patient)

    def test_the_whatsapp_prefix_is_ignored(self):
        self.assertEqual(attribution.resolve_inbound(f"whatsapp:{CLIENT_NO}").patient, self.ada)


class SharedNumbers(AttributionTestCase):
    """A shared number is normal; guessing differently each time is not."""

    def setUp(self):
        super().setUp()
        # Grace looks after two people.
        self.bob = Patient.objects.create(name="Bob", lastname="Hopper",
                                          caregiver=self.caregiver, navigator=self.other_nav)

    def test_a_clients_own_number_beats_a_caregiver_number(self):
        Patient.objects.create(name="Cy", lastname="Z", phone_number=CAREGIVER_NO)
        inbound = attribution.resolve_inbound(CAREGIVER_NO)
        self.assertEqual(inbound.patient.name, "Cy")
        self.assertEqual(inbound.role, Role.CLIENT)

    def test_with_no_history_the_oldest_client_record(self):
        self.assertEqual(attribution.resolve_inbound(CAREGIVER_NO).patient, self.ada)

    def test_the_client_this_number_last_wrote_about(self):
        Message.objects.create(conversation_id="x", user=CAREGIVER_NO, user_message="hi",
                               response_message="", patient=self.bob,
                               sender_role=Role.CAREGIVER)
        self.assertEqual(attribution.resolve_inbound(CAREGIVER_NO).patient, self.bob)

    def test_the_same_answer_every_time(self):
        answers = {attribution.resolve_inbound(CAREGIVER_NO).patient.pk for _ in range(5)}
        self.assertEqual(len(answers), 1)

    def test_it_is_logged(self):
        with self.assertLogs("ConvAI.message_attribution", level=logging.WARNING):
            attribution.resolve_inbound(CAREGIVER_NO)

    def test_it_is_listed_for_admins(self):
        shared = attribution.shared_numbers()
        self.assertIn(CAREGIVER_NO, shared)
        self.assertEqual({p.pk for _r, p in shared[CAREGIVER_NO]}, {self.ada.pk, self.bob.pk})
        self.assertNotIn(CLIENT_NO, shared)


# ───────────────────────────── numbers change ──────────────────────────────

class ANumberChanges(AttributionTestCase):
    def setUp(self):
        super().setUp()
        self.before = self.receive(CLIENT_NO, "before the change")
        self.ada.phone_number = NEW_NO
        self.ada.save(update_fields=["phone_number"])

    def test_the_history_stays_on_the_file(self):
        self.assertIn(self.before, self.on_file(self.ada))

    def test_the_new_number_resolves_to_the_same_client(self):
        self.assertEqual(self.receive(NEW_NO).patient, self.ada)

    def test_the_old_number_stops_resolving_at_once(self):
        self.assertIsNone(attribution.resolve_inbound(CLIENT_NO).patient)

    def test_a_message_from_the_old_number_is_not_on_the_file(self):
        Message.objects.create(conversation_id="x", user=CLIENT_NO, user_message="hi",
                               response_message="", sender_role=Role.PROSPECT)
        self.assertEqual(self.on_file(self.ada).filter(user=CLIENT_NO).count(), 1)


class ARecycledNumber(AttributionTestCase):
    """Ada's old number is given to a new client, Zed, under another navigator."""

    def setUp(self):
        super().setUp()
        self.before = self.receive(CLIENT_NO, "Ada, before")
        self.ada.phone_number = NEW_NO
        self.ada.save(update_fields=["phone_number"])
        self.zed = Patient.objects.create(name="Zed", lastname="New", phone_number=CLIENT_NO,
                                          navigator=self.other_nav)

    def test_zed_does_not_inherit_adas_history(self):
        self.assertNotIn(self.before, self.on_file(self.zed))
        self.assertIn(self.before, self.on_file(self.ada))

    def test_new_messages_on_that_number_are_zeds(self):
        self.assertEqual(self.receive(CLIENT_NO).patient, self.zed)

    def test_zeds_navigator_cannot_download_adas_voice_notes(self):
        """Access used to follow the number; it follows the owner now."""
        self.client.force_login(self.other_nav)
        response = self.client.get(reverse("serve_audio_file", args=[self.before.pk, "input"]))
        self.assertEqual(response.status_code, 403)

    def test_adas_navigator_still_can(self):
        self.client.force_login(self.nav)
        response = self.client.get(reverse("serve_audio_file", args=[self.before.pk, "input"]))
        self.assertNotEqual(response.status_code, 403)

    def test_zeds_navigator_cannot_rate_adas_messages(self):
        self.client.force_login(self.other_nav)
        response = self.client.post(reverse("message_feedback", args=[self.before.pk]),
                                    data=json.dumps({"action": "like"}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 404)


# ───────────────────────────── who wrote in ────────────────────────────────

class EveryWritePathStamps(AttributionTestCase):
    def test_the_tester_chat_is_the_clients_file_typed_by_the_tester(self):
        from ConvAI.utils import process_message_for_patient
        tester = ConvAIUser.objects.create_user(username="tester", password="x")
        with patch("ConvAI.utils.generate_response_langgraph", return_value="ok"), \
             patch("ConvAI.utils._review_after_message"):
            process_message_for_patient(self.ada, "hi", user_label="tester",
                                        sender_role=Role.TESTER, account=tester)
        msg = Message.objects.latest("pk")
        self.assertEqual((msg.patient, msg.account, msg.sender_role),
                         (self.ada, tester, Role.TESTER))

    def test_the_link_worker_bubble_is_the_navigators_own(self):
        from ConvAI.utils import save_message
        msg = save_message("nav", "who are my clients?", "…", str(Conversation().id),
                           account=self.nav, sender_role=Role.STAFF)
        self.assertEqual((msg.patient, msg.account, msg.sender_role),
                         (None, self.nav, Role.STAFF))

    def test_an_outbound_only_row_is_the_platform(self):
        from ConvAI.utils import save_message
        msg = save_message(CAREGIVER_NO, "", "Reminder", str(Conversation().id),
                           patient=self.ada)
        self.assertEqual(msg.sender_role, Role.PLATFORM)

    def test_create_message_demands_a_role(self):
        with self.assertRaises(TypeError):
            attribution.create_message(conversation_id="x", user="y")

    def test_the_panel_says_who_wrote_from_the_stamp_not_the_number(self):
        """The caregiver changes number; the line still says who wrote.

        Matched against today's numbers, the old message matched nobody and the
        "Written by" line disappeared.
        """
        from django.utils import timezone
        self.receive(CAREGIVER_NO)
        self.caregiver.phone_number = NEW_NO
        self.caregiver.save(update_fields=["phone_number"])
        self.client.force_login(self.nav)
        pane = self.client.get(reverse("panel_fragment"),
                               {"item": f"chat-{self.ada.pk}-{timezone.localdate()}"}
                               ).content.decode()
        self.assertIn("Written by", pane)
        self.assertIn("Grace Hopper", pane)


# ─────────────────────────── legacy and backfill ───────────────────────────

class LegacyRows(AttributionTestCase):
    def legacy(self, **kw):
        return Message.objects.create(conversation_id=kw.pop("cid", "legacy"),
                                      user=kw.pop("user", CLIENT_NO),
                                      user_message="old", response_message="", **kw)

    def test_an_unplaced_legacy_row_is_still_matched_by_number(self):
        row = self.legacy()
        self.assertIn(row, self.on_file(self.ada))

    def test_a_stamped_row_is_never_matched_by_number(self):
        row = self.legacy(sender_role=Role.PROSPECT)
        self.assertNotIn(row, self.on_file(self.ada))

    def test_a_row_owned_by_someone_else_is_not_matched_by_number(self):
        other = Patient.objects.create(name="O", lastname="T")
        row = self.legacy(patient=other, sender_role=Role.CLIENT)
        self.assertNotIn(row, self.on_file(self.ada))


class TheBackfill(AttributionTestCase):
    def run_backfill(self):
        backfill_mod.backfill(real_apps, None)

    def row(self, **kw):
        return Message.objects.create(user_message=kw.pop("text", "hi"),
                                      response_message="", **kw)

    def test_places_by_conversation_first(self):
        conv = Conversation.objects.create(patient=self.ada)
        # A number that today belongs to nobody: the conversation still knows.
        m = self.row(conversation_id=str(conv.id), user="+440000000000")
        self.run_backfill()
        m.refresh_from_db()
        self.assertEqual(m.patient, self.ada)

    def test_places_reminders_by_their_key(self):
        from django.utils import timezone
        from ConvAI.models import Meeting
        meeting = Meeting.objects.create(patient=self.ada, scheduled_time=timezone.now())
        m = self.row(conversation_id=f"reminder-{meeting.pk}", user="x@example.com", text="")
        self.run_backfill()
        m.refresh_from_db()
        self.assertEqual((m.patient, m.sender_role), (self.ada, Role.PLATFORM))

    def test_places_the_link_worker_bubble_by_login(self):
        m = self.row(conversation_id=str(Conversation.objects.create().id), user="nav")
        self.run_backfill()
        m.refresh_from_db()
        self.assertEqual((m.account, m.sender_role), (self.nav, Role.STAFF))

    def test_places_by_number_only_when_unambiguous(self):
        m = self.row(conversation_id="legacy", user=CAREGIVER_NO)
        self.run_backfill()
        m.refresh_from_db()
        self.assertEqual((m.patient, m.sender_role), (self.ada, Role.CAREGIVER))

    def test_leaves_a_shared_number_for_the_fallback(self):
        Patient.objects.create(name="Bob", lastname="H", caregiver=self.caregiver)
        m = self.row(conversation_id="legacy", user=CAREGIVER_NO)
        self.run_backfill()
        m.refresh_from_db()
        self.assertIsNone(m.patient)
        self.assertEqual(m.sender_role, "")

    def test_does_not_touch_rows_already_stamped(self):
        other = Patient.objects.create(name="O", lastname="T")
        m = self.row(conversation_id="legacy", user=CLIENT_NO, patient=other,
                     sender_role=Role.CLIENT)
        self.run_backfill()
        m.refresh_from_db()
        self.assertEqual(m.patient, other)
