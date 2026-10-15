"""Replies sent as separate messages, and agents ending a conversation.

*Send each message separately*: a remote agent that answers one turn with
several messages (RECO v2: the summary, then "does that look right?") has each
sent as its own SMS or WhatsApp message and its own bubble in the web chats,
while the history keeps the turn as one joined reply. Off, nothing changes.

*Ending*: POST /api/v1/run/end/ with the run token says the conversation is
over. The client's next message starts a new one, as after /quit; the web chats
are told so.

    python3 manage.py test ConvAI.test_turn_parts_and_end --settings=test_settings
"""
import uuid
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ConvAI import run_tokens, tasks
from ConvAI.forms import AgentForm
from ConvAI.models import Agent, ConvAIUser, Conversation, Message, Patient
from ConvAI.utils import (AgentReply, end_conversation, generate_response_with_agent,
                          process_message_for_patient, remote_turn_parts, remote_turn_reply,
                          reply_parts)

H = {"type": "human", "content": "yes please"}
SUMMARY = {"type": "ai", "content": "💬 What you said\nFootball on Saturday."}
NOTE_CALL = {"type": "ai", "content": "", "tool_calls": [{"name": "report_summary", "id": "1"}]}
TOOL = {"type": "tool", "content": "SUMMARY_RECORDED"}
CHECK = {"type": "ai", "content": "Does that look right?"}


def _remote(**kw):
    return Agent.objects.create(name=kw.pop("name", "RECO v2"), kind="remote",
                                langgraph_name="reco_v2", host="localhost", port=8123, **kw)


def _invoke(agent, user, messages, thread=None):
    rg = MagicMock()
    rg.invoke.return_value = {"messages": messages}
    with patch("langgraph.pregel.remote.RemoteGraph", return_value=rg), \
            patch("ConvAI.utils.agent_host_allowed", return_value=True):
        return generate_response_with_agent(agent, user, "yes please", thread or str(uuid.uuid4()))


class Parts(TestCase):
    def test_every_text_in_the_turn_when_asked(self):
        msgs = [H, SUMMARY, NOTE_CALL, TOOL, CHECK]
        self.assertEqual(remote_turn_parts(msgs, every=True),
                         [SUMMARY["content"], CHECK["content"]])
        # Today's rule, for every other agent: the cut at the tool step.
        self.assertEqual(remote_turn_reply(msgs), CHECK["content"])

    def test_text_spoken_while_calling_a_tool_counts(self):
        bye = {"type": "ai", "content": "Thanks, take care.",
               "tool_calls": [{"name": "end_conversation", "id": "2"}]}
        self.assertEqual(remote_turn_parts([H, bye, TOOL], every=True), ["Thanks, take care."])

    def test_the_reply_is_the_joined_text_and_carries_its_parts(self):
        reply = AgentReply.of(["One", " ", "Two "])
        self.assertEqual(reply, "One\n\nTwo")
        self.assertEqual(reply_parts(reply), ["One", "Two"])
        self.assertEqual(reply_parts("plain"), ["plain"])
        self.assertEqual(reply_parts(""), [])


