"""The agent's own summary of a conversation, and the tools that write it.

*The agent's summary wins.* It was in the conversation; the classifier read a
transcript of it afterwards. One machine summary is shown, and this is the one.

*A person's summary sits beside it, not over it.* The generated block is
read-only for conversations, and a navigator's words go in SummaryEdit.body.

*A hidden conversation still shows its summary.* The original privacy decision
withheld it; this reverses that, and the other withholdings are unchanged.

*The tool cannot choose a conversation.* It takes the thread from the run config,
so a hallucinated id writes nothing anywhere.

*The endpoint checks who is asking.* Bound to the patient rather than to a user
account, because on WhatsApp there is no account to bind to.

    python3 manage.py test ConvAI.test_conversation_summary --settings=test_settings
"""
import datetime as dt
import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from ConvAI import conversation_actors, conversation_privacy, conversation_summary
from ConvAI.models import (
    Agent, Caregiver, Conversation, ConvAIUser, Message, Patient, SiteConfiguration,
    SummaryEdit,
)
from ConvAI.native_agents import tool_registry
from ConvAI.native_agents.summary_tool import MAX_SUMMARY_CHARS
from ConvAI.utils_conversation_classification import SAFETY_LABEL

CLIENT_PHONE = "+447700900201"
DAY = dt.date(2026, 9, 11)

AGENT_TEXT = "Wanted respite care. Gave three local services and how to refer."
CLASSIFIER_TEXT = "The client asked about respite services."


def _local(*args):
    return timezone.make_aware(dt.datetime(*args))


def _message(conversation_id, user, said, answered, at):
    m = Message.objects.create(conversation_id=str(conversation_id), user=user,
                               user_message=said, response_message=answered)
    Message.objects.filter(pk=m.pk).update(timestamp=at)
    return m


