"""Conversations that start hidden, and remote replies made of several messages.

*Hidden from the first message* for an agent that can unhide it — a prompt
agent with the Conversation privacy tool, or a remote agent with "Conversations
start hidden" and callbacks — so a link worker cannot read it while it is still
happening. Only while privacy is switched on, and only when the conversation is
created: after that, the client's answer decides.

*A remote turn is everything the agent said in it*, joined, not its last message.

    python3 manage.py test ConvAI.test_starts_hidden --settings=test_settings
"""
import uuid
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase

from ConvAI import conversation_privacy, run_tokens
from ConvAI.forms import AgentForm
from ConvAI.models import Agent, ConvAIUser, Conversation, Patient, SiteConfiguration
from ConvAI.utils import _ensure_conversation, remote_turn_reply, save_message


def _privacy(on=True):
    cfg = SiteConfiguration.load()
    cfg.conversation_privacy_enabled = "1" if on else "0"
    cfg.save()
    cache.clear()


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _privacy(True)
        self.nav = ConvAIUser.objects.create_user("nav", password="x")
        self.nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.ada = Patient.objects.create(name="Ada", lastname="Lovelace", navigator=self.nav,
                                          phone_number="+447700900001")
        self.reco = Agent.objects.create(name="RECO v2", kind="remote", langgraph_name="reco_v2",
                                         host="localhost", port=8123, allow_callbacks=True,
                                         starts_hidden=True)
        self.private_prompt = Agent.objects.create(name="Private", kind="prompt",
                                                   system_prompt="Be kind.",
                                                   tools={"conversation_privacy": {}})
        self.plain_prompt = Agent.objects.create(name="Plain", kind="prompt", system_prompt="Be kind.")
        self.impact = Agent.objects.create(name="IMPACT", kind="remote", langgraph_name="impact_v2",
                                           host="localhost", port=8123)


class WhoStartsHidden(Base):
    def test_the_rule(self):
        self.assertTrue(conversation_privacy.starts_hidden(self.reco))
        self.assertTrue(conversation_privacy.starts_hidden(self.private_prompt))
        self.assertFalse(conversation_privacy.starts_hidden(self.plain_prompt))
        self.assertFalse(conversation_privacy.starts_hidden(self.impact))
        self.assertFalse(conversation_privacy.starts_hidden(None))

    def test_a_remote_agent_without_callbacks_never_does(self):
        self.reco.allow_callbacks = False
        self.reco.save()
        self.assertFalse(conversation_privacy.starts_hidden(self.reco))

    def test_a_ticked_but_switched_off_tool_does_not(self):
        self.private_prompt.tools = {"conversation_privacy": {"enabled": False, "prompt": "x"}}
        self.private_prompt.save()
        self.assertFalse(conversation_privacy.starts_hidden(self.private_prompt))

    def test_nothing_while_privacy_is_switched_off(self):
        _privacy(False)
        self.assertFalse(conversation_privacy.starts_hidden(self.reco))
        self.assertFalse(conversation_privacy.starts_hidden(self.private_prompt))


class HiddenFromTheFirstMessage(Base):
    def test_a_remote_conversation_made_before_the_run_is_hidden(self):
        conv = _ensure_conversation(str(uuid.uuid4()), patient=self.ada, agent=self.reco)
        self.assertTrue(conv.hidden)
        self.assertIsNotNone(conv.hidden_at)

    def test_a_prompt_agents_first_saved_turn_is_hidden(self):
        self.ada.agent = self.private_prompt
        self.ada.save()
        tid = str(uuid.uuid4())
        save_message("+447700900001", "hi", "hello", tid, patient=self.ada)
        self.assertTrue(Conversation.objects.get(pk=tid).hidden)

    def test_a_later_turn_does_not_hide_it_again(self):
        self.ada.agent = self.private_prompt
        self.ada.save()
        tid = str(uuid.uuid4())
        save_message("+447700900001", "hi", "hello", tid, patient=self.ada)
        conversation_privacy.set_hidden(Conversation.objects.get(pk=tid), False)
        save_message("+447700900001", "and another", "ok", tid, patient=self.ada)
        self.assertFalse(Conversation.objects.get(pk=tid).hidden)

    def test_other_agents_start_visible(self):
        for agent in (self.plain_prompt, self.impact):
            conv = Conversation.objects.create(patient=self.ada, agent=agent)
            self.assertFalse(conv.hidden)

    def test_the_link_worker_cannot_read_it_while_it_happens(self):
        conv = Conversation.objects.create(patient=self.ada, agent=self.reco)
        self.assertTrue(conversation_privacy.is_withheld(conv, self.nav))

    def test_once_unhidden_it_stays_unhidden(self):
        conv = Conversation.objects.create(patient=self.ada, agent=self.reco)
        conversation_privacy.set_hidden(conv, False)
        conv.last_message_at = conv.started_at
        conv.save()
        self.assertFalse(Conversation.objects.get(pk=conv.pk).hidden)
        self.assertFalse(conversation_privacy.is_withheld(conv, self.nav))

    def test_the_agent_unhides_it_with_its_run_token(self):
        conv = Conversation.objects.create(patient=self.ada, agent=self.reco)
        token = run_tokens.mint(conv.id, agent_id=self.reco.pk, patient_id=self.ada.pk)
        resp = self.client.post("/api/v1/run/visibility/", data={"hidden": False},
                                content_type="application/json",
                                HTTP_AUTHORIZATION=f"RunToken {token}")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Conversation.objects.get(pk=conv.pk).hidden)

    def test_not_while_privacy_is_off(self):
        _privacy(False)
        self.assertFalse(Conversation.objects.create(patient=self.ada, agent=self.reco).hidden)


