"""When an idle conversation ends: a platform default and a per-agent override.

Settings → Agents sets how long a quiet spell ends every agent's conversations
(blank: CONVERSATION_IDLE_HOURS in .env, then three hours). An agent can set its
own, which wins for its clients; 0 is never, so only /quit or the agent itself
(POST /api/v1/run/end/) ends one. The Agents page and Settings say so wherever
an agent differs.

    python3 manage.py test ConvAI.test_conversation_rollover --settings=test_settings
"""
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI import conversation_rollover as rollover
from ConvAI.forms import AgentConfigForm, AgentForm, NativeAgentForm
from ConvAI.models import Agent, ConvAIUser, Conversation, Patient, SiteConfiguration
from ConvAI.utils import _get_or_create_thread


def _remote(**kw):
    return Agent.objects.create(name=kw.pop("name", "RECO v2"), kind="remote",
                                langgraph_name="reco_v2", host="localhost", port=8123, **kw)


def _platform(hours):
    cfg = SiteConfiguration.load()
    cfg.conversation_idle_hours = hours
    cfg.save()


class Base(TestCase):
    # SiteConfiguration.load() is cached; a test's rolled-back row must not
    # leak into the next one through it.
    def setUp(self):
        cache.clear()


class Resolving(Base):
    def test_three_hours_when_nothing_is_set(self):
        with patch.dict("os.environ", {rollover.ENV_KEY: ""}):
            self.assertEqual(rollover.platform_idle_hours(), 3)

    def test_env_then_settings(self):
        with patch.dict("os.environ", {rollover.ENV_KEY: "6"}):
            self.assertEqual(rollover.platform_idle_hours(), 6)
            _platform(12)
            self.assertEqual(rollover.platform_idle_hours(), 12)

    def test_zero_on_the_platform_is_never_not_unset(self):
        _platform(0)
        self.assertEqual(rollover.platform_idle_hours(), 0)

    def test_the_agent_wins_and_blank_follows(self):
        _platform(5)
        self.assertEqual(rollover.idle_hours(_remote(name="a")), 5)
        self.assertEqual(rollover.idle_hours(_remote(name="b", conversation_idle_hours=0)), 0)
        self.assertEqual(rollover.idle_hours(_remote(name="c", conversation_idle_hours=24)), 24)
        self.assertEqual(rollover.idle_hours(None), 5)

    def test_an_override_equal_to_the_platform_is_not_shown_as_one(self):
        _platform(3)
        self.assertFalse(rollover.overrides_platform(_remote(name="a", conversation_idle_hours=3)))
        self.assertTrue(rollover.overrides_platform(_remote(name="b", conversation_idle_hours=0)))
        self.assertEqual(_remote(name="c", conversation_idle_hours=0).idle_override, "Never")


class Rollover(Base):
    def _client_idle(self, hours_ago, agent):
        p = Patient.objects.create(name="Ada", phone_number="+447700900001", agent=agent)
        conv = Conversation.objects.create(id=uuid.uuid4(), patient=p, agent=agent,
                                           last_message_at=timezone.now() - timedelta(hours=hours_ago))
        p.current_thread_id = str(conv.id)
        p.save()
        return p, str(conv.id)

    def test_the_platform_default_rolls_over(self):
        p, tid = self._client_idle(4, _remote())
        self.assertNotEqual(_get_or_create_thread(p), tid)

    def test_inside_the_window_it_continues(self):
        p, tid = self._client_idle(2, _remote())
        self.assertEqual(_get_or_create_thread(p), tid)

    def test_never_keeps_the_conversation_however_long(self):
        p, tid = self._client_idle(24 * 30, _remote(conversation_idle_hours=0))
        self.assertEqual(_get_or_create_thread(p), tid)

    def test_the_agents_own_window(self):
        p, tid = self._client_idle(4, _remote(conversation_idle_hours=8))
        self.assertEqual(_get_or_create_thread(p), tid)
        p2, tid2 = self._client_idle(9, _remote(name="other", conversation_idle_hours=8))
        self.assertNotEqual(_get_or_create_thread(p2), tid2)

    def test_the_platform_set_to_never(self):
        _platform(0)
        p, tid = self._client_idle(100, _remote())
        self.assertEqual(_get_or_create_thread(p), tid)


class Forms(Base):
    BASE = {"name": "R", "langgraph_name": "reco_v2", "host": "localhost", "port": 8123,
            "detectors": "{}"}

    def _save(self, **data):
        form = AgentForm({**self.BASE, "name": f"R{Agent.objects.count()}", **data})
        self.assertTrue(form.is_valid(), form.errors)
        return form.save().conversation_idle_hours

    def test_default_never_and_hours(self):
        self.assertIsNone(self._save(conversation_idle_hours_0="default"))
        self.assertEqual(self._save(conversation_idle_hours_0="never"), 0)
        self.assertEqual(self._save(conversation_idle_hours_0="hours", conversation_idle_hours_1="12"), 12)

    def test_not_sending_it_is_the_default(self):
        self.assertIsNone(self._save())

    def test_hours_needs_a_number(self):
        form = AgentForm({**self.BASE, "conversation_idle_hours_0": "hours"})
        self.assertFalse(form.is_valid())
        self.assertIn("conversation_idle_hours", form.errors)

    def test_the_default_option_names_the_platform_value(self):
        _platform(6)
        choices = dict(AgentForm().fields["conversation_idle_hours"].fields[0].choices)
        self.assertIn("6 hours", str(choices["default"]))

    def test_an_overriding_agent_says_so_on_its_form(self):
        agent = _remote(conversation_idle_hours=0)
        self.assertIn("overrides the platform default", str(AgentForm(instance=agent).idle_override_note))
        self.assertEqual(AgentForm(instance=_remote(name="b")).idle_override_note, "")

    def test_native_agents_have_it_too(self):
        agent = Agent.objects.create(name="Loop", kind="native", native_key="loopback")
        form = NativeAgentForm({"conversation_idle_hours_0": "never"}, instance=agent)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().conversation_idle_hours, 0)

    def test_the_platform_setting(self):
        cfg = SiteConfiguration.load()
        form = AgentConfigForm({"conversation_idle_hours_0": "hours", "conversation_idle_hours_1": "5",
                                "use_azure": "", "link_worker_v2_enabled": "",
                                "link_worker_whatsapp_enabled": "", "tts_provider": ""},
                               instance=cfg)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(rollover.platform_idle_hours(), 5)


class Pages(Base):
    def setUp(self):
        super().setUp()
        self.admin = ConvAIUser.objects.create_superuser("root", "r@example.org", "x")
        self.client.force_login(self.admin)

    def test_the_card_shows_an_agent_that_differs(self):
        _remote(conversation_idle_hours=0)
        _remote(name="Plain")
        html = self.client.get(reverse("agents")).content.decode()
        self.assertEqual(html.count("Idle end:"), 1)
        self.assertIn("Idle end: Never", html)

    def test_settings_names_the_agents_that_differ(self):
        _remote(conversation_idle_hours=0)
        html = self.client.get(reverse("config")).content.decode()
        self.assertIn("End idle conversations", html)
        self.assertIn("These agents set their own", html)
        self.assertIn("RECO v2</a> (never)", html)
