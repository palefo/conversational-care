"""Tests for the path from an inbound message to an alert somebody sees.

Narrow on purpose. What is covered here is the logic that decides whether a
caregiver saying something serious reaches a human, and the failure mode is
silence — nothing errors, no page turns red, an alert simply never appears.
That is not a thing anyone notices in review, so it is a thing that gets a
test.

Run with ``python manage.py test ConvAI``.
"""
import uuid
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.utils import translation

from .models import Agent, Alert, Caregiver, Conversation, ConvAIUser, Message, Patient
from .utils_conversation_classification import (
    SAFETY_LABEL, detectors_for, normalize_detectors,
)


class DetectorShapeTests(TestCase):
    """``Agent.detectors`` has been stored two ways; both have to read."""

    def test_legacy_string_rows_do_not_raise(self):
        # These predate alerts entirely. Promoting them would start paging
        # people about things nobody asked to be paged about.
        d = normalize_detectors({"Missed medication": "Skipped doses"})["Missed medication"]
        self.assertEqual(d.instruction, "Skipped doses")
        self.assertFalse(d.raises)

    def test_current_rows_keep_their_settings(self):
        d = normalize_detectors(
            {"Burden of care": {"instruction": "Exhaustion", "raises": True, "priority": 1}}
        )["Burden of care"]
        self.assertTrue(d.raises)
        self.assertEqual(d.priority, 1)

    def test_junk_is_survived_not_trusted(self):
        dets = normalize_detectors({"X": {"instruction": "i", "priority": 99}, "   ": "y"})
        self.assertEqual(dets["X"].priority, 2)   # clamped to Medium
        self.assertNotIn("   ", dets)             # blank label dropped


class SafetyFloorTests(TestCase):
    """The one detector an admin cannot switch off."""

    def test_applies_with_no_agent_at_all(self):
        self.assertIn(SAFETY_LABEL, detectors_for(None))

    def test_applies_to_an_agent_that_configured_nothing(self):
        agent = Agent.objects.create(name="Bare", detectors={})
        self.assertTrue(detectors_for(agent)[SAFETY_LABEL].raises)

    def test_cannot_be_downgraded_or_switched_off(self):
        # An admin may reword it — their wording is often better than ours.
        # What they cannot do is stop it raising, or drop it below High.
        agent = Agent.objects.create(name="Sneaky", detectors={
            SAFETY_LABEL: {"instruction": "my own wording", "raises": False, "priority": 3},
        })
        d = detectors_for(agent)[SAFETY_LABEL]
        self.assertEqual(d.instruction, "my own wording")
        self.assertTrue(d.raises)
        self.assertEqual(d.priority, 1)


VERDICT = {
    "abstract": "Headaches and low mood, then a direct statement of intent.",
    "classification": "Emergency",
    "important": True,
    "detectors": {SAFETY_LABEL: True, "Missed medication": False,
                  "Asks about services": True},
    "triggers": {SAFETY_LABEL: 'The caregiver wrote "I am feeling suicidal".'},
}


class LabelDisplayTests(TestCase):
    """The label is stored in one language and read in another."""

    def test_the_stored_key_never_moves(self):
        # Dedupe, the agent's config and the audit trail all match on this
        # string. A key that changed with the viewer's language would stop
        # matching itself, and the same crisis would raise an alert per locale.
        from .utils_conversation_classification import SAFETY_LABEL
        self.assertEqual(SAFETY_LABEL, "Self-harm")

    def test_it_reads_in_the_viewer_s_language(self):
        from .utils_conversation_classification import display_label
        with translation.override("es-pe"):
            self.assertEqual(display_label(SAFETY_LABEL), "Autolesión")
        with translation.override("it"):
            self.assertEqual(display_label(SAFETY_LABEL), "Autolesionismo")

    def test_an_admin_s_own_label_is_left_alone(self):
        from .utils_conversation_classification import display_label
        with translation.override("es-pe"):
            self.assertEqual(display_label("Olvida la medicación"),
                             "Olvida la medicación")


