"""Client records: what staff may ask about their clients, and Link Worker v2.

*The screens' rule, and nothing wider.* A navigator reads their own clients and
an admin everyone — through the assistant and the API alike, because both call
``ConvAI.client_records``. A client outside that is refused exactly as a client
that does not exist.

*Summaries, never transcripts.* A conversation the client hid gives its summary
and nothing else; no message body is ever read.

*Every look is logged.* One ``RecordAccess`` row per client revealed.

*Off means v1.* With the switch off the chat bubble runs the original Link
Worker exactly as before.

    python3 manage.py test ConvAI.test_client_records --settings=test_settings
"""
import json
import os
import sys
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from ConvAI import client_records as records
from ConvAI.models import (
    Agent, Alert, Answer, Conversation, ConvAIUser, Meeting, Message, Note, Patient,
    Protocol, Question, RecordAccess, SiteConfiguration,
)

# The SDK is shipped from client_sdk/, not installed; import it from there, as
# test_sdk_run_tools does.
SDK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client_sdk")
if SDK_DIR not in sys.path:
    sys.path.insert(0, SDK_DIR)

PASSWORD = "test-pass-not-a-real-secret"
SECRET_WORDS = "words-from-a-chat-that-must-never-leave"


def _config(**kwargs):
    cfg = SiteConfiguration.load()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    cfg.save()
    cache.clear()


def _navigator(username):
    user = ConvAIUser.objects.create_user(username, password=PASSWORD)
    user.groups.add(Group.objects.get_or_create(name="Navigator")[0])
    return user


def _admin(username="boss"):
    return ConvAIUser.objects.create_user(username, password=PASSWORD,
                                          is_staff=True, is_superuser=True)


class CaseloadMixin:
    """Two navigators, a client each, and one client with a full record."""

    def setUp(self):
        cache.clear()
        self.nav = _navigator("nav")
        self.other_nav = _navigator("other")
        self.admin = _admin()

        self.ada = Patient.objects.create(name="Ada", lastname="Lovelace", navigator=self.nav,
                                          phone_number="+447700900001",
                                          details="Lives alone. Type 2 diabetes, diet-controlled.")
        self.bob = Patient.objects.create(name="Bob", lastname="Other", navigator=self.other_nav,
                                          details="Mentions diabetes too.")

        self.wellbeing = Protocol.objects.create(number=2, title="Weekly wellbeing")
        self.sleep_q = Question.objects.create(protocol=self.wellbeing, order=1,
                                               prompt_md="How have you been **sleeping**?")
        self.falls_q = Question.objects.create(protocol=self.wellbeing, order=2,
                                               prompt_md="Any falls this week?")
        self.iqcode = Protocol.objects.create(number=3, title="IQCODE cognitive decline",
                                              repeatable=True)
        self.memory_q = Question.objects.create(protocol=self.iqcode, order=1,
                                                prompt_md="Remembering recent events")

        now = timezone.now()
        self.call1 = Meeting.objects.create(patient=self.ada, scheduled_time=now - timedelta(days=30),
                                            status=Meeting.Status.COMPLETED)
        self.call2 = Meeting.objects.create(patient=self.ada, scheduled_time=now - timedelta(days=7),
                                            status=Meeting.Status.COMPLETED,
                                            protocol_summary="Sleep improving since the walks.")
        self.next_call = Meeting.objects.create(patient=self.ada,
                                                scheduled_time=now + timedelta(days=3))
        self.cancelled = Meeting.objects.create(patient=self.ada,
                                                scheduled_time=now + timedelta(days=4),
                                                status=Meeting.Status.CANCELLED)
        self.bobs_call = Meeting.objects.create(patient=self.bob,
                                                scheduled_time=now + timedelta(days=2))

        Answer.objects.create(meeting=self.call1, question=self.sleep_q,
                              response="Waking at three most nights.")
        Answer.objects.create(meeting=self.call2, question=self.sleep_q,
                              response="Better: sleeping through four nights of seven.")
        Answer.objects.create(meeting=self.call1, question=self.falls_q, response="None.")
        Answer.objects.create(meeting=self.call1, question=self.memory_q, response="Slightly worse")
        Answer.objects.create(meeting=self.call2, question=self.memory_q, response="Much worse")

        Note.objects.create(meeting=self.call2, author=self.nav, body="Referred to the falls clinic.")
        Alert.objects.create(patient=self.ada, title="Missed medication", priority=Alert.Priority.HIGH)

        self.open_conv = Conversation.objects.create(patient=self.ada, summary="Asked about day centres.",
                                                     topic="Services")
        self.hidden_conv = Conversation.objects.create(patient=self.ada, hidden=True,
                                                       summary="Talked about family worries.",
                                                       topic="Family")
        Message.objects.create(conversation_id=str(self.hidden_conv.id), user="ada",
                               user_message=SECRET_WORDS, response_message="ok", patient=self.ada)

    def tearDown(self):
        cache.clear()


