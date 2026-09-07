"""Sensei agents: the flag that gates them, the opaque id, and the passcode.

Three things here are worth holding still.

*The flag is off unless someone turns it on.* Sensei is one installation's
integration, not a platform feature, and a fresh install must not offer it.

*The id sent to Sensei must not identify anyone.* It is an HMAC, so it has to
stay stable across turns (or a caregiver is logged out every message) and stay
distinct across id spaces (or an admin testing the agent lands in a patient's
Sensei account).

*The passcode must not reach the database.* It arrives in an ordinary chat
message, and every inbound turn is stored, read by navigators, and swept by the
classifier.

    python3 manage.py test ConvAI.test_sensei --settings=test_settings
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ConvAI import sensei
from ConvAI.models import Agent, ConvAIUser, Message, Patient, SiteConfiguration
from ConvAI.utils import save_message


def _configure(enabled="1", secret="test-secret"):
    """Point the adapter at a fake Sensei with the flag in a known state."""
    cfg = SiteConfiguration.load()
    cfg.sensei_enabled = enabled
    cfg.sensei_api_url = "https://sensei.example/api/send_message"
    cfg.sensei_function_key = "test-key"
    cfg.sensei_user_id_secret = secret
    cfg.save()
    return cfg


class SenseiTestCase(TestCase):
    """Every test starts from a cold settings cache.

    ``SiteConfiguration.load()`` memoises the singleton for 30 seconds in the
    process cache, which no test rolls back — so without this a test that turns
    Sensei on hands the next one an installation that already has it on, and
    "off by default" quietly stops being tested.
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


class TheFlag(SenseiTestCase):
    """Off by default; nothing Sensei-shaped works until it is on."""

    def test_it_is_off_on_a_fresh_installation(self):
        self.assertFalse(sensei.enabled())

    def test_a_disabled_installation_says_so_rather_than_calling_out(self):
        _configure(enabled="0")
        ok, reason = sensei.configured()
        self.assertFalse(ok)
        self.assertIn("disabled", reason)

    def test_turning_it_on_without_an_endpoint_is_not_configured(self):
        cfg = _configure()
        cfg.sensei_api_url = ""
        cfg.save()
        ok, reason = sensei.configured()
        self.assertFalse(ok)
        self.assertIn("URL", reason)

    def test_a_fully_filled_in_installation_is_ready(self):
        _configure()
        self.assertEqual(sensei.configured(), (True, ""))


class TheOpaqueId(SenseiTestCase):
    def setUp(self):
        super().setUp()
        _configure()
        self.patient = Patient.objects.create(name="Ada", lastname="Lovelace")

    def test_it_is_namespaced_so_sensei_can_tell_it_is_ours(self):
        self.assertTrue(sensei.external_user_id(self.patient).startswith("cc_"))

    def test_it_is_a_digest_and_nothing_else(self):
        # The whole point: Sensei learns nothing about who this is. Asserting
        # the shape rather than "the pk is absent" — a one-digit pk turns up in
        # a 64-character hex string by chance, which would pass either way.
        user_id = sensei.external_user_id(self.patient)
        self.assertRegex(user_id, r"^cc_[0-9a-f]{64}$")

    def test_it_is_the_same_every_turn(self):
        # A changing id would log the caregiver out of Sensei on every message.
        self.assertEqual(sensei.external_user_id(self.patient),
                         sensei.external_user_id(self.patient))

    def test_two_patients_get_different_ids(self):
        other = Patient.objects.create(name="Grace", lastname="Hopper")
        self.assertNotEqual(sensei.external_user_id(self.patient),
                            sensei.external_user_id(other))

    def test_a_user_and_a_patient_sharing_a_pk_get_different_ids(self):
        # The agent test chat speaks as the logged-in admin, and the two primary
        # key spaces overlap. Without the model label in the digest, an admin
        # testing the agent would answer as whichever patient shares their pk.
        user = ConvAIUser.objects.create_user(username="admin1", password="x")
        user.pk = self.patient.pk
        self.assertNotEqual(sensei.external_user_id(self.patient),
                            sensei.external_user_id(user))

    def test_it_changes_when_the_secret_changes(self):
        before = sensei.external_user_id(self.patient)
        _configure(secret="a-different-secret")
        self.assertNotEqual(before, sensei.external_user_id(self.patient))