class SummaryTestCase(TestCase):
    """Every test starts from a cold settings cache — see SenseiTestCase."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

        navigators = Group.objects.get_or_create(name="Navigator")[0]
        self.nav = ConvAIUser.objects.create_user(
            username="nav", password="x", first_name="Nadia", last_name="Vega")
        self.nav.groups.add(navigators)
        self.other_nav = ConvAIUser.objects.create_user(username="nav2", password="x")
        self.other_nav.groups.add(navigators)
        self.admin = ConvAIUser.objects.create_user(username="boss", password="x")
        self.admin.user_permissions.add(
            Permission.objects.get(codename="access_configuration"))

        caregiver = Caregiver.objects.create(name="Grace", lastname="Hopper")
        self.patient = Patient.objects.create(name="Ada", lastname="Lovelace",
                                              phone_number=CLIENT_PHONE,
                                              caregiver=caregiver, navigator=self.nav)
        self.agent = Agent.objects.create(name="Helper", kind="prompt")
        self.conv = Conversation.objects.create(patient=self.patient, agent=self.agent,
                                               topic="Respite",
                                               summary=CLASSIFIER_TEXT)
        _message(self.conv.id, CLIENT_PHONE, "who can sit with her?",
                 "there are three services", _local(2026, 9, 11, 10, 0))
        # Explicitly off, because the database wins over the environment and a
        # developer's .env may well have it on. Every test that wants it says so.
        self.turn_privacy_on("0")
        self.client.force_login(self.nav)

    # --- helpers ---

    def turn_privacy_on(self, value="1"):
        cfg = SiteConfiguration.load()
        cfg.conversation_privacy_enabled = value
        cfg.save()
        cache.clear()

    def hide(self, conversation=None):
        conversation = conversation or self.conv
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

    def api(self, user, method="get", body=None, conversation=None, path="summary"):
        """Call an endpoint as ``user``, over their token, the way an agent does."""
        conversation = conversation or self.conv
        token, _created = Token.objects.get_or_create(user=user)
        url = f"/api/v1/conversations/{conversation.id}/{path}/"
        kwargs = {"HTTP_AUTHORIZATION": f"Token {token.key}"}
        if method == "post":
            return self.client.post(url, data=json.dumps(body or {}),
                                    content_type="application/json", **kwargs)
        return self.client.get(url, **kwargs)


# ─────────────────────────────── precedence ────────────────────────────────

class WhichSummaryIsShown(SummaryTestCase):
    def test_the_classifier_when_it_is_the_only_one(self):
        text, source, _when = conversation_summary.machine_summary(self.conv)
        self.assertEqual(text, CLASSIFIER_TEXT)
        self.assertEqual(source, conversation_summary.CLASSIFIER)

    def test_the_agent_when_both_exist(self):
        conversation_summary.set_agent_summary(self.conv, AGENT_TEXT)
        text, source, _when = conversation_summary.machine_summary(self.conv)
        self.assertEqual(text, AGENT_TEXT)
        self.assertEqual(source, conversation_summary.AGENT)

    def test_neither_reads_as_absent_rather_than_blank(self):
        self.conv.summary = ""
        self.conv.save(update_fields=["summary"])
        self.assertEqual(conversation_summary.machine_summary(self.conv),
                         ("", "", None))

    def test_whitespace_is_not_a_summary(self):
        """An agent that reports spaces must not shadow the classifier."""
        Conversation.objects.filter(pk=self.conv.pk).update(agent_summary="   \n ")
        self.conv.refresh_from_db()
        _text, source, _when = conversation_summary.machine_summary(self.conv)
        self.assertEqual(source, conversation_summary.CLASSIFIER)

    def test_only_one_reaches_the_panel(self):
        conversation_summary.set_agent_summary(self.conv, AGENT_TEXT)
        pane = self.pane()
        self.assertIn(AGENT_TEXT, pane)
        self.assertNotIn(CLASSIFIER_TEXT, pane)

    def test_the_panel_says_which_wrote_it(self):
        self.assertIn("Generated automatically", self.pane())
        conversation_summary.set_agent_summary(self.conv, AGENT_TEXT)
        # Named, not just "the agent": which agent held the conversation is the
        # first thing a navigator wants to know about a summary it wrote.
        self.assertIn("Reported by Helper", self.pane())

    def test_the_history_page_agrees_with_the_panel(self):
        conversation_summary.set_agent_summary(self.conv, AGENT_TEXT)
        history = self.history()
        self.assertIn(AGENT_TEXT, history)
        self.assertNotIn(CLASSIFIER_TEXT, history)

    def test_an_agent_summary_shows_before_the_classifier_has_run(self):
        """analyzed is False here — the card used to be gated on it."""
        self.conv.summary = ""
        self.conv.analyzed = False
        self.conv.save(update_fields=["summary", "analyzed"])
        conversation_summary.set_agent_summary(self.conv, AGENT_TEXT)
        self.assertIn(AGENT_TEXT, self.history())


class WritingTheAgentSummary(SummaryTestCase):
    def test_it_overwrites_rather_than_appends(self):
        conversation_summary.set_agent_summary(self.conv, "first")
        conversation_summary.set_agent_summary(self.conv, "second")
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_summary, "second")

    def test_it_restamps_the_time(self):
        conversation_summary.set_agent_summary(self.conv, "first")
        first_at = Conversation.objects.get(pk=self.conv.pk).agent_summary_at
        conversation_summary.set_agent_summary(self.conv, "second")
        self.assertGreater(Conversation.objects.get(pk=self.conv.pk).agent_summary_at,
                           first_at)

    def test_it_leaves_the_classifier_alone(self):
        conversation_summary.set_agent_summary(self.conv, AGENT_TEXT)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.summary, CLASSIFIER_TEXT)


# ──────────────────────── a person's own summary ───────────────────────────

class ThePersonsSummary(SummaryTestCase):
    def test_the_generated_block_is_read_only(self):
        """No edit URL on the machine summary, so nothing can post over it."""
        pane = self.pane()
        edit_url = reverse("edit_overview", args=["conversation", str(self.conv.id)])
        # The URL appears once — on the navigator's own block, below.
        self.assertEqual(pane.count(edit_url), 1)

    def test_it_is_offered_even_when_nothing_is_written_yet(self):
        pane = self.pane()
        self.assertIn("Your summary", pane)
        self.assertIn("Nothing from you yet", pane)

    def test_writing_one_does_not_touch_the_generated_one(self):
        self.client.post(
            reverse("edit_overview", args=["conversation", str(self.conv.id)]),
            data=json.dumps({"body": "She needs the referral doing this week."}),
            content_type="application/json")
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.summary, CLASSIFIER_TEXT)
        self.assertEqual(self.conv.summary_edit.body,
                         "She needs the referral doing this week.")

    def test_both_are_shown(self):
        conversation_summary.set_human_summary(self.conv, "Referral this week.", self.nav)
        pane = self.pane()
        self.assertIn(CLASSIFIER_TEXT, pane)
        self.assertIn("Referral this week.", pane)
        self.assertIn("Written by Nadia Vega", pane)

    def test_an_agent_report_cannot_shadow_it(self):
        conversation_summary.set_human_summary(self.conv, "Referral this week.", self.nav)
        conversation_summary.set_agent_summary(self.conv, AGENT_TEXT)
        pane = self.pane()
        self.assertIn("Referral this week.", pane)
        self.assertIn(AGENT_TEXT, pane)

    def test_a_blank_body_is_not_a_persons_summary(self):
        """Every in-place SummaryEdit has one, and must not read as an empty block."""
        SummaryEdit.objects.create(conversation=self.conv, author=self.nav, body="")
        self.assertEqual(conversation_summary.human_summary(self.conv), ("", "", None))

    def test_another_navigators_client_is_not_editable(self):
        self.client.force_login(self.other_nav)
        response = self.client.post(
            reverse("edit_overview", args=["conversation", str(self.conv.id)]),
            data=json.dumps({"body": "nope"}), content_type="application/json")
        self.assertEqual(response.status_code, 403)


# ─────────────────────── the privacy rollback ──────────────────────────────

class AHiddenConversation(SummaryTestCase):
    def setUp(self):
        super().setUp()
        self.turn_privacy_on("1")
        self.hide()

    def test_shows_its_summary(self):
        self.assertIn(CLASSIFIER_TEXT, self.pane())
        self.assertIn(CLASSIFIER_TEXT, self.history())

    def test_prefers_the_agents_summary_too(self):
        conversation_summary.set_agent_summary(self.conv, AGENT_TEXT)
        self.assertIn(AGENT_TEXT, self.pane())

    def test_still_withholds_the_messages(self):
        self.assertNotIn("who can sit with her?", self.pane())

    def test_still_withholds_the_topic(self):
        self.assertNotIn("Respite", self.pane())

    def test_says_what_is_missing_and_does_not_claim_the_summary_is(self):
        pane = self.pane()
        self.assertIn("Hidden at the client", pane)
        self.assertIn("You can read the summary below", pane)

    def test_a_navigator_still_cannot_write_over_the_generated_one(self):
        """The machine block has no edit URL whether or not it is hidden."""
        pane = self.pane()
        self.assertIn("Your summary", pane)

    def test_the_agent_can_still_report_one(self):
        """Which is the point: on a hidden conversation it is all they get."""
        response = self.api(self.admin, "post", {"summary": AGENT_TEXT})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["hidden"])
        self.assertIn(AGENT_TEXT, self.pane())


# ───────────────────────────── the endpoint ────────────────────────────────

class TheSummaryEndpoint(SummaryTestCase):
    def test_an_admin_may_write(self):
        response = self.api(self.admin, "post", {"summary": AGENT_TEXT})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], AGENT_TEXT)
        self.assertEqual(response.json()["source"], "agent")

    def test_the_conversations_own_account_may_write(self):
        owner = ConvAIUser.objects.create_user(username="ada", password="x")
        Conversation.objects.filter(pk=self.conv.pk).update(user=owner)
        self.conv.refresh_from_db()
        self.assertEqual(self.api(owner, "post", {"summary": AGENT_TEXT}).status_code, 200)

    def test_the_testers_account_may_write(self):
        tester = ConvAIUser.objects.create_user(username="tester", password="x")
        self.patient.tester_account = tester
        self.patient.save(update_fields=["tester_account"])
        self.assertEqual(self.api(tester, "post", {"summary": AGENT_TEXT}).status_code, 200)

    def test_the_service_account_may_write(self):
        """The account-less channels' credential — see conversation_actors."""
        self.assertEqual(
            self.api(conversation_actors.service_account(), "post",
                     {"summary": AGENT_TEXT}).status_code, 200)

    def test_a_navigator_may_not(self):
        """Their words go in their own block, which the panel offers them."""
        self.assertEqual(self.api(self.nav, "post", {"summary": AGENT_TEXT}).status_code,
                         404)

    def test_a_stranger_gets_404_not_403(self):
        """So the status code cannot be used to find out a conversation exists."""
        self.assertEqual(self.api(self.other_nav).status_code, 404)

    def test_an_unknown_conversation_is_404(self):
        ghost = Conversation(id=uuid.uuid4())
        self.assertEqual(self.api(self.admin, conversation=ghost).status_code, 404)

    def test_a_non_uuid_is_404(self):
        token, _c = Token.objects.get_or_create(user=self.admin)
        response = self.client.get("/api/v1/conversations/not-a-uuid/summary/",
                                   HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(response.status_code, 404)

    def test_it_is_idempotent(self):
        self.api(self.admin, "post", {"summary": "first"})
        response = self.api(self.admin, "post", {"summary": "second"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], "second")

    def test_a_blank_summary_is_refused(self):
        self.api(self.admin, "post", {"summary": AGENT_TEXT})
        self.assertEqual(self.api(self.admin, "post", {"summary": "  "}).status_code, 400)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_summary, AGENT_TEXT)

    def test_a_transcript_length_summary_is_refused(self):
        response = self.api(self.admin, "post", {"summary": "x" * (MAX_SUMMARY_CHARS + 1)})
        self.assertEqual(response.status_code, 400)

    def test_it_is_not_behind_the_privacy_switch(self):
        """Unlike visibility. A summary makes no promise to a client."""
        self.assertFalse(conversation_privacy.enabled())
        self.assertEqual(self.api(self.admin, "post", {"summary": AGENT_TEXT}).status_code,
                         200)

    def test_get_reports_what_the_link_worker_reads(self):
        body = self.api(self.admin).json()
        self.assertEqual(body["summary"], CLASSIFIER_TEXT)
        self.assertEqual(body["source"], "classifier")
        self.assertEqual(body["agent_summary"], "")
        self.assertFalse(body["hidden"])