class Caseload(CaseloadMixin, TestCase):
    pass


# ----------------------------------------------------------------------------
# The service
# ----------------------------------------------------------------------------

class VisibilityTests(Caseload):
    def test_a_navigator_sees_their_own_clients_only(self):
        self.assertEqual(list(records.visible_patients(self.nav)), [self.ada])

    def test_an_admin_sees_everyone(self):
        self.assertEqual(records.visible_patients(self.admin).count(), 2)

    def test_somebody_elses_client_reads_as_no_client_at_all(self):
        for patient_id in (self.bob.pk, 999999, "not-a-number"):
            with self.subTest(patient_id=patient_id):
                with self.assertRaises(records.NotVisible):
                    records.overview(self.nav, patient_id, via="agent")

    def test_an_inactive_account_sees_nobody(self):
        self.nav.is_active = False
        self.nav.save()
        self.assertEqual(records.visible_patients(self.nav).count(), 0)


class OverviewTests(Caseload):
    def test_it_gathers_the_record(self):
        out = records.overview(self.nav, self.ada.pk, via="agent")
        self.assertEqual(out["client"]["name"], "Ada Lovelace")
        self.assertEqual(out["client"]["phone"], "+447700900001")
        self.assertIn("diabetes", out["details"])
        self.assertEqual([m["id"] for m in out["next_meetings"]], [self.next_call.pk],
                         "a cancelled meeting is not an upcoming one")
        self.assertEqual(out["open_alerts"][0]["title"], "Missed medication")
        self.assertEqual(out["recent_notes"][0]["text"], "Referred to the falls clinic.")
        self.assertTrue(any(p["protocol"] == "2. Weekly wellbeing" and p["answered"] == 2
                            for p in out["protocols"]))

    def test_a_hidden_conversation_gives_its_summary_and_nothing_else(self):
        out = records.overview(self.nav, self.ada.pk, via="agent")
        hidden = [c for c in out["recent_conversations"] if c.get("hidden_by_client")]
        self.assertEqual(len(hidden), 1)
        self.assertEqual(hidden[0]["summary"], "Talked about family worries.")
        self.assertNotIn("topic", hidden[0], "the topic is withheld with the words")

    def test_an_admin_is_not_withheld_from(self):
        out = records.overview(self.admin, self.ada.pk, via="agent")
        self.assertFalse(any(c.get("hidden_by_client") for c in out["recent_conversations"]))

    def test_no_message_body_is_ever_read(self):
        blob = json.dumps(records.overview(self.admin, self.ada.pk, via="agent"))
        self.assertNotIn(SECRET_WORDS, blob)

    def test_the_output_is_json(self):
        json.dumps(records.overview(self.nav, self.ada.pk, via="agent"))


