"""The SDK's LangGraph tools, end to end over real HTTP.

What a remote agent (RECO B, soon on WhatsApp) actually runs: ``build_cc_tools``
from the client SDK, reading ``cc_run_token`` out of its run config and calling
``/api/v1/run/`` on a live server — the same path it takes in production, minus
the model.

Also keeps the SDK's copy of the tool wording identical to the platform's, so an
agent on another server promises a client exactly what this platform does.

    python3 manage.py test ConvAI.test_sdk_run_tools --settings=test_settings
"""
import os
import sys
from unittest.mock import patch

from django.core.cache import cache
from django.test import LiveServerTestCase, SimpleTestCase

from ConvAI.models import Agent, Conversation, Patient, SiteConfiguration

SDK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client_sdk")
if SDK_DIR not in sys.path:
    sys.path.insert(0, SDK_DIR)

from conversationalcare_api import langgraph_tools  # noqa: E402


class TheWordingMatchesThePlatform(SimpleTestCase):
    def test_privacy(self):
        from ConvAI.native_agents.privacy_tool import PRIVACY_PROMPT_SUFFIX
        self.assertEqual(langgraph_tools.PRIVACY_PROMPT, PRIVACY_PROMPT_SUFFIX)
        from ConvAI.native_agents.privacy_tool import PRIVACY_PROMPT_STARTS_HIDDEN
        self.assertEqual(langgraph_tools.PRIVACY_PROMPT_STARTS_HIDDEN, PRIVACY_PROMPT_STARTS_HIDDEN)

    def test_summary(self):
        from ConvAI.native_agents.summary_tool import SUMMARY_PROMPT_SUFFIX
        self.assertEqual(langgraph_tools.SUMMARY_PROMPT, SUMMARY_PROMPT_SUFFIX)


class TheToolsOverHttp(LiveServerTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        cfg = SiteConfiguration.load()
        cfg.conversation_privacy_enabled = "1"
        cfg.save()
        cache.clear()
        self.agent = Agent.objects.create(name="RECO B", kind="remote", langgraph_name="reco_b",
                                          host="localhost", port=8123, allow_callbacks=True)
        self.patient = Patient.objects.create(name="Ada", lastname="Lovelace")
        self.conv = Conversation.objects.create(patient=self.patient, agent=self.agent)
        self.tools = {t.name: t for t in langgraph_tools.build_cc_tools()}

    def config(self, **extra):
        """The run config the platform sends a remote agent, as _run_identity builds it."""
        from ConvAI.utils import _run_identity
        with patch("ConvAI.utils.get_setting", return_value=self.live_server_url):
            configurable = _run_identity(self.agent, self.patient, str(self.conv.id))
        configurable.update(extra)
        return {"configurable": configurable}

    def test_the_platform_sends_a_token_and_where_to_use_it(self):
        cfg = self.config()["configurable"]
        self.assertIn("cc_run_token", cfg)
        self.assertEqual(cfg["cc_api_url"], self.live_server_url)

    def test_report_summary(self):
        out = self.tools["report_summary"].invoke(
            {"summary": "Wanted help sleeping. Suggested a wind-down hour."},
            config=self.config())
        self.assertIn("SUMMARY_RECORDED", out)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_summary,
                         "Wanted help sleeping. Suggested a wind-down hour.")

    def test_set_and_read_privacy(self):
        out = self.tools["set_conversation_privacy"].invoke({"hidden": True},
                                                            config=self.config())
        self.assertIn("PRIVACY_HIDDEN", out)
        self.conv.refresh_from_db()
        self.assertTrue(self.conv.hidden)
        self.assertIn("PRIVACY_HIDDEN",
                      self.tools["get_conversation_privacy"].invoke({}, config=self.config()))

    def test_the_model_cannot_name_a_conversation(self):
        for name in ("report_summary", "set_conversation_privacy", "get_conversation_privacy"):
            props = set(self.tools[name].args_schema.model_json_schema().get("properties", {}))
            self.assertFalse(props & {"conversation_id", "patient_id", "client_id", "thread_id"},
                             name)

    def test_without_a_token_they_say_so_and_do_not_raise(self):
        cfg = {"configurable": {"thread_id": str(self.conv.id)}}
        self.assertIn("not connected",
                      self.tools["report_summary"].invoke({"summary": "x"}, config=cfg))
        self.assertIn("not connected",
                      self.tools["set_conversation_privacy"].invoke({"hidden": True}, config=cfg))

    def test_a_refused_summary_is_reported_to_the_model(self):
        out = self.tools["report_summary"].invoke({"summary": "x" * 2500}, config=self.config())
        self.assertIn("SUMMARY_REJECTED", out)