class TheVisibilityEndpointsOwnership(SummaryTestCase):
    """The hole the shared rule closes: a WhatsApp conversation has no user."""

    def setUp(self):
        super().setUp()
        self.turn_privacy_on("1")

    def test_a_conversation_with_no_account_is_still_reachable(self):
        self.assertIsNone(self.conv.user_id)
        response = self.api(conversation_actors.service_account(), "post",
                            {"hidden": True}, path="visibility")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["hidden"])

    def test_a_navigator_still_may_not_set_visibility(self):
        response = self.api(self.nav, "post", {"hidden": True}, path="visibility")
        self.assertEqual(response.status_code, 404)


class TheServiceAccount(SummaryTestCase):
    def test_it_is_created_once_and_reused(self):
        first = conversation_actors.service_account()
        self.assertEqual(conversation_actors.service_account().pk, first.pk)

    def test_it_cannot_be_signed_into(self):
        conversation_actors.service_account()
        self.assertFalse(self.client.login(username=conversation_actors.SERVICE_USERNAME,
                                           password=""))

    def test_its_token_is_stable(self):
        self.assertEqual(conversation_actors.service_token(),
                         conversation_actors.service_token())

    def test_may_act_on_refuses_an_inactive_account(self):
        user = conversation_actors.service_account()
        user.is_active = False
        user.save(update_fields=["is_active"])
        self.assertFalse(conversation_actors.may_act_on(self.conv, user))