class ReviewConversationTests(TestCase):
    """Classifying a conversation, and what it does and does not raise."""

    def setUp(self):
        self.nav = ConvAIUser.objects.create(username="nav")
        self.agent = Agent.objects.create(name="Care", detectors={
            "Missed medication": {"instruction": "Skipped doses", "raises": True, "priority": 2},
            "Asks about services": {"instruction": "Day centres", "raises": False, "priority": 3},
        })
        cg = Caregiver.objects.create(name="Ada", lastname="R", phone_number="+441234567890")
        self.patient = Patient.objects.create(
            name="Maria", lastname="Rossi", caregiver=cg, navigator=self.nav,
            agent=self.agent, phone_number="+441234567891")
        self.tid = uuid.uuid4()
        Conversation.objects.create(id=self.tid, patient=self.patient, agent=self.agent)
        Message.objects.create(conversation_id=str(self.tid), user="+441234567891",
                               user_message="I'm feeling suicidal", response_message="...")

    def _review(self, verdict=VERDICT):
        from . import conversation_alerts as ca
        with patch.object(ca, "classify_conversation_with_llm", return_value=verdict):
            return ca.review_conversation(self.tid)

    def test_a_firing_detector_raises_one_alert(self):
        alerts = self._review()["alerts"]
        self.assertEqual(len(alerts), 1)
        a = alerts[0]
        self.assertEqual(a.title, SAFETY_LABEL)
        self.assertEqual(a.priority, Alert.Priority.HIGH)
        self.assertEqual(a.alert_type, Alert.AlertType.CONVERSATION)
        self.assertEqual(a.patient_id, self.patient.pk)
        self.assertEqual(a.user_id, self.nav.pk)
        # Nobody created it, and the audit trail should not imply otherwise.
        self.assertIsNone(a.created_by_id)

    def test_the_alert_names_the_conversation_it_came_from(self):
        # The panel resolves the exchange from this key. Without it the tab
        # falls back to guessing by date, which is the behaviour this whole
        # change exists to stop.
        self.assertEqual(self._review()["alerts"][0].data["conversation_id"], str(self.tid))

    def test_the_trigger_is_stored_apart_from_the_summary(self):
        a = self._review()["alerts"][0]
        self.assertIn("suicidal", a.data["trigger"])
        self.assertEqual(a.description, VERDICT["abstract"])

    def test_a_detector_that_does_not_raise_only_flags(self):
        self._review()
        conv = Conversation.objects.get(id=self.tid)
        self.assertTrue(conv.auto_flags["Asks about services"])
        self.assertFalse(
            Alert.objects.filter(data__detector="Asks about services").exists())

    def test_important_alone_does_not_raise(self):
        # It is the model's opinion that a conversation is worth reading. That
        # is a review flag, not grounds to interrupt somebody.
        quiet = {**VERDICT, "detectors": {}, "triggers": {}}
        self.assertEqual(self._review(quiet)["alerts"], [])
        self.assertTrue(Conversation.objects.get(id=self.tid).is_important)

    def test_a_long_crisis_is_one_alert_not_fourteen(self):
        self._review()
        Message.objects.create(conversation_id=str(self.tid), user="+441234567891",
                               user_message="I don't want to go on", response_message="...")
        self.assertEqual(self._review()["alerts"], [])

    def test_a_resolved_alert_does_not_block_the_next_one(self):
        first = self._review()["alerts"][0]
        Alert.objects.filter(pk=first.pk).update(status=Alert.AlertStatus.RESOLVED)
        self.assertEqual(len(self._review()["alerts"]), 1)

    def test_a_classifier_outage_raises_nothing_and_crashes_nothing(self):
        from . import conversation_alerts as ca
        with patch.object(ca, "classify_conversation_with_llm",
                          side_effect=RuntimeError("api down")):
            self.assertEqual(ca.review_conversation(self.tid),
                             {"analyzed": False, "alerts": []})


class MessageHookTests(TestCase):
    """What the inbound path hands to the classifier, and what it swallows."""

    def setUp(self):
        self.tid = uuid.uuid4()
        self.msg = Message.objects.create(
            conversation_id=str(self.tid), user="+441234567891",
            user_message="I'm feeling suicidal", response_message="...")

    def test_an_agent_only_turn_is_not_reclassified(self):
        from . import utils
        with patch("ConvAI.conversation_alerts.review_conversation_async") as queued:
            utils._review_after_message(self.msg, "   ")
            self.assertFalse(queued.called)

    def test_the_review_follows_the_saved_message_not_the_thread_id(self):
        # save_message replaces a thread id it cannot parse as a UUID. Keying
        # the review off the id we asked for would then aim it at a
        # conversation that does not exist, and nothing would be reviewed.
        from . import utils
        with patch("ConvAI.conversation_alerts.review_conversation_async") as queued:
            utils._review_after_message(self.msg, "I'm feeling suicidal")
            queued.assert_called_once_with(str(self.tid))

    def test_a_broken_queue_does_not_break_delivery(self):
        # The message is already saved and the reply already sent by this
        # point. Detection failing must not turn into a message that didn't.
        from . import utils
        with patch("ConvAI.conversation_alerts.review_conversation_async",
                   side_effect=RuntimeError("pool down")):
            utils._review_after_message(self.msg, "hello")   # must not raise

    def test_an_unparseable_id_is_declined_not_raised(self):
        from .conversation_alerts import review_conversation
        self.assertEqual(review_conversation("not-a-uuid"),
                         {"analyzed": False, "alerts": []})


