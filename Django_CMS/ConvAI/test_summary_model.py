"""The Summary model, and a review the provider's content filter refuses.

*One model writes every automatic summary* — conversation reviews, meeting and
call-transcript summaries — when one is set in Settings → Agents, whatever
model the agents chat with. Blank keeps the old order: the agent's model, then
the default.

*A blocked review is a review not done yet.* Left unanalysed for the next
message or the Settings batch to retry, with no alert, and logged as a
content-filter block rather than a generic failure.

    python3 manage.py test ConvAI.test_summary_model --settings=test_settings
"""
import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase

from ConvAI import conversation_alerts, summarization
from ConvAI.forms import AgentConfigForm
from ConvAI.models import Agent, Alert, Conversation, Message, Patient, SiteConfiguration
from ConvAI.utils_conversation_classification import classify_conversation_with_llm

VERDICT = '{"abstract": "They talked about school.", "classification": "Other", "important": false}'


def _config(**kwargs):
    cfg = SiteConfiguration.load()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    cfg.save()
    cache.clear()


def _llm(reply=VERDICT):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=reply)
    return llm


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _config(default_agent_model="openai/gpt-4.1-mini", summary_model="")
        self.rows = [{"timestamp": None, "from": "user", "text": "i went to school"}]

    def model_used(self, agent=None):
        with patch("ConvAI.llm_factory.make_llm", return_value=_llm()) as make:
            classify_conversation_with_llm(self.rows, agent=agent)
        return make.call_args.args[0]


class WhichModel(Base):
    def test_the_summary_model_comes_first(self):
        _config(summary_model="openai/gpt-5.4-mini")
        agent = Agent(name="A", kind="prompt", model="openai/gpt-4.1")
        self.assertEqual(self.model_used(agent), "openai/gpt-5.4-mini")

    def test_blank_keeps_the_agents_model_then_the_default(self):
        self.assertEqual(self.model_used(Agent(name="A", kind="prompt", model="openai/gpt-4.1")),
                         "openai/gpt-4.1")
        self.assertEqual(self.model_used(Agent(name="R", kind="remote", model="")),
                         "openai/gpt-4.1-mini")

    def test_meeting_and_call_summaries_use_it_too(self):
        _config(summary_model="openai/gpt-5.4-mini")
        with patch("ConvAI.llm_factory.make_llm", return_value=_llm("A summary.")) as make:
            summarization._run_llm("Summarise.", "the call")
        self.assertEqual(make.call_args.args[0], "openai/gpt-5.4-mini")

    def test_and_without_it_they_use_the_default(self):
        with patch("ConvAI.llm_factory.make_llm", return_value=_llm("A summary.")) as make:
            summarization._run_llm("Summarise.", "the call")
        self.assertIsNone(make.call_args.args[0])

    def test_settings_save_it(self):
        cfg = SiteConfiguration.load()
        data = {f: getattr(cfg, f) or "" for f in AgentConfigForm.Meta.fields}
        data["summary_model"] = "openai/gpt-5.4-mini"
        form = AgentConfigForm(data, instance=cfg)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(SiteConfiguration.load().summary_model, "openai/gpt-5.4-mini")


class InputBlocked(Exception):
    """What openai raises when Azure refuses the prompt."""

    def __init__(self):
        super().__init__("Error code: 400 - The response was filtered due to the prompt "
                         "triggering Azure OpenAI's content management policy. code: content_filter")
        self.body = {"innererror": {"code": "ResponsibleAIPolicyViolation", "content_filter_result": {
            "hate": {"filtered": False, "severity": "safe"},
            "self_harm": {"filtered": True, "severity": "medium"},
            "jailbreak": {"detected": False, "filtered": False}}}}


class BlockedReview(Base):
    def setUp(self):
        super().setUp()
        self.patient = Patient.objects.create(name="Ada", lastname="Lovelace")
        self.conv = Conversation.objects.create(patient=self.patient)
        Message.objects.create(conversation_id=str(self.conv.id), user="x", patient=self.patient,
                               user_message="honestly i want to end it all", response_message="I'm here.")

    def test_the_blocked_categories_are_read(self):
        self.assertEqual(conversation_alerts.content_filter_block(InputBlocked()), "self_harm")
        output_blocked = ValueError("Azure has not provided the response due to a content filter being triggered")
        self.assertEqual(conversation_alerts.content_filter_block(output_blocked), "")
        self.assertIsNone(conversation_alerts.content_filter_block(RuntimeError("timeout")))

    def test_it_stays_unreviewed_with_no_alert_and_says_why(self):
        with patch("ConvAI.conversation_alerts.classify_conversation_with_llm", side_effect=InputBlocked()), \
                self.assertLogs("ConvAI.conversation_alerts", level="WARNING") as logs:
            result = conversation_alerts.review_conversation(self.conv.id)
        self.assertEqual(result, {"analyzed": False, "alerts": []})
        self.assertFalse(Conversation.objects.get(pk=self.conv.pk).analyzed)
        self.assertEqual(Alert.objects.count(), 0)
        self.assertIn("content filter blocked it (self_harm)", logs.output[0])

    def test_other_failures_are_still_logged_as_errors(self):
        with patch("ConvAI.conversation_alerts.classify_conversation_with_llm",
                   side_effect=RuntimeError("timeout")), \
                self.assertLogs("ConvAI.conversation_alerts", level="ERROR"):
            conversation_alerts.review_conversation(self.conv.id)
        self.assertFalse(Conversation.objects.get(pk=self.conv.pk).analyzed)