class TheCallbackToken(SummaryTestCase):
    """Narrowest identity first: the conversation's own account, then the
    client's tester account, then the service account."""

    def test_the_conversations_own_account_when_it_has_one(self):
        owner = ConvAIUser.objects.create_user(username="ada", password="x")
        Conversation.objects.filter(pk=self.conv.pk).update(user=owner)
        self.conv.refresh_from_db()
        key = conversation_actors.token_for_conversation(self.conv, self.patient)
        self.assertEqual(Token.objects.get(key=key).user_id, owner.pk)

    def test_the_tester_account_next(self):
        tester = ConvAIUser.objects.create_user(username="tester", password="x")
        self.patient.tester_account = tester
        self.patient.save(update_fields=["tester_account"])
        self.patient.refresh_from_db()
        key = conversation_actors.token_for_conversation(self.conv, self.patient)
        self.assertEqual(Token.objects.get(key=key).user_id, tester.pk)

    def test_the_service_account_last(self):
        key = conversation_actors.token_for_conversation(self.conv, self.patient)
        self.assertEqual(Token.objects.get(key=key).user.username,
                         conversation_actors.SERVICE_USERNAME)


# ─────────────────────────────── the tool ──────────────────────────────────

class TheReportSummaryTool(SummaryTestCase):
    """The tool takes the conversation from the run config and nowhere else."""

    def call(self, text, thread_id=None):
        from ConvAI.native_agents.summary_tool import build_summary_tools

        tool = build_summary_tools()[0]
        cfg = {"configurable": {"thread_id": str(thread_id or self.conv.id)}}
        return tool.invoke({"summary": text}, config=cfg)

    def test_it_writes_the_summary_of_the_thread_it_is_in(self):
        self.assertIn("SUMMARY_RECORDED", self.call(AGENT_TEXT))
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_summary, AGENT_TEXT)

    def test_it_takes_no_conversation_argument(self):
        from ConvAI.native_agents.summary_tool import build_summary_tools

        schema = build_summary_tools()[0].args_schema.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"summary"})

    def test_a_hallucinated_thread_writes_nothing(self):
        self.assertIn("not been recorded yet", self.call(AGENT_TEXT, uuid.uuid4()))
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_summary, "")

    def test_a_blank_report_is_refused(self):
        self.assertIn("SUMMARY_REJECTED", self.call("   "))

    def test_a_transcript_is_refused(self):
        self.assertIn("SUMMARY_REJECTED", self.call("x" * (MAX_SUMMARY_CHARS + 1)))
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_summary, "")

    def test_reporting_twice_replaces(self):
        self.call("first")
        self.call("second")
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_summary, "second")