class AlertPanelTests(TestCase):
    """How the two kinds of alert are drawn, which is not the same way."""

    def setUp(self):
        self.nav = ConvAIUser.objects.create(username="nav", is_staff=True, is_superuser=True)
        cg = Caregiver.objects.create(name="Ada", lastname="R", phone_number="+441234567890")
        self.patient = Patient.objects.create(
            name="Maria", lastname="Rossi", caregiver=cg, navigator=self.nav,
            phone_number="+441234567891")
        self.req = RequestFactory().get("/")
        self.req.user = self.nav

    def _panel_for(self, alert):
        from .views import _panel
        return _panel._alert_panel(self.req, alert.pk)

    def _classifier_alert(self, **data):
        return Alert.objects.create(
            patient=self.patient, user=self.nav,
            alert_type=Alert.AlertType.CONVERSATION, priority=Alert.Priority.HIGH,
            title=SAFETY_LABEL, description="A summary of the exchange.",
            data={"detector": SAFETY_LABEL, "trigger": "They said it directly.",
                  "trigger_at": "2026-08-27T22:55:00+00:00", **data})

    def test_a_classifier_alert_shows_its_trigger_and_an_editable_summary(self):
        item = self._panel_for(self._classifier_alert())
        self.assertEqual(item["trigger"], "They said it directly.")
        self.assertIsNotNone(item["trigger_at"])
        self.assertIn("overview_edit_url", item)

    def test_the_panel_title_is_translated_but_the_row_is_not_rematched(self):
        alert = self._classifier_alert()
        with translation.override("es-pe"):
            self.assertEqual(self._panel_for(alert)["title"], "Autolesión")
        alert.refresh_from_db()
        self.assertEqual(alert.title, SAFETY_LABEL)
        self.assertEqual(alert.data["detector"], SAFETY_LABEL)

    def test_the_trigger_is_not_repeated_under_technical(self):
        item = self._panel_for(self._classifier_alert())
        self.assertNotIn("trigger", dict(item["alert_data"]))
        self.assertNotIn("trigger_at", dict(item["alert_data"]))

    def test_an_unparseable_timestamp_does_not_break_the_panel(self):
        item = self._panel_for(self._classifier_alert(trigger_at="not-a-date"))
        self.assertIsNone(item["trigger_at"])
        self.assertTrue(item["trigger"])

    def test_a_human_raised_alert_keeps_its_own_words(self):
        # Their description is the note they typed. Offering to edit "the
        # generated summary" of something a person wrote would be nonsense.
        alert = Alert.objects.create(
            patient=self.patient, user=self.nav, created_by=self.nav,
            alert_type=Alert.AlertType.DEFAULT, priority=Alert.Priority.HIGH,
            title="Very urgent message", data={"raised_by_human": True})
        item = self._panel_for(alert)
        self.assertEqual(item["trigger"], "")
        self.assertNotIn("overview_edit_url", item)

    def test_a_navigator_cannot_open_another_navigator_s_alert(self):
        stranger = ConvAIUser.objects.create(username="stranger")
        req = RequestFactory().get("/")
        req.user = stranger
        from .views import _panel
        self.assertIsNone(_panel._alert_panel(req, self._classifier_alert().pk))


class WhatsAppDeliveryTests(TestCase):
    """Whether a message was actually delivered, as opposed to accepted.

    Twilio's ``create()`` returns successfully for a message it is about to
    fail: the commonest failure here — 63016, freeform text outside WhatsApp's
    24-hour window — arrives seconds later on the message resource. The old
    boolean read the acceptance, so ``start_protocol_automation`` told the
    navigator the caregiver had been asked something they were never asked, and
    left the client switched to the protocol agent for three hours over it.

    Same failure mode as the rest of this file: nothing errors, nothing turns
    red, a question simply never arrives.
    """

    def _client(self, created_status, fetched=None):
        from unittest.mock import MagicMock
        from types import SimpleNamespace

        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(
            sid="SM_test", status=created_status, error_code=None,
        )
        if fetched is not None:
            client.messages.return_value.fetch.return_value = SimpleNamespace(
                status=fetched[0], error_code=fetched[1],
            )
        return client

    def _send(self, client, **kw):
        from . import utils

        with patch.object(utils, "Client", return_value=client), \
             patch.object(utils, "get_setting", side_effect=lambda k, *a: {
                 "TWILIO_ACCOUNT_SID": "AC_test",
                 "TWILIO_AUTH_TOKEN": "tok",
                 "PLATFORM_PHONE": "+441234567890",
             }.get(k, "")), \
             patch.object(utils.time, "sleep", lambda s: None):
            return utils.send_whatsapp_text_result("+447000000000", "Hi", **kw)

    def test_accepted_then_undelivered_is_not_a_send(self):
        # The exact shape of a 63016: create() succeeds, the failure lands on
        # the next fetch. Reported as sent, this is the bug.
        res = self._send(self._client("queued", ("undelivered", 63016)))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], 63016)
        # And it says why, because "could not be sent" is not something a
        # navigator can act on — "they have not replied in 24 hours" is.
        self.assertIn("24 hours", res["reason"])

    def test_delivered_is_a_send(self):
        res = self._send(self._client("queued", ("delivered", None)))
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "delivered")
        self.assertEqual(res["reason"], "")

    def test_still_in_flight_is_not_reported_as_failure(self):
        # Nothing has gone wrong yet, so nothing should be announced as having
        # gone wrong. A late failure is the status callback's problem.
        res = self._send(self._client("queued", ("queued", None)), wait_s=0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "queued")

    def test_unknown_error_code_still_names_itself(self):
        res = self._send(self._client("queued", ("failed", 12345)))
        self.assertFalse(res["ok"])
        self.assertIn("12345", res["reason"])