class ProtocolTests(Caseload):
    def test_the_latest_answer_wins(self):
        out = records.protocol_answers(self.nav, self.ada.pk, "2", via="agent")
        sleep = out["protocols"][0]["answers"][0]
        self.assertTrue(sleep["question"].startswith("Q1. How have you been sleeping"),
                        "the question reads as plain text, not Markdown")
        self.assertEqual(sleep["answer"], "Better: sleeping through four nights of seven.")

    def test_a_protocol_can_be_named_the_way_the_answers_name_it(self):
        """A model hands back "3. IQCODE …" because that is how it was shown it."""
        out = records.protocol_history(self.nav, self.ada.pk, "3. IQCODE cognitive decline", via="agent")
        self.assertEqual(out["protocol"], "3. IQCODE cognitive decline")

    def test_a_protocol_can_be_named_by_its_title(self):
        out = records.protocol_answers(self.nav, self.ada.pk, "iqcode", via="agent")
        self.assertEqual(out["protocols"][0]["protocol"], "3. IQCODE cognitive decline")

    def test_every_protocol_when_none_is_named(self):
        out = records.protocol_answers(self.nav, self.ada.pk, via="agent")
        self.assertEqual({p["protocol"] for p in out["protocols"]},
                         {"2. Weekly wellbeing", "3. IQCODE cognitive decline"})

    def test_an_unknown_or_ambiguous_protocol_is_refused_by_name(self):
        Protocol.objects.create(number=4, title="Weekly mood")
        for ref in ("99", "nothing-like-it", "weekly"):
            with self.subTest(ref=ref):
                with self.assertRaises(records.BadQuestion):
                    records.protocol_answers(self.nav, self.ada.pk, ref, via="agent")

    def test_history_runs_call_by_call_oldest_first(self):
        out = records.protocol_history(self.nav, self.ada.pk, "3", via="agent")
        self.assertEqual([r["answers"][0]["answer"] for r in out["rounds"]],
                         ["Slightly worse", "Much worse"])
        self.assertIsNone(out["note"])

    def test_history_carries_the_meetings_own_summary(self):
        out = records.protocol_history(self.nav, self.ada.pk, "2", via="agent")
        self.assertEqual(out["rounds"][-1]["meeting_summary"], "Sleep improving since the walks.")

    def test_history_says_when_there_is_nothing_to_compare(self):
        Answer.objects.filter(meeting=self.call2).delete()
        out = records.protocol_history(self.nav, self.ada.pk, "3", via="agent")
        self.assertIn("Only one call", out["note"])


class MeetingTests(Caseload):
    def test_the_caseload_is_the_navigators_own(self):
        out = records.upcoming_meetings(self.nav, via="agent")
        self.assertEqual([m["id"] for m in out["meetings"]], [self.next_call.pk])

    def test_an_admin_sees_everybodys_soonest_first(self):
        out = records.upcoming_meetings(self.admin, via="agent")
        self.assertEqual([m["id"] for m in out["meetings"]], [self.bobs_call.pk, self.next_call.pk])

    def test_the_window_is_respected(self):
        self.assertEqual(records.upcoming_meetings(self.nav, days=1, via="agent")["total"], 0)

    def test_one_client_outside_the_caseload_is_refused(self):
        with self.assertRaises(records.NotVisible):
            records.upcoming_meetings(self.nav, patient_id=self.bob.pk, via="agent")


class SearchTests(Caseload):
    def test_it_finds_a_phrase_across_the_written_record(self):
        out = records.search_records(self.nav, "falls", via="agent")
        self.assertEqual({h["source"] for h in out["hits"]}, {"note"})
        out = records.search_records(self.nav, "diabetes", via="agent")
        self.assertEqual([h["client"]["name"] for h in out["hits"]], ["Ada Lovelace"],
                         "another navigator's client must not appear")

    def test_an_admin_searches_everyone(self):
        out = records.search_records(self.admin, "diabetes", via="agent")
        self.assertEqual(out["clients_matched"], 2)

    def test_it_never_searches_message_bodies(self):
        out = records.search_records(self.admin, SECRET_WORDS[:20], via="agent")
        self.assertEqual(out["total"], 0)

    def test_a_phrase_matches_at_the_start_of_a_word(self):
        """"fall" finds "falls" and "falling", never the middle of a word."""
        Note.objects.create(meeting=self.call1, author=self.nav, body="She will call back.")
        self.assertEqual(records.search_records(self.nav, "ill", via="agent")["total"], 0,
                         '"ill" must not match "will"')
        self.assertEqual(records.search_records(self.nav, "fall", via="agent")["total"], 1,
                         '"fall" matches "falls" (the falls clinic note)')
        self.assertEqual(records.search_records(self.nav, "clinic", via="agent")["hits"][0]["snippet"],
                         "Referred to the falls clinic.")

    def test_an_alert_from_a_hidden_conversation_keeps_only_its_title(self):
        """As on the alert panel: the classifier's sentences stay with the exchange."""
        Alert.objects.create(patient=self.ada, title="Low mood", alert_type=Alert.AlertType.CONVERSATION,
                             description="She said the grandchildren never visit.",
                             data={"conversation_id": str(self.hidden_conv.id), "detector": "Low mood"})
        self.assertEqual(records.search_records(self.nav, "grandchildren", via="agent")["total"], 0)
        self.assertEqual(records.search_records(self.admin, "grandchildren", via="agent")["total"], 1,
                         "an admin is not withheld from")
        hit = records.search_records(self.nav, "low mood", via="agent")["hits"][0]
        self.assertEqual(hit["snippet"], "Low mood", "the title stands, without the description")

    def test_a_superseded_summary_is_not_searched(self):
        """The agent's summary is the one shown; the classifier's under it is not."""
        self.open_conv.agent_summary = "Asked about the Tuesday lunch club."
        self.open_conv.summary = "Mentioned money worries."
        self.open_conv.save()
        self.assertEqual(records.search_records(self.admin, "money", via="agent")["total"], 0)
        self.assertEqual(records.search_records(self.admin, "lunch", via="agent")["total"], 1)

    def test_a_search_needs_three_characters(self):
        with self.assertRaises(records.BadQuestion):
            records.search_records(self.nav, "ab", via="agent")