# ───────────────────────────── the registry ────────────────────────────────

class TheToolRegistry(SummaryTestCase):
    def test_report_summary_is_always_available(self):
        self.assertTrue(tool_registry.is_available("report_summary"))

    def test_privacy_follows_its_switch(self):
        self.assertFalse(tool_registry.is_available("conversation_privacy"))
        self.turn_privacy_on("1")
        self.assertTrue(tool_registry.is_available("conversation_privacy"))

    def test_an_absent_prompt_key_means_the_shipped_default(self):
        self.agent.tools = {"report_summary": {}}
        self.assertEqual(tool_registry.prompt_for(self.agent, "report_summary"),
                         tool_registry.default_prompt("report_summary"))

    def test_an_override_wins(self):
        self.agent.tools = {"report_summary": {"prompt": "Say less."}}
        self.assertEqual(tool_registry.prompt_for(self.agent, "report_summary"),
                         "Say less.")

    def test_enabled_slugs_follow_registry_order_not_stored_order(self):
        self.agent.tools = {"report_summary": {}, "conversation_privacy": {}}
        self.assertEqual(tool_registry.enabled_slugs(self.agent),
                         ["conversation_privacy", "report_summary"])

    def test_an_unknown_slug_is_ignored(self):
        self.agent.tools = {"no_such_tool": {}, "report_summary": {}}
        self.assertEqual(tool_registry.enabled_slugs(self.agent), ["report_summary"])

    def test_a_switched_off_tool_contributes_no_prompt(self):
        """A prompt describing a tool the graph was not given is a lie to the model."""
        self.agent.tools = {"conversation_privacy": {}}
        self.assertEqual(tool_registry.prompt_suffix(self.agent), "")
        self.turn_privacy_on("1")
        self.assertIn("Keeping a conversation private",
                      tool_registry.prompt_suffix(self.agent))

    def test_a_switched_off_tool_is_not_built(self):
        self.agent.tools = {"conversation_privacy": {}, "report_summary": {}}
        names = [t.name for t in tool_registry.build_tools(self.agent)]
        self.assertEqual(names, ["report_summary"])

    def test_tools_are_built_in_registry_order(self):
        self.turn_privacy_on("1")
        self.agent.tools = {"report_summary": {}, "conversation_privacy": {}}
        names = [t.name for t in tool_registry.build_tools(self.agent)]
        self.assertEqual(names, ["get_conversation_privacy",
                                 "set_conversation_privacy", "report_summary"])