class SeparateAgent(TestCase):
    def setUp(self):
        self.user = ConvAIUser.objects.create_user("u", password="x")

    def test_on_each_message_is_a_part(self):
        reply = _invoke(_remote(send_separately=True), self.user, [H, SUMMARY, CHECK])
        self.assertEqual(reply_parts(reply), [SUMMARY["content"], CHECK["content"]])

    def test_off_nothing_changes(self):
        reply = _invoke(_remote(), self.user, [H, SUMMARY, CHECK])
        self.assertEqual(reply, SUMMARY["content"] + "\n\n" + CHECK["content"])
        self.assertEqual(reply_parts(reply), [reply])

    def test_the_form_saves_it(self):
        form = AgentForm({"name": "R", "langgraph_name": "reco_v2", "host": "localhost",
                          "port": 8123, "send_separately": "on", "detectors": "{}"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.save().send_separately)


class Sending(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(name="Ada", lastname="L", phone_number="+447400123401")

    def test_sms_and_whatsapp_get_one_message_per_part_in_order(self):
        for channel, sender in (("sms", "send_sms_text"), ("whatsapp", "send_whatsapp_text")):
            with patch("ConvAI.tasks.process_received_message",
                       return_value=AgentReply.of(["Summary", "Does that look right?"])), \
                    patch(f"ConvAI.tasks.{sender}") as send, patch("ConvAI.tasks.time.sleep") as nap:
                tasks.job_reply_text(channel, "+447400123401", "yes")
            self.assertEqual([c.args for c in send.call_args_list],
                             [("+447400123401", "Summary"),
                              ("+447400123401", "Does that look right?")])
            nap.assert_called_once()

    def test_a_plain_reply_is_one_message(self):
        with patch("ConvAI.tasks.process_received_message", return_value="Hello"), \
                patch("ConvAI.tasks.send_sms_text") as send:
            tasks.job_reply_text("sms", "+447400123401", "hi")
        send.assert_called_once_with("+447400123401", "Hello")

    def test_the_history_keeps_one_joined_turn(self):
        self.patient.agent = _remote(send_separately=True)
        self.patient.save()
        with patch("ConvAI.utils.generate_response_langgraph",
                   return_value=AgentReply.of(["Summary", "Does that look right?"])), \
                patch("ConvAI.utils._review_after_message"):
            process_message_for_patient(self.patient, "yes")
        self.assertEqual(Message.objects.get().response_message,
                         "Summary\n\nDoes that look right?")


class Ending(TestCase):
    def setUp(self):
        cache.clear()
        self.reco = _remote(allow_callbacks=True)
        self.ada = Patient.objects.create(name="Ada", lastname="L", agent=self.reco,
                                          current_thread_id=str(uuid.uuid4()))
        self.conv = Conversation.objects.create(id=self.ada.current_thread_id, patient=self.ada,
                                                agent=self.reco)

    def post(self, conv=None, scopes=run_tokens.SCOPES):
        conv = conv or self.conv
        token = run_tokens.mint(conv.id, agent_id=self.reco.pk, patient_id=self.ada.pk, scopes=scopes)
        return self.client.post("/api/v1/run/end/", data={}, content_type="application/json",
                                HTTP_AUTHORIZATION=f"RunToken {token}")

    def test_the_next_message_starts_a_new_conversation(self):
        old = self.ada.current_thread_id
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ended"])
        self.ada.refresh_from_db()
        self.assertNotEqual(self.ada.current_thread_id, old)
        self.assertIsNotNone(Conversation.objects.get(pk=old).ended_at)

    def test_twice_is_the_same_as_once(self):
        self.post()
        self.ada.refresh_from_db()
        moved_to = self.ada.current_thread_id
        self.assertEqual(self.post().status_code, 200)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.current_thread_id, moved_to)

    def test_an_old_conversation_does_not_move_the_current_one(self):
        older = Conversation.objects.create(patient=self.ada, agent=self.reco)
        current = self.ada.current_thread_id
        self.post(older)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.current_thread_id, current)

    def test_a_token_without_the_scope_cannot(self):
        resp = self.post(scopes=(run_tokens.SUMMARY, run_tokens.VISIBILITY))
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(Conversation.objects.get(pk=self.conv.pk).ended_at)

    def test_no_client_no_thread_to_move(self):
        orphan = Conversation.objects.create(agent=self.reco)
        self.assertFalse(end_conversation(orphan))
        self.assertIsNotNone(orphan.ended_at)


class WebChats(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = ConvAIUser.objects.create_superuser("root", "r@example.org", "x")
        self.reco = _remote(allow_callbacks=True, send_separately=True)

    def _ends_during_run(self, thread_id):
        def run(agent, user, text, tid, **kw):
            end_conversation(Conversation.objects.get_or_create(id=tid)[0])
            return AgentReply.of(["Thanks for chatting.", "Take care!"])
        return run

    def test_the_agent_test_chat_gets_bubbles_and_is_told_to_restart(self):
        self.client.force_login(self.admin)
        tid = str(uuid.uuid4())
        with patch("ConvAI.views.agents.generate_response_with_agent",
                   side_effect=self._ends_during_run(tid)):
            out = self.client.post(reverse("agent_test_send", args=[self.reco.pk]),
                                   data={"message": "bye", "conversation_id": tid},
                                   content_type="application/json").json()
        self.assertEqual(out["bot_messages"], ["Thanks for chatting.", "Take care!"])
        self.assertTrue(out["reset"])

    def test_the_test_user_chat_too(self):
        tester = ConvAIUser.objects.create_user("tester", password="x")
        tester.groups.add(Group.objects.get_or_create(name="PatientTester")[0])
        Patient.objects.create(name="Ada", lastname="L", agent=self.reco, tester_account=tester)
        self.client.force_login(tester)
        with patch("ConvAI.utils.generate_response_langgraph",
                   side_effect=lambda p, text, tid, **kw: self._ends_during_run(tid)(None, p, text, tid)), \
                patch("ConvAI.utils._review_after_message"):
            out = self.client.post(reverse("send_external_message"), data={"message": "bye"},
                                   content_type="application/json").json()
        self.assertEqual(out["bot_messages"], ["Thanks for chatting.", "Take care!"])
        self.assertTrue(out["reset"])

    def test_an_ordinary_turn_does_not_reset(self):
        self.client.force_login(self.admin)
        with patch("ConvAI.views.agents.generate_response_with_agent", return_value="Hi"):
            out = self.client.post(reverse("agent_test_send", args=[self.reco.pk]),
                                   data={"message": "hi", "conversation_id": str(uuid.uuid4())},
                                   content_type="application/json").json()
        self.assertEqual((out["bot_messages"], out["reset"]), (["Hi"], False))

    def test_the_restart_button_is_on_both(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("agent_test", args=[self.reco.pk]))
        self.assertContains(page, 'id="restartButton"')