class CredentialCommands(SenseiTestCase):
    def test_login_becomes_a_structured_login_operation(self):
        self.assertEqual(
            sensei.parse_command("/login ada 1234"),
            {"operation": "login", "app_user_id": "ada", "passcode": "1234"},
        )

    def test_register_user_becomes_the_api_s_register_operation(self):
        # The command the user types and the operation the API takes differ.
        self.assertEqual(
            sensei.parse_command("/register_user ada 1234")["operation"], "register")

    def test_the_command_is_case_insensitive_and_may_be_padded(self):
        self.assertEqual(sensei.parse_command("  /LOGIN ada 1234 ")["app_user_id"], "ada")

    def test_ordinary_chat_is_not_a_command(self):
        self.assertIsNone(sensei.parse_command("how did I sleep last night?"))
        self.assertFalse(sensei.is_credential_command("how did I sleep last night?"))

    def test_a_malformed_command_is_recognised_but_not_parsed(self):
        # Recognised so it can be answered with help, not parsed so a passcode
        # is never guessed out of an ambiguous line.
        self.assertIsNone(sensei.parse_command("/login ada"))
        self.assertTrue(sensei.is_credential_command("/login ada"))


class Redaction(SenseiTestCase):
    def test_the_passcode_is_replaced_and_the_account_name_kept(self):
        # The account name is what makes a failed login diagnosable.
        self.assertEqual(sensei.redact("/login ada 1234"),
                         f"/login ada {sensei.REDACTED}")

    def test_register_user_is_redacted_too(self):
        self.assertEqual(sensei.redact("/register_user ada 1234"),
                         f"/register_user ada {sensei.REDACTED}")

    def test_ordinary_messages_are_untouched(self):
        self.assertEqual(sensei.redact("how did I sleep?"), "how did I sleep?")

    def test_a_malformed_command_carries_no_passcode_to_strip(self):
        self.assertEqual(sensei.redact("/login ada"), "/login ada")

    def test_a_mistyped_command_with_extra_words_still_loses_its_passcode(self):
        # The line does not parse, so it is never sent — but it was still typed,
        # and a passcode in it must not be what ends up in the transcript.
        self.assertNotIn("s3cret", sensei.redact("/login ada s3cret oops"))

    def test_a_passcode_on_its_own_line_is_removed_too(self):
        self.assertNotIn("s3cret", sensei.redact("/login ada\ns3cret"))

    def test_the_passcode_never_reaches_the_database(self):
        # save_message is the choke point every inbound turn is stored from.
        patient = Patient.objects.create(name="Ada", lastname="Lovelace")
        msg = save_message("+440000000000", "/login ada s3cret", "Signed in.",
                           "3f2a4e8b-9d7c-4a0f-8e5d-4c3b2a1908f7", patient=patient)
        stored = Message.objects.get(pk=msg.pk)
        self.assertNotIn("s3cret", stored.user_message)
        self.assertIn("ada", stored.user_message)

    def test_redaction_does_not_depend_on_which_agent_was_addressed(self):
        # A passcode typed at the wrong agent is still a passcode.
        msg = save_message("+440000000000", "/login ada s3cret", "Hello.",
                           "3f2a4e8b-9d7c-4a0f-8e5d-4c3b2a1908f8")
        self.assertNotIn("s3cret", Message.objects.get(pk=msg.pk).user_message)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class TheCall(SenseiTestCase):
    def setUp(self):
        super().setUp()
        _configure()
        self.patient = Patient.objects.create(name="Ada", lastname="Lovelace")

    def test_a_chat_turn_is_sent_as_a_message_operation(self):
        with patch("ConvAI.sensei.requests.post",
                   return_value=_FakeResponse({"status": "ok", "response": "8,431 steps."})) as post:
            reply = sensei.send(self.patient, "How many steps yesterday?", "thread-1")

        self.assertEqual(reply, "8,431 steps.")
        body = post.call_args.kwargs["data"]
        self.assertIn('"operation": "message"', body)
        self.assertIn('"conversation_id": "thread-1"', body)
        self.assertEqual(post.call_args.kwargs["headers"]["x-functions-key"], "test-key")

    def test_a_login_turn_is_sent_as_a_login_operation(self):
        with patch("ConvAI.sensei.requests.post",
                   return_value=_FakeResponse({"status": "ok", "response": "Signed in."})) as post:
            sensei.send(self.patient, "/login ada 1234", "thread-1")

        body = post.call_args.kwargs["data"]
        self.assertIn('"operation": "login"', body)
        # The passcode goes to Sensei — that is the whole point of the call —
        # and nowhere else.
        self.assertIn('"passcode": "1234"', body)
        self.assertNotIn('"message"', body)

    def test_sensei_s_own_refusal_is_relayed_verbatim(self):
        # The 401 body is the instruction that tells the caregiver what to do.
        refusal = "Please use /login <AppUserId> <Passcode> first."
        with patch("ConvAI.sensei.requests.post",
                   return_value=_FakeResponse({"status": "error", "response": refusal}, 401)):
            self.assertEqual(sensei.send(self.patient, "hello", "t"), refusal)

    def test_a_malformed_command_is_answered_with_help_not_forwarded(self):
        # Forwarding it would put the passcode into Sensei as free-text chat.
        with patch("ConvAI.sensei.requests.post") as post:
            reply = sensei.send(self.patient, "/login ada", "t")
        post.assert_not_called()
        self.assertIn("/login <AppUserId> <Passcode>", reply)

    def test_a_disabled_installation_never_calls_out(self):
        _configure(enabled="0")
        with patch("ConvAI.sensei.requests.post") as post:
            reply = sensei.send(self.patient, "hello", "t")
        post.assert_not_called()
        self.assertIn("not configured", reply)

    def test_an_unreachable_sensei_answers_rather_than_raising(self):
        import requests
        with patch("ConvAI.sensei.requests.post", side_effect=requests.ConnectionError):
            self.assertIn("could not reach", sensei.send(self.patient, "hi", "t").lower())