class Wording(Base):
    def test_a_private_prompt_agent_is_told_it_starts_hidden(self):
        from ConvAI.native_agents import tool_registry
        suffix = tool_registry.prompt_suffix(self.private_prompt)
        self.assertIn("starts **hidden**", suffix)
        self.assertNotIn("starts visible again", suffix)

    def test_the_form_refuses_hidden_without_callbacks(self):
        data = {"name": "X", "langgraph_name": "x", "host": "localhost", "port": 8123,
                "starts_hidden": "on", "detectors": "{}"}
        form = AgentForm(data)
        self.assertFalse(form.is_valid())
        self.assertIn("starts_hidden", form.errors)
        form = AgentForm({**data, "allow_callbacks": "on"})
        self.assertNotIn("starts_hidden", form.errors)


class RemoteTurns(TestCase):
    H = {"type": "human", "content": "yes please"}

    def test_every_text_after_the_message_is_joined(self):
        msgs = [{"type": "human", "content": "earlier"}, {"type": "ai", "content": "old"}, self.H,
                {"type": "ai", "content": "Football trial\n\nWhat you said: ..."},
                {"type": "ai", "content": "One thing for Saturday:"},
                {"type": "ai", "content": "Does that look right?"}]
        self.assertEqual(remote_turn_reply(msgs),
                         "Football trial\n\nWhat you said: ...\n\nOne thing for Saturday:\n\n"
                         "Does that look right?")

    def test_a_supervisor_gives_only_its_final_answer(self):
        msgs = [self.H,
                {"type": "ai", "content": "", "tool_calls": [{"name": "transfer", "id": "1"}]},
                {"type": "tool", "content": "transferred"},
                {"type": "ai", "content": "Sub-agent draft"},
                {"type": "ai", "content": "Transferring back", "tool_calls": [{"name": "back", "id": "2"}]},
                {"type": "tool", "content": "ok"},
                {"type": "ai", "content": "Final answer."}]
        self.assertEqual(remote_turn_reply(msgs), "Final answer.")

    def test_text_spoken_while_calling_a_tool_is_kept_if_nothing_follows(self):
        msgs = [self.H,
                {"type": "ai", "content": "Thanks, take care.", "tool_calls": [{"name": "report_summary", "id": "1"}]},
                {"type": "tool", "content": "SUMMARY_RECORDED"},
                {"type": "ai", "content": ""}]
        self.assertEqual(remote_turn_reply(msgs), "Thanks, take care.")

    def test_content_blocks_and_nothing_at_all(self):
        self.assertEqual(remote_turn_reply([self.H, {"type": "ai", "content": [{"type": "text", "text": "Hi"}]}]), "Hi")
        self.assertEqual(remote_turn_reply([self.H, {"type": "ai", "content": ""}]), "")

    def test_the_remote_path_sends_the_joined_reply(self):
        from ConvAI.utils import generate_response_with_agent
        agent = Agent.objects.create(name="R", kind="remote", langgraph_name="reco_v2",
                                     host="localhost", port=8123)
        user = ConvAIUser.objects.create_user("u", password="x")
        rg = MagicMock()
        rg.invoke.return_value = {"messages": [self.H, {"type": "ai", "content": "One"},
                                               {"type": "ai", "content": "Two"}]}
        with patch("langgraph.pregel.remote.RemoteGraph", return_value=rg), \
                patch("ConvAI.utils.agent_host_allowed", return_value=True):
            reply = generate_response_with_agent(agent, user, "yes please", str(uuid.uuid4()))
        self.assertEqual(reply, "One\n\nTwo")