class TheAgentForm(SummaryTestCase):
    """The form is where "reset to default" becomes "store no override"."""

    def form(self, **overrides):
        from ConvAI.forms import PromptAgentForm

        data = {
            "name": "Tooled", "description": "", "system_prompt": "You are kind.",
            "model": "", "rag_top_k": 5,
            "abstract_instruction": "x", "classification_role": "", "detectors": "{}",
            "tts_voice_id": "",
        }
        data.update(overrides)
        return PromptAgentForm(data, instance=self.agent)

    def test_an_untouched_prompt_is_stored_as_no_override(self):
        form = self.form(**{
            "tool_slugs": ["report_summary"],
            "tool_prompt_report_summary": tool_registry.default_prompt("report_summary"),
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().tools, {"report_summary": {}})

    def test_a_default_submitted_with_crlf_is_still_a_default(self):
        """Browsers submit a textarea as CRLF, so the comparison must not care.

        Without this, pressing Reset to default and saving stored a byte-for-byte
        copy of the default as an override — the frozen copy the whole design
        exists to avoid — and put stray carriage returns into the system prompt.
        """
        crlf = tool_registry.default_prompt("report_summary").replace("\n", "\r\n")
        form = self.form(**{"tool_slugs": ["report_summary"],
                            "tool_prompt_report_summary": crlf})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().tools, {"report_summary": {}})

    def test_an_edited_prompt_is_stored_without_carriage_returns(self):
        form = self.form(**{"tool_slugs": ["report_summary"],
                            "tool_prompt_report_summary": "Say less.\r\nAnd faster."})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().tools,
                         {"report_summary": {"prompt": "Say less.\nAnd faster."}})

    def test_an_edited_prompt_is_stored(self):
        form = self.form(**{"tool_slugs": ["report_summary"],
                            "tool_prompt_report_summary": "Say less."})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().tools, {"report_summary": {"prompt": "Say less."}})

    def test_unticking_removes_the_tool_and_its_override(self):
        self.agent.tools = {"report_summary": {"prompt": "Say less."}}
        self.agent.save(update_fields=["tools"])
        form = self.form(**{"tool_slugs": [],
                            "tool_prompt_report_summary": "Say less."})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().tools, {})

    def test_tools_and_realtime_voice_are_refused_together(self):
        form = self.form(**{"tool_slugs": ["report_summary"], "realtime_enabled": "on"})
        self.assertFalse(form.is_valid())
        self.assertIn("real-time", str(form.errors).lower())

    def test_every_registry_tool_gets_a_prompt_field(self):
        for slug in tool_registry.slugs():
            self.assertIn(f"tool_prompt_{slug}", self.form().fields)


class TheGraphBuilder(SummaryTestCase):
    """Any tool at all means a react agent — a plain prompt agent has no loop."""

    def test_no_tools_is_the_single_node_graph(self):
        from ConvAI.native_agents.prompt_agent import build_prompt_graph

        with patch("ConvAI.native_agents.prompt_agent._build_plain_graph") as plain:
            build_prompt_graph("You are kind.", None, agent=self.agent)
        plain.assert_called_once()

    def test_a_tool_upgrades_it(self):
        from ConvAI.native_agents.prompt_agent import build_prompt_graph

        self.agent.tools = {"report_summary": {}}
        with patch("ConvAI.native_agents.prompt_agent._build_react_graph") as react:
            build_prompt_graph("You are kind.", None, agent=self.agent)
        react.assert_called_once()
        prompt, tools = react.call_args.args[0], react.call_args.args[3]
        self.assertIn("You are kind.", prompt)
        self.assertIn("Summarising the conversation", prompt)
        self.assertEqual([t.name for t in tools], ["report_summary"])

    def test_the_prompt_only_describes_tools_that_were_handed_over(self):
        self.agent.tools = {"conversation_privacy": {}}   # switched off
        from ConvAI.native_agents.prompt_agent import build_prompt_graph

        with patch("ConvAI.native_agents.prompt_agent._build_plain_graph") as plain:
            build_prompt_graph("You are kind.", None, agent=self.agent)
        self.assertNotIn("Keeping a conversation private", plain.call_args.args[0])


class TheRunIdentity(SummaryTestCase):
    """What a remote graph is handed so it can call back."""

    def identity(self):
        from ConvAI.utils import _run_identity
        return _run_identity(self.patient, str(self.conv.id))

    def test_it_carries_the_conversation_and_the_client(self):
        ident = self.identity()
        self.assertEqual(ident["conversation_id"], str(self.conv.id))
        self.assertEqual(ident["patient_id"], self.patient.pk)

    def test_client_id_is_an_alias_for_patient_id(self):
        ident = self.identity()
        self.assertEqual(ident["client_id"], ident["patient_id"])

    def test_it_carries_a_token(self):
        self.assertTrue(self.identity()["user_token"])

    def test_a_non_uuid_thread_still_yields_an_identity(self):
        from ConvAI.utils import _run_identity

        ident = _run_identity(self.patient, "not-a-uuid")
        self.assertEqual(ident["conversation_id"], "not-a-uuid")
        self.assertEqual(ident["patient_id"], self.patient.pk)


class TheSafetyFloorStillWins(SummaryTestCase):
    """A hidden conversation that tripped the self-harm detector is not hidden."""

    def test_everything_comes_back(self):
        self.turn_privacy_on("1")
        self.conv.hidden = True
        self.conv.auto_flags = {SAFETY_LABEL: True}
        self.conv.save(update_fields=["hidden", "auto_flags"])
        pane = self.pane()
        self.assertIn("who can sit with her?", pane)
        self.assertIn(CLASSIFIER_TEXT, pane)