class AccessLogTests(Caseload):
    def test_reading_a_record_is_logged(self):
        records.protocol_history(self.nav, self.ada.pk, "3", via="agent")
        row = RecordAccess.objects.get()
        self.assertEqual((row.user, row.patient, row.action, row.via, row.detail),
                         (self.nav, self.ada, "history", "agent", "protocol 3"))
        self.assertEqual(row.patient_label, "Ada Lovelace")

    def test_a_search_logs_each_client_it_revealed(self):
        records.search_records(self.admin, "diabetes", via="api")
        self.assertEqual(set(RecordAccess.objects.values_list("patient__name", flat=True)),
                         {"Ada", "Bob"})

    def test_a_refusal_reveals_nothing_and_logs_nothing(self):
        with self.assertRaises(records.NotVisible):
            records.overview(self.nav, self.bob.pk, via="agent")
        self.assertEqual(RecordAccess.objects.count(), 0)

    def test_the_log_is_append_only(self):
        records.overview(self.nav, self.ada.pk, via="agent")
        row = RecordAccess.objects.get()
        row.detail = "tampered"
        with self.assertRaises(ValidationError):
            row.save()

    def test_the_log_survives_the_client_being_deleted(self):
        records.overview(self.nav, self.ada.pk, via="agent")
        self.ada.delete()
        self.assertEqual(RecordAccess.objects.get().patient_label, "Ada Lovelace")


# ----------------------------------------------------------------------------
# The API
# ----------------------------------------------------------------------------