class DispatchingToTheAdapter(SenseiTestCase):
    """A Sensei agent routes through the adapter, not the LangGraph client."""

    def setUp(self):
        super().setUp()
        _configure()
        self.patient = Patient.objects.create(name="Ada", lastname="Lovelace")
        self.agent = Agent.objects.create(name="Sensei", kind=Agent.Kind.SENSEI)

    def test_the_turn_goes_to_sensei(self):
        from ConvAI.utils import generate_response_with_agent
        with patch("ConvAI.sensei.send", return_value="8,431 steps.") as send:
            reply = generate_response_with_agent(self.agent, self.patient, "steps?", "t")
        send.assert_called_once()
        self.assertEqual(reply, "8,431 steps.")

    def test_an_existing_agent_stops_answering_when_the_flag_goes_off(self):
        _configure(enabled="0")
        from ConvAI.utils import generate_response_with_agent
        with patch("ConvAI.sensei.send") as send:
            reply = generate_response_with_agent(self.agent, self.patient, "steps?", "t")
        send.assert_not_called()
        self.assertIn("not enabled", reply)


class TheAdminSurface(SenseiTestCase):
    def setUp(self):
        super().setUp()
        self.admin = ConvAIUser.objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def test_the_agents_page_hides_sensei_on_an_installation_that_does_not_use_it(self):
        response = self.client.get(reverse("agents"))
        self.assertNotContains(response, 'data-agent-tab="sensei"')

    def test_turning_it_on_offers_the_new_kind(self):
        _configure()
        response = self.client.get(reverse("agents"))
        self.assertContains(response, 'data-agent-tab="sensei"')

    def test_a_sensei_agent_cannot_be_created_while_the_flag_is_off(self):
        response = self.client.post(reverse("agent_create", args=["sensei"]),
                                    {"name": "Sensei"}, follow=True)
        self.assertFalse(Agent.objects.filter(kind=Agent.Kind.SENSEI).exists())
        self.assertContains(response, "not enabled")

    def test_one_can_be_created_once_it_is_on(self):
        _configure()
        self.client.post(reverse("agent_create", args=["sensei"]),
                         {"name": "Sensei", "description": "Sensor questions."})
        agent = Agent.objects.get(name="Sensei")
        self.assertEqual(agent.kind, Agent.Kind.SENSEI)

    def test_an_agent_created_earlier_stays_editable_after_the_flag_goes_off(self):
        # Otherwise turning Sensei off and on again costs the admin their work.
        _configure()
        agent = Agent.objects.create(name="Sensei", kind=Agent.Kind.SENSEI)
        _configure(enabled="0")
        self.assertEqual(self.client.get(reverse("agent_edit", args=[agent.pk])).status_code, 200)

    def test_the_settings_form_generates_an_id_secret_rather_than_asking_for_one(self):
        from ConvAI.forms import SenseiConfigForm
        form = SenseiConfigForm(
            {"sensei_enabled": "1",
             "sensei_api_url": "https://sensei.example/api/send_message",
             "sensei_function_key": "k",
             "sensei_user_id_secret": ""},
            instance=SiteConfiguration.load())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data["sensei_user_id_secret"])