class ApiTests(Caseload):
    def setUp(self):
        super().setUp()
        _config(link_worker_v2_enabled="1")

    def test_with_the_switch_off_the_endpoints_do_not_exist(self):
        """An installation that has not opted in gains no new way to read records."""
        _config(link_worker_v2_enabled="")
        token = Token.objects.create(user=self.admin)
        for url in (reverse("api_client_overview", args=[self.ada.pk]),
                    reverse("api_meetings_upcoming") + "?days=x",
                    reverse("api_records_search") + "?q=falls"):
            with self.subTest(url=url):
                resp = self.client.get(url, HTTP_AUTHORIZATION=f"Token {token.key}")
                self.assertEqual(resp.status_code, 404)
        self.assertEqual(RecordAccess.objects.count(), 0)

    def _get(self, url, user):
        token = Token.objects.create(user=user)
        return self.client.get(url, HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_the_endpoints_answer_as_the_token_holder(self):
        urls = [
            reverse("api_client_overview", args=[self.ada.pk]),
            reverse("api_meetings_upcoming") + f"?patient_id={self.ada.pk}&days=10",
            reverse("api_client_protocol_answers", args=[self.ada.pk]) + "?protocol=2",
            reverse("api_client_protocol_history", args=[self.ada.pk, "iqcode"]),
            reverse("api_records_search") + "?q=falls",
        ]
        token = Token.objects.create(user=self.nav)
        for url in urls:
            with self.subTest(url=url):
                resp = self.client.get(url, HTTP_AUTHORIZATION=f"Token {token.key}")
                self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(set(RecordAccess.objects.values_list("via", flat=True)), {"api"})

    def test_another_navigators_client_is_a_404(self):
        resp = self._get(reverse("api_client_overview", args=[self.bob.pk]), self.nav)
        self.assertEqual(resp.status_code, 404)

    def test_no_token_no_answer(self):
        resp = self.client.get(reverse("api_client_overview", args=[self.ada.pk]))
        self.assertIn(resp.status_code, (401, 403))

    def test_a_bad_question_is_a_400_that_says_why(self):
        resp = self._get(reverse("api_client_protocol_history", args=[self.ada.pk, "99"]), self.nav)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no protocol 99", resp.json()["detail"])

    def test_the_sdk_calls_the_same_paths(self):
        """Each SDK method, routed through Django's test client."""
        from conversationalcare_api import Client

        token = Token.objects.create(user=self.nav)
        sdk = Client(base_url="http://testserver", token=token.key)
        test_client = self.client

        class _Resp:
            def __init__(self, r):
                self.status_code, self._r = r.status_code, r
                self.text = r.content.decode()

            def json(self):
                return self._r.json()

        def get(url, params=None, timeout=None):
            path = url.replace("http://testserver", "")
            return _Resp(test_client.get(path, params or {},
                                         HTTP_AUTHORIZATION=f"Token {token.key}"))

        with patch.object(sdk.session, "get", side_effect=get):
            self.assertEqual(sdk.client_overview(self.ada.pk)["client"]["name"], "Ada Lovelace")
            self.assertIsNone(sdk.client_overview(self.bob.pk))
            self.assertEqual(sdk.upcoming_meetings()["total"], 1)
            self.assertEqual(len(sdk.protocol_answers(self.ada.pk, "iqcode")["protocols"]), 1)
            self.assertEqual(len(sdk.protocol_history(self.ada.pk, 3)["rounds"]), 2)
            self.assertEqual(sdk.search_records("falls")["clients_matched"], 1)


# ----------------------------------------------------------------------------
# The agent
# ----------------------------------------------------------------------------

class ActorTests(Caseload):
    def test_only_the_staff_id_the_server_sets_counts(self):
        from ConvAI.native_agents.link_worker_v2 import actor_from

        self.assertEqual(actor_from({"staff_user_id": self.nav.pk}), self.nav)
        self.assertIsNone(actor_from({"user_id": self.nav.pk}),
                          "user_id is a client's id in a client's conversation")
        self.assertIsNone(actor_from({"user_token": Token.objects.create(user=self.nav).key}))

    def test_a_client_facing_account_is_not_staff(self):
        from ConvAI.native_agents.link_worker_v2 import actor_from

        tester = ConvAIUser.objects.create_user("tester", password=PASSWORD)
        tester.groups.add(Group.objects.get_or_create(name="PatientTester")[0])
        self.assertIsNone(actor_from({"staff_user_id": tester.pk}))


def _scripted_model(calls):
    """A chat model that makes the given tool calls, then says it is done."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class Scripted(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    replies = [AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"call{i}"}])
               for i, (n, a) in enumerate(calls)] + [AIMessage(content="Done.")]
    return Scripted(messages=iter(replies))


class AgentGraphTests(CaseloadMixin, TransactionTestCase):
    """The real graph and the real tools, with a scripted model in place of an LLM.

    A TransactionTestCase because LangGraph runs tools in a worker thread, and
    the test database is SQLite, which a TestCase's open transaction would lock
    against that thread.
    """

    def _run(self, calls, configurable):
        from langgraph.checkpoint.memory import MemorySaver

        from ConvAI.native_agents import link_worker_v2

        with patch("ConvAI.llm_factory.make_llm", return_value=_scripted_model(calls)):
            graph = link_worker_v2.build(MemorySaver())
        out = graph.invoke({"messages": [{"role": "user", "content": "question"}]},
                           {"configurable": {"thread_id": "t1", **configurable}})
        return [m.content for m in out["messages"] if m.type == "tool"]

    def test_the_tools_answer_as_the_signed_in_navigator(self):
        results = self._run([("upcoming_meetings", {}),
                             ("protocol_history", {"client_id": self.ada.pk, "protocol": "iqcode"})],
                            {"staff_user_id": self.nav.pk})
        self.assertIn("Ada Lovelace", results[0])
        self.assertIn("Much worse", results[1])
        self.assertEqual(set(RecordAccess.objects.values_list("via", flat=True)), {"agent"})

    def test_without_a_staff_id_the_tools_refuse(self):
        results = self._run([("client_overview", {"client_id": self.ada.pk})],
                            {"user_id": self.nav.pk})
        self.assertIn("only answers staff", results[0])
        self.assertEqual(RecordAccess.objects.count(), 0)

    def test_another_navigators_client_is_not_visible(self):
        results = self._run([("client_overview", {"client_id": self.bob.pk})],
                            {"staff_user_id": self.nav.pk})
        self.assertTrue(results[0].startswith("NOT_VISIBLE"))

    def test_it_runs_the_way_the_platform_runs_it(self):
        """Asynchronously, inside an event loop, where Django refuses the ORM.

        The platform ainvoke()s native agents. Anything that touches the
        database on the loop — the prompt, not only the tools — fails there
        with SynchronousOnlyOperation while passing a synchronous invoke().
        """
        import asyncio

        from langgraph.checkpoint.memory import MemorySaver

        from ConvAI.native_agents import link_worker_v2

        model = _scripted_model([("upcoming_meetings", {})])
        with patch("ConvAI.llm_factory.make_llm", return_value=model):
            graph = link_worker_v2.build(MemorySaver())
        out = asyncio.run(graph.ainvoke(
            {"messages": [{"role": "user", "content": "next meeting?"}]},
            {"configurable": {"thread_id": "t-async", "staff_user_id": self.nav.pk}}))
        tool_answers = [m.content for m in out["messages"] if m.type == "tool"]
        self.assertIn("Ada Lovelace", tool_answers[0])
        self.assertEqual(out["messages"][-1].content, "Done.")

    def test_a_fault_in_a_tool_is_reported_not_raised(self):
        with patch("ConvAI.client_records.overview", side_effect=RuntimeError("db down")):
            with self.assertLogs("ConvAI.native_agents.link_worker_v2", level="ERROR"):
                results = self._run([("client_overview", {"client_id": self.ada.pk})],
                                    {"staff_user_id": self.nav.pk})
        self.assertTrue(results[0].startswith("CANNOT_ANSWER"))

    def test_the_prompts_clock_is_the_one_the_tools_use(self):
        """Tool times are Django-local; "Now" in the prompt must be too."""
        from langgraph.checkpoint.memory import MemorySaver

        from ConvAI.native_agents import link_worker_v2

        seen = {}

        class Recording(type(_scripted_model([]))):
            def _generate(self, messages, *args, **kwargs):
                seen["system"] = messages[0].content
                return super()._generate(messages, *args, **kwargs)

        from langchain_core.messages import AIMessage
        with patch("ConvAI.llm_factory.make_llm",
                   return_value=Recording(messages=iter([AIMessage(content="ok")]))):
            graph = link_worker_v2.build(MemorySaver())
        with self.settings(TIME_ZONE="Pacific/Auckland"):
            graph.invoke({"messages": [{"role": "user", "content": "hi"}]},
                         {"configurable": {"thread_id": "tz", "staff_user_id": self.nav.pk}})
        self.assertIn("(Pacific/Auckland)", seen["system"])

    def test_a_long_answer_is_cut_and_says_so(self):
        from ConvAI.native_agents.link_worker_v2 import MAX_TOOL_CHARS, as_tool_output

        text = as_tool_output({"x": "y" * (MAX_TOOL_CHARS * 2)})
        self.assertLess(len(text), MAX_TOOL_CHARS + 100)
        self.assertIn("narrower question", text)


# ----------------------------------------------------------------------------
# The switch
# ----------------------------------------------------------------------------

class BubbleRoutingTests(Caseload):
    def setUp(self):
        super().setUp()
        self.v1 = Agent.objects.create(name="Link Worker", kind="native", native_key="link_worker")
        self.v2 = Agent.objects.create(name="Link Worker v2 (beta)", kind="native",
                                       native_key="link_worker_v2")
        self.client.force_login(self.nav)

    def _send(self):
        with patch("ConvAI.views.chat.generate_response_with_agent",
                   return_value="hi") as gen:
            resp = self.client.post(reverse("send_chat_message"),
                                    data=json.dumps({"message": "hello",
                                                     "conversation_id": "c-1"}),
                                    content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        return gen.call_args

    def test_off_the_bubble_runs_v1_as_before(self):
        call = self._send()
        self.assertEqual(call.args[0], self.v1)
        self.assertIn("user_token", call.kwargs["extra_configurable"])

    def test_on_the_bubble_runs_v2_with_no_token(self):
        _config(link_worker_v2_enabled="1")
        call = self._send()
        self.assertEqual(call.args[0], self.v2)
        extra = call.kwargs["extra_configurable"]
        self.assertEqual(extra["staff_user_id"], self.nav.pk)
        self.assertNotIn("user_token", extra)

    def test_on_without_the_v2_row_falls_back_to_v1(self):
        _config(link_worker_v2_enabled="1")
        self.v2.delete()
        self.assertEqual(self._send().args[0], self.v1)


class AgentTestChatTests(Caseload):
    """An admin trying v2 from the Agents page is answered as staff."""

    def test_the_test_chat_says_who_is_asking(self):
        v2 = Agent.objects.create(name="Link Worker v2 (beta)", kind="native",
                                  native_key="link_worker_v2")
        self.client.force_login(self.admin)
        with patch("ConvAI.views.agents.generate_response_with_agent", return_value="hi") as gen:
            self.client.post(reverse("agent_test_send", args=[v2.pk]),
                             data=json.dumps({"message": "hello"}),
                             content_type="application/json")
        self.assertEqual(gen.call_args.kwargs["extra_configurable"],
                         {"staff_user_id": self.admin.pk, "is_admin": True})

    def test_other_agents_are_tested_as_before(self):
        other = Agent.objects.create(name="Loopback", kind="native", native_key="loopback")
        self.client.force_login(self.admin)
        with patch("ConvAI.views.agents.generate_response_with_agent", return_value="hi") as gen:
            self.client.post(reverse("agent_test_send", args=[other.pk]),
                             data=json.dumps({"message": "hello"}),
                             content_type="application/json")
        self.assertIsNone(gen.call_args.kwargs["extra_configurable"])


class OffLooksUntouchedTests(Caseload):
    """With v2 off, the bubble a navigator sees is the bubble they saw before."""

    def setUp(self):
        super().setUp()
        self.nav.agent = Agent.objects.create(name="Any", kind="native", native_key="loopback")
        self.nav.save()
        self.client.force_login(self.nav)

    def test_the_bubble_carries_no_trace_of_v2(self):
        body = self.client.get(reverse("patients")).content.decode()
        self.assertIn('id="chatbot-window"', body, "the bubble must be on the page to test it")
        for marker in ("v2 beta", "chatbot-beta", "Ask about a client"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, body)

    def test_switched_on_the_bubble_says_which_assistant_it_is(self):
        _config(link_worker_v2_enabled="1")
        body = self.client.get(reverse("patients")).content.decode()
        self.assertIn("v2 beta", body)
        self.assertIn("Ask about a client", body)


class StaffOnlyAgentTests(Caseload):
    """v2 answers staff only, so it is never offered for a client's conversations.

    Otherwise it would sit in every agent picker whether or not it was switched
    on — a change navigators could see on an installation that never opted in.
    """

    def setUp(self):
        super().setUp()
        self.v1 = Agent.objects.create(name="Link Worker", kind="native", native_key="link_worker")
        self.v2 = Agent.objects.create(name="Link Worker v2 (beta)", kind="native",
                                       native_key="link_worker_v2")

    def test_it_is_not_among_the_agents_for_clients(self):
        self.assertIn(self.v1, Agent.for_clients())
        self.assertNotIn(self.v2, Agent.for_clients())

    def test_no_client_picker_offers_it(self):
        from ConvAI.forms import ClientForm, PatientForm

        for form in (ClientForm(), ClientForm(is_admin=True), PatientForm()):
            with self.subTest(form=type(form).__name__):
                self.assertNotIn(self.v2, form.fields["agent"].queryset)
                self.assertIn(self.v1, form.fields["agent"].queryset)

        self.client.force_login(self.nav)
        body = self.client.get(reverse("patient_detail", args=[self.ada.pk])).content.decode()
        self.assertIn("Link Worker", body, "the page must list agents for this to mean anything")
        self.assertNotIn("Link Worker v2", body)

    def test_it_cannot_be_given_to_a_client_by_hand(self):
        self.client.force_login(self.nav)
        self.client.post(reverse("update_client_terms", args=[self.ada.pk]),
                         {"agent": str(self.v2.pk)})
        self.ada.refresh_from_db()
        self.assertIsNone(self.ada.agent_id)

        self.client.post(reverse("update_client_terms", args=[self.ada.pk]),
                         {"agent": str(self.v1.pk)})
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.agent_id, self.v1.pk, "an ordinary agent is still assignable")
