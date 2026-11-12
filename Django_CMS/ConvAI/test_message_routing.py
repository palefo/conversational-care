"""One Twilio number shared by several installations: the routing hub.

Twilio calls one webhook per number. The installation it calls — the hub —
asks itself and every linked installation whether a message is theirs, and
forwards it, re-signed, to the first that says so. See shared_numbers.md and
ConvAI.message_routing.

    python3 manage.py test ConvAI.test_message_routing --settings=test_settings
"""
import os
from datetime import timedelta
from unittest.mock import patch

import requests
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from twilio.request_validator import RequestValidator

from ConvAI import message_routing
from ConvAI.forms import LinkedInstallationForm, RegistrationConfigForm, RoutingConfigForm
from ConvAI.message_attribution import create_message
from ConvAI.models import (Caregiver, ConvAIUser, LinkedInstallation, Message, Patient,
                           SelfRegistration, SiteConfiguration, StaffWhatsAppLink)

CLIENT = "+447700900001"
CAREGIVER = "+447700900002"
NAVIGATOR = "+447700900003"
STRANGER = "+447700900099"
PLATFORM = "+447700900999"
AUTH_TOKEN = "shared-twilio-token"
WEBHOOK_PATH = reverse("whatsapp_webhook")
PEER_URL = "http://testserver" + WEBHOOK_PATH


def _configure(**fields):
    cfg = SiteConfiguration.load()
    for name, value in fields.items():
        setattr(cfg, name, value)
    cfg.save()
    cache.clear()


def _peer(name="Reco", priority=10, **extra):
    fields = {"webhook_url": "https://reco.example.org/impact/whatsapp_webhook",
              "api_token": "peer-token", "enabled": True}
    fields.update(extra)
    return LinkedInstallation.objects.create(name=name, priority=priority, **fields)


class FakeResponse:
    def __init__(self, data=None, status=200, content=b"", content_type="application/json"):
        self._data = data if data is not None else {}
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": content_type}

    def json(self):
        return self._data


def _claiming(answers):
    """A requests.get stand-in answering each peer by name (looked up from the
    token it was sent), or raising when the answer is an exception."""
    by_token = {}
    for peer, answer in answers.items():
        by_token[peer.api_token] = answer

    def get(url, params=None, headers=None, timeout=None):
        token = (headers or {}).get("Authorization", "").replace("Token ", "")
        answer = by_token[token]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, FakeResponse):
            return answer
        return FakeResponse({"claims": bool(answer), "reason": "client" if answer else "unknown number"})
    return get


class Base(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


# ----------------------------------------------------------------------------
# The claim rule
# ----------------------------------------------------------------------------

class TheClaimRule(Base):
    def test_a_clients_own_number_is_claimed(self):
        Patient.objects.create(name="Ada", lastname="Lovelace", phone_number=CLIENT)
        self.assertEqual(message_routing.claims(f"whatsapp:{CLIENT}", "hello"), (True, "client"))

    def test_a_caregivers_number_is_claimed(self):
        cg = Caregiver.objects.create(name="Grace", lastname="Hopper", phone_number=CAREGIVER)
        Patient.objects.create(name="Ada", lastname="Lovelace", caregiver=cg)
        self.assertEqual(message_routing.claims(CAREGIVER, "hello"), (True, "caregiver"))

    def test_a_linked_navigator_phone_is_claimed(self):
        _configure(link_worker_v2_enabled="1", link_worker_whatsapp_enabled="1")
        nav = ConvAIUser.objects.create_user("rosa", password="x", phone_number=NAVIGATOR)
        nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        StaffWhatsAppLink.objects.create(user=nav, phone_number=NAVIGATOR, verified_at=timezone.now())
        self.assertEqual(message_routing.claims(NAVIGATOR, "what do I have today?"),
                         (True, "navigator"))

    def test_a_pending_registration_is_claimed(self):
        SelfRegistration.objects.create(name="New", lastname="Person", phone_number=STRANGER)
        self.assertEqual(message_routing.claims(STRANGER, "any text"), (True, "registration pending"))

    def test_an_approved_registration_alone_is_not(self):
        SelfRegistration.objects.create(name="Old", lastname="Person", phone_number=STRANGER,
                                        state=SelfRegistration.State.APPROVED)
        self.assertEqual(message_routing.claims(STRANGER, "any text"), (False, "unknown number"))

    def test_a_prospect_who_wrote_in_the_last_day_is_claimed(self):
        create_message(conversation_id="c1", user=STRANGER, user_message="hi",
                       sender_role=Message.SenderRole.PROSPECT)
        self.assertEqual(message_routing.claims(STRANGER, "my name is Sam"), (True, "registering"))

    def test_but_not_one_who_wrote_last_week(self):
        msg = create_message(conversation_id="c1", user=STRANGER, user_message="hi",
                             sender_role=Message.SenderRole.PROSPECT)
        Message.objects.filter(pk=msg.pk).update(timestamp=timezone.now() - timedelta(days=3))
        self.assertEqual(message_routing.claims(STRANGER, "my name is Sam"), (False, "unknown number"))

    def test_the_registration_message_is_claimed_while_self_registration_is_on(self):
        _configure(self_registration_enabled="1", self_reg_message="Hi! I want to join RECO")
        ok, reason = message_routing.claims(STRANGER, "hi!   i WANT to join reco, please")
        self.assertEqual((ok, reason), (True, "registration message"))

    def test_another_installations_registration_message_is_not(self):
        _configure(self_registration_enabled="1", self_reg_message="Hi! I want to join RECO")
        self.assertEqual(message_routing.claims(STRANGER, "Hi! I want to register"),
                         (False, "unknown number"))

    def test_the_default_registration_message_counts(self):
        _configure(self_registration_enabled="1", self_reg_message="")
        self.assertEqual(message_routing.claims(STRANGER, "Hi! I want to register"),
                         (True, "registration message"))

    def test_with_self_registration_off_the_message_means_nothing(self):
        _configure(self_registration_enabled="0", self_reg_message="Hi! I want to join RECO")
        self.assertEqual(message_routing.claims(STRANGER, "Hi! I want to join RECO"),
                         (False, "unknown number"))

    def test_no_number_no_claim(self):
        self.assertEqual(message_routing.claims("", "hello"), (False, "no number"))


# ----------------------------------------------------------------------------
# Priority, and asking the peers
# ----------------------------------------------------------------------------

class Deciding(Base):
    def test_the_hub_wins_when_it_ranks_first(self):
        Patient.objects.create(name="Ada", lastname="Lovelace", phone_number=CLIENT)
        _configure(routing_priority=1)
        peer = _peer(priority=10)
        with patch("requests.get", side_effect=_claiming({peer: True})):
            winner = message_routing.decide(CLIENT, "hi", [peer])
        self.assertTrue(winner.is_hub)
        self.assertEqual(winner.reason, "client")

    def test_a_peer_wins_when_it_ranks_first(self):
        Patient.objects.create(name="Ada", lastname="Lovelace", phone_number=CLIENT)
        peer = _peer(priority=10)  # hub stays at its default of 100
        with patch("requests.get", side_effect=_claiming({peer: True})):
            winner = message_routing.decide(CLIENT, "hi", [peer])
        self.assertIs(winner.peer, peer)

    def test_among_peers_the_lowest_number_goes_first(self):
        first = _peer("First", priority=5, api_token="t1")
        second = _peer("Second", priority=20, api_token="t2")
        with patch("requests.get", side_effect=_claiming({first: True, second: True})):
            winner = message_routing.decide(STRANGER, "hi", [second, first])
        self.assertEqual(winner.name, "First")

    def test_on_a_tie_the_peer_beats_the_hub(self):
        Patient.objects.create(name="Ada", lastname="Lovelace", phone_number=CLIENT)
        _configure(routing_priority=10)
        peer = _peer(priority=10)
        with patch("requests.get", side_effect=_claiming({peer: True})):
            winner = message_routing.decide(CLIENT, "hi", [peer])
        self.assertIs(winner.peer, peer)

    def test_nobody_claims(self):
        peer = _peer()
        with patch("requests.get", side_effect=_claiming({peer: False})):
            self.assertIsNone(message_routing.decide(STRANGER, "hi", [peer]))

    def test_a_peer_that_times_out_has_not_claimed_and_says_so_on_its_row(self):
        Patient.objects.create(name="Ada", lastname="Lovelace", phone_number=CLIENT)
        peer = _peer(priority=1)
        with patch("requests.get", side_effect=_claiming({peer: requests.Timeout("2s")})), \
             self.assertLogs("ConvAI.message_routing", level="WARNING"):
            winner = message_routing.decide(CLIENT, "hi", [peer])
        self.assertTrue(winner.is_hub)
        peer.refresh_from_db()
        self.assertIn("timeout", peer.last_error)
        self.assertIsNone(peer.last_contact)

    def test_a_peer_that_errors_has_not_claimed_either(self):
        peer = _peer(priority=1)
        with patch("requests.get", side_effect=_claiming({peer: FakeResponse(status=500)})), \
             self.assertLogs("ConvAI.message_routing", level="WARNING"):
            self.assertIsNone(message_routing.decide(STRANGER, "hi", [peer]))
        peer.refresh_from_db()
        self.assertIn("HTTP 500", peer.last_error)

    def test_a_peer_that_answers_is_marked_contacted(self):
        peer = _peer(last_error="old failure")
        with patch("requests.get", side_effect=_claiming({peer: False})) as get:
            message_routing.decide(STRANGER, "hi", [peer])
        peer.refresh_from_db()
        self.assertIsNotNone(peer.last_contact)
        self.assertEqual(peer.last_error, "")
        call = get.call_args
        self.assertEqual(call.args[0], "https://reco.example.org/api/v1/routing/claim/")
        self.assertEqual(call.kwargs["params"], {"phone": STRANGER, "body": "hi"})
        self.assertEqual(call.kwargs["timeout"], message_routing.CLAIM_TIMEOUT)


# ----------------------------------------------------------------------------
# The webhook
# ----------------------------------------------------------------------------

def _signed(params, url=PEER_URL, token=AUTH_TOKEN):
    return RequestValidator(token).compute_signature(url, params)


@override_settings(ASYNC_WHATSAPP_REPLY=False)
@patch.dict(os.environ, {"TWILIO_AUTH_TOKEN": AUTH_TOKEN})
class TheWebhook(Base):
    """Real signatures throughout: what the hub forwards has to pass the peer's
    own check, so nothing here stubs the validator."""

    def setUp(self):
        super().setUp()
        self.params = {"From": f"whatsapp:{STRANGER}", "To": f"whatsapp:{PLATFORM}",
                       "Body": "hello", "NumMedia": "0"}

    def post(self, params=None, **headers):
        params = params or self.params
        return self.client.post(WEBHOOK_PATH, params,
                                HTTP_X_TWILIO_SIGNATURE=_signed(params), **headers)

    def test_without_linked_installations_nothing_changes(self):
        with patch("requests.get") as get, \
             patch("ConvAI.views.chat.process_received_message", return_value="hi") as local:
            resp = self.post()
        self.assertEqual(resp.status_code, 200)
        get.assert_not_called()
        local.assert_called_once()

    def test_a_disabled_installation_is_not_asked(self):
        _peer(enabled=False)
        with patch("requests.get") as get, \
             patch("ConvAI.views.chat.process_received_message", return_value="hi") as local:
            self.post()
        get.assert_not_called()
        local.assert_called_once()

    def test_a_routed_message_is_never_routed_again(self):
        peer = _peer()
        with patch("requests.get", side_effect=_claiming({peer: True})) as get, \
             patch("ConvAI.views.chat.process_received_message", return_value="hi") as local:
            resp = self.post(HTTP_X_CC_ROUTED="1")
        self.assertEqual(resp.status_code, 200)
        get.assert_not_called()
        local.assert_called_once()

    def test_the_hub_handles_what_it_wins(self):
        Patient.objects.create(name="Ada", lastname="Lovelace", phone_number=STRANGER)
        _configure(routing_priority=1)
        peer = _peer(priority=10)
        with patch("requests.get", side_effect=_claiming({peer: True})), \
             patch("requests.post") as post, \
             patch("ConvAI.views.chat.process_received_message", return_value="hi") as local:
            self.post()
        post.assert_not_called()
        local.assert_called_once()

    def test_what_nobody_claims_is_handled_here(self):
        peer = _peer()
        with patch("requests.get", side_effect=_claiming({peer: False})), \
             patch("requests.post") as post, \
             patch("ConvAI.views.chat.process_received_message", return_value="hi") as local:
            self.post()
        post.assert_not_called()
        local.assert_called_once()

    def test_a_peers_message_is_forwarded_and_its_answer_relayed(self):
        peer = _peer()
        twiml = b"<Response><Message>ola</Message></Response>"
        with patch("requests.get", side_effect=_claiming({peer: True})), \
             patch("requests.post", return_value=FakeResponse(
                 status=200, content=twiml, content_type="text/xml")) as post, \
             patch("ConvAI.views.chat.process_received_message") as local, \
             self.assertLogs("ConvAI.message_routing", level="INFO") as logs:
            resp = self.post()
        local.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, twiml)
        self.assertEqual(resp["Content-Type"], "text/xml")
        call = post.call_args
        self.assertEqual(call.args[0], peer.webhook_url)
        self.assertEqual(call.kwargs["data"], self.params)
        self.assertEqual(call.kwargs["headers"]["X-CC-Routed"], "1")
        self.assertEqual(call.kwargs["headers"]["X-Twilio-Signature"],
                         _signed(self.params, url=peer.webhook_url))
        self.assertEqual(call.kwargs["timeout"], message_routing.FORWARD_TIMEOUT)
        self.assertTrue(any("Routed message" in line and "Reco" in line and "client" in line
                            for line in logs.output))
        peer.refresh_from_db()
        self.assertIsNotNone(peer.last_contact)

    def test_the_forward_passes_the_peers_own_signature_check(self):
        """The peer is this same installation, reached through its webhook:
        the forwarded request must be accepted as if Twilio had sent it."""
        peer = _peer(webhook_url=PEER_URL)
        outer = self

        def deliver(url, data=None, headers=None, timeout=None):
            resp = outer.client.post(WEBHOOK_PATH, data,
                                     HTTP_X_TWILIO_SIGNATURE=headers["X-Twilio-Signature"],
                                     HTTP_X_CC_ROUTED=headers["X-CC-Routed"])
            return FakeResponse(status=resp.status_code, content=resp.content,
                                content_type=resp["Content-Type"])

        with patch("requests.get", side_effect=_claiming({peer: True})) as get, \
             patch("requests.post", side_effect=deliver), \
             patch("ConvAI.views.chat.process_received_message", return_value="hi there") as local:
            resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"hi there", resp.content)
        # Processed exactly once — on the peer's side — and the peer, seeing
        # X-CC-Routed, asked nobody.
        local.assert_called_once()
        self.assertEqual(get.call_count, 1)

    def test_a_forward_is_retried_once_on_a_connection_failure(self):
        peer = _peer()
        answers = [requests.ConnectionError("refused"),
                   FakeResponse(status=200, content=b"<Response></Response>", content_type="text/xml")]
        with patch("requests.get", side_effect=_claiming({peer: True})), \
             patch("requests.post", side_effect=answers) as post, \
             patch("ConvAI.views.chat.process_received_message") as local, \
             self.assertLogs("ConvAI.message_routing", level="WARNING"):
            resp = self.post()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(resp.status_code, 200)
        local.assert_not_called()

    def test_when_the_forward_fails_twice_twilio_gets_empty_twiml_and_nothing_runs_here(self):
        peer = _peer()
        with patch("requests.get", side_effect=_claiming({peer: True})), \
             patch("requests.post", side_effect=requests.ConnectionError("refused")) as post, \
             patch("ConvAI.views.chat.process_received_message") as local, \
             self.assertLogs("ConvAI.message_routing", level="ERROR") as logs:
            resp = self.post()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"<Response></Response>")
        self.assertEqual(resp["Content-Type"], "text/xml")
        local.assert_not_called()
        self.assertTrue(any("could not be forwarded" in line for line in logs.output))
        peer.refresh_from_db()
        self.assertIn("ConnectionError", peer.last_error)

    @override_settings(ASYNC_WHATSAPP_REPLY=True)
    def test_routing_happens_before_the_async_branch_too(self):
        peer = _peer()
        with patch("requests.get", side_effect=_claiming({peer: True})), \
             patch("requests.post", return_value=FakeResponse(
                 status=200, content=b"<Response></Response>", content_type="text/xml")), \
             patch("ConvAI.async_reply.submit") as submit:
            self.post()
        submit.assert_not_called()


# ----------------------------------------------------------------------------
# The claim API
# ----------------------------------------------------------------------------

class TheClaimApi(Base):
    def setUp(self):
        super().setUp()
        user = ConvAIUser.objects.create_user("hub", password="x")
        self.auth = {"HTTP_AUTHORIZATION": f"Token {Token.objects.create(user=user).key}"}
        self.url = reverse("api_routing_claim")

    def test_answers_the_claim_rule(self):
        Patient.objects.create(name="Ada", lastname="Lovelace", phone_number=CLIENT)
        resp = self.client.get(self.url, {"phone": CLIENT, "body": "hi"}, **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual({k: resp.json()[k] for k in ("claims", "reason")},
                         {"claims": True, "reason": "client"})
        resp = self.client.get(self.url, {"phone": STRANGER, "body": "hi"}, **self.auth)
        self.assertEqual({k: resp.json()[k] for k in ("claims", "reason")},
                         {"claims": False, "reason": "unknown number"})

    def test_needs_a_token(self):
        resp = self.client.get(self.url, {"phone": CLIENT, "body": "hi"})
        self.assertIn(resp.status_code, (401, 403))

    def test_needs_a_number(self):
        resp = self.client.get(self.url, {"body": "hi"}, **self.auth)
        self.assertEqual(resp.status_code, 400)


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

class TheSettings(Base):
    def setUp(self):
        super().setUp()
        self.admin = ConvAIUser.objects.create_user(
            "admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.back = reverse("config") + "?tab=messaging#messaging"

    def test_the_card_is_on_the_messaging_tab(self):
        peer = _peer(last_error="HTTP 500")
        resp = self.client.get(reverse("config"))
        self.assertContains(resp, 'id="linked-installations"')
        self.assertContains(resp, peer.webhook_url)
        self.assertContains(resp, "HTTP 500")
        self.assertContains(resp, 'name="routing_priority"')

    def test_adding_one(self):
        resp = self.client.post(reverse("linked_installation_add"), {
            "new-name": "Reco", "new-webhook_url": "https://reco.example.org/impact/whatsapp_webhook",
            "new-api_token": "secret", "new-priority": "10", "new-enabled": "on"})
        self.assertRedirects(resp, self.back, fetch_redirect_response=False)
        inst = LinkedInstallation.objects.get()
        self.assertEqual((inst.name, inst.api_token, inst.priority, inst.enabled),
                         ("Reco", "secret", 10, True))
        self.assertEqual(inst.claim_url, "https://reco.example.org/api/v1/routing/claim/")

    def test_a_new_one_needs_a_token_and_a_full_url(self):
        form = LinkedInstallationForm({"name": "Reco", "webhook_url": "not a url",
                                       "priority": "10"})
        self.assertFalse(form.is_valid())
        self.assertIn("api_token", form.errors)
        self.assertIn("webhook_url", form.errors)
        # Posted, the page comes back with the add form open and the errors in it.
        resp = self.client.post(reverse("linked_installation_add"), {
            "new-name": "Reco", "new-webhook_url": "not a url", "new-priority": "10"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '<details id="li-add" class="border border-gray-200 rounded-xl p-4 mb-6" open>')
        self.assertContains(resp, "This field is required.")
        self.assertEqual(LinkedInstallation.objects.count(), 0)

    def test_editing_with_a_blank_token_keeps_the_stored_one(self):
        inst = _peer(api_token="keep-me")
        resp = self.client.post(reverse("linked_installation_save", args=[inst.pk]), {
            f"li{inst.pk}-name": "Reco (renamed)", f"li{inst.pk}-webhook_url": inst.webhook_url,
            f"li{inst.pk}-api_token": "", f"li{inst.pk}-priority": "7"})
        self.assertRedirects(resp, self.back, fetch_redirect_response=False)
        inst.refresh_from_db()
        self.assertEqual((inst.name, inst.api_token, inst.priority, inst.enabled),
                         ("Reco (renamed)", "keep-me", 7, False))

    def test_the_token_is_never_rendered(self):
        _peer(api_token="top-secret")
        resp = self.client.get(reverse("config"))
        self.assertNotContains(resp, "top-secret")

    def test_removing_one(self):
        inst = _peer()
        resp = self.client.post(reverse("linked_installation_delete", args=[inst.pk]))
        self.assertRedirects(resp, self.back, fetch_redirect_response=False)
        self.assertFalse(LinkedInstallation.objects.exists())

    def test_the_connection_test_records_the_outcome(self):
        inst = _peer()
        with patch("requests.get", side_effect=_claiming({inst: False})) as get:
            self.client.post(reverse("linked_installation_test", args=[inst.pk]))
        inst.refresh_from_db()
        self.assertIsNotNone(inst.last_contact)
        self.assertEqual(inst.last_error, "")
        self.assertEqual(get.call_args.kwargs["params"]["phone"], message_routing.TEST_PHONE)
        with patch("requests.get", side_effect=_claiming({inst: requests.ConnectionError("refused")})):
            resp = self.client.post(reverse("linked_installation_test", args=[inst.pk]), follow=True)
        inst.refresh_from_db()
        self.assertIn("ConnectionError", inst.last_error)
        self.assertContains(resp, "could not be reached")

    def test_the_hubs_priority_is_a_setting(self):
        resp = self.client.post(reverse("config_save"), {"section": "routing", "routing_priority": "3"})
        self.assertRedirects(resp, reverse("config") + "?tab=messaging#routing",
                             fetch_redirect_response=False)
        self.assertEqual(SiteConfiguration.load().routing_priority, 3)
        self.assertTrue(RoutingConfigForm({"routing_priority": "0"}).is_valid())
        self.assertFalse(RoutingConfigForm({"routing_priority": "-1"}).is_valid())

    def test_navigators_cannot_manage_them(self):
        nav = ConvAIUser.objects.create_user("rosa", password="x")
        nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.client.force_login(nav)
        resp = self.client.post(reverse("linked_installation_add"), {
            "new-name": "Reco", "new-webhook_url": "https://reco.example.org/hook",
            "new-api_token": "secret", "new-priority": "10"})
        self.assertNotEqual(resp.status_code, 200)
        self.assertFalse(LinkedInstallation.objects.exists())


class TheRegistrationMessage(Base):
    def test_is_a_setting_with_the_old_words_as_default(self):
        from ConvAI import registration
        self.assertEqual(registration.registration_message(), "Hi! I want to register")
        _configure(self_reg_message="  Hi! I want to join RECO ")
        self.assertEqual(registration.registration_message(), "Hi! I want to join RECO")

    def test_falls_back_to_the_environment(self):
        from ConvAI import registration
        with patch.dict(os.environ, {"SELF_REG_MESSAGE": "Hola, quiero registrarme"}):
            self.assertEqual(registration.registration_message(), "Hola, quiero registrarme")

    def test_the_qr_codes_and_links_carry_it(self):
        from ConvAI.views.settings_views import _selfreg_qr_context
        _configure(platform_phone="+14155550100", self_reg_message="Hi! I want to join RECO")
        with patch("ConvAI.views.settings_views._qr_svg", return_value="<svg/>") as qr:
            ctx = _selfreg_qr_context(None)
        self.assertEqual(ctx["message"], "Hi! I want to join RECO")
        self.assertIn("text=Hi%21%20I%20want%20to%20join%20RECO", ctx["whatsapp"]["url"])
        self.assertIn("body=Hi%21%20I%20want%20to%20join%20RECO", ctx["sms"]["url"])
        self.assertIn("SMSTO:+14155550100:Hi! I want to join RECO", [c.args[0] for c in qr.call_args_list])

    def test_is_edited_under_registrations(self):
        form = RegistrationConfigForm(instance=SiteConfiguration.load())
        self.assertIn("self_reg_message", form.fields)
        self.assertEqual(form.fields["self_reg_message"].widget.attrs["placeholder"],
                         "Hi! I want to register")
        admin = ConvAIUser.objects.create_user("admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        resp = self.client.post(reverse("config_save"), {
            "section": "registrations", "self_reg_message": "Hi! I want to join RECO",
            "self_reg_auto_approve": "0"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SiteConfiguration.load().self_reg_message, "Hi! I want to join RECO")


# ----------------------------------------------------------------------------
# Alarms: every misconfiguration that can be detected is shown
# ----------------------------------------------------------------------------

class Alarms(Base):
    def setUp(self):
        super().setUp()
        self.admin = ConvAIUser.objects.create_user(
            "admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def page(self):
        cache.clear()
        return self.client.get(reverse("config"))

    def test_mask_keeps_numbers_out_of_the_log(self):
        self.assertEqual(message_routing.mask(f"whatsapp:{CLIENT}"), "+44…0001")
        self.assertEqual(message_routing.mask(""), "…")

    # --- the claim endpoint tells the hub about itself ---

    @override_settings(ASYNC_WHATSAPP_REPLY=True)
    def test_the_claim_endpoint_reports_async_and_the_registration_message(self):
        _configure(self_registration_enabled="1", self_reg_message="Hi! I want to join RECO")
        token = Token.objects.create(user=self.admin).key
        resp = self.client.get(reverse("api_routing_claim"), {"phone": STRANGER, "body": "x"},
                               HTTP_AUTHORIZATION=f"Token {token}")
        self.assertEqual(resp.json(), {
            "claims": False, "reason": "unknown number", "async_replies": True,
            "self_registration": True, "registration_message": "Hi! I want to join RECO"})
        # ... and writes down that a hub asked, at most hourly.
        first = SiteConfiguration.objects.get(pk=1).routing_claimed_at
        self.assertIsNotNone(first)
        self.client.get(reverse("api_routing_claim"), {"phone": STRANGER},
                        HTTP_AUTHORIZATION=f"Token {token}")
        self.assertEqual(SiteConfiguration.objects.get(pk=1).routing_claimed_at, first)

    def test_with_self_registration_off_no_message_is_reported(self):
        _configure(self_registration_enabled="0", self_reg_message="Hi! I want to join RECO")
        payload = message_routing.claim_payload(STRANGER, "x")
        self.assertEqual((payload["self_registration"], payload["registration_message"]), (False, ""))

    # --- Test connection: one message per way of failing ---

    def _test(self, answer):
        inst = _peer()
        with patch("requests.get", side_effect=_claiming({inst: answer})):
            resp = self.client.post(reverse("linked_installation_test", args=[inst.pk]), follow=True)
        inst.refresh_from_db()
        return inst, resp

    def test_a_wrong_token(self):
        inst, resp = self._test(FakeResponse(status=401))
        self.assertContains(resp, "rejected the API token (HTTP 401)")
        self.assertIn("rejected the API token", inst.last_error)

    def test_an_older_release_without_the_endpoint(self):
        inst, resp = self._test(FakeResponse(status=404))
        self.assertContains(resp, "has no claim endpoint (HTTP 404)")
        self.assertIn("older release", inst.last_error)

    def test_an_unreachable_url(self):
        inst, resp = self._test(requests.ConnectionError("Name or service not known"))
        self.assertContains(resp, "could not be reached (ConnectionError)")
        # urllib3's wrapping is cut down to the operating system's reason.
        inst, resp = self._test(requests.ConnectionError(
            "HTTPConnectionPool(host='127.0.0.1', port=9): Max retries exceeded with url: /x "
            "(Caused by NewConnectionError('<conn>: Failed to establish a new connection: "
            "[Errno 111] Connection refused'))"))
        self.assertEqual(inst.last_error, "could not be reached (ConnectionError): "
                                          "[Errno 111] Connection refused")

    def test_a_peer_that_is_too_slow(self):
        inst, resp = self._test(requests.Timeout())
        self.assertContains(resp, "did not answer within 2 seconds")

    def test_a_page_that_is_not_the_claim_endpoint(self):
        inst, resp = self._test(FakeResponse(data=["not", "it"]))
        self.assertContains(resp, "not with the claim endpoint")

    def test_a_peer_that_replies_synchronously_is_flagged(self):
        inst, resp = self._test(FakeResponse({"claims": False, "reason": "unknown number",
                                              "async_replies": False}))
        self.assertContains(resp, "answered, but")
        self.assertContains(resp, "Replies synchronously")
        self.assertIs(inst.peer_async, False)
        self.assertEqual(message_routing.peer_status(inst)["level"], "warn")

    # --- the hub's own configuration ---

    @override_settings(ASYNC_WHATSAPP_REPLY=False)
    def test_a_synchronous_hub_is_warned_once_it_has_peers(self):
        self.assertNotContains(self.page(), "data-routing-warning")
        _peer()
        resp = self.page()
        self.assertContains(resp, "data-routing-warning")
        self.assertContains(resp, "ASYNC_WHATSAPP_REPLY is off")
        self.assertContains(resp, 'data-tab-alert="messaging"')

    @override_settings(ASYNC_WHATSAPP_REPLY=True)
    def test_an_asynchronous_hub_is_not(self):
        _peer(last_contact=timezone.now())
        resp = self.page()
        self.assertNotContains(resp, "data-routing-warning")
        self.assertNotContains(resp, 'data-tab-alert="messaging"')

    # --- each peer's state ---

    def test_peer_states(self):
        now = timezone.now()
        healthy = _peer("Healthy", last_contact=now, api_token="a")
        never = _peer("Never", api_token="b")
        stale = _peer("Stale", last_contact=now - timedelta(days=4), api_token="c")
        failing = _peer("Failing", last_contact=now, last_error="HTTP 500", api_token="d")
        off = _peer("Off", enabled=False, api_token="e")
        level = {p.name: message_routing.peer_status(p, now)["level"]
                 for p in (healthy, never, stale, failing, off)}
        self.assertEqual(level, {"Healthy": "ok", "Never": "error", "Stale": "error",
                                 "Failing": "error", "Off": "off"})
        resp = self.page()
        self.assertContains(resp, 'data-li-level="error"', count=3)
        self.assertContains(resp, "Has never answered")
        self.assertContains(resp, "No answer since")

    # --- failures are logged, masked, and counted ---

    @override_settings(ASYNC_WHATSAPP_REPLY=False)
    @patch.dict(os.environ, {"TWILIO_AUTH_TOKEN": AUTH_TOKEN})
    def test_a_message_that_falls_through_on_a_timeout_is_a_warning(self):
        _peer()
        params = {"From": f"whatsapp:{STRANGER}", "Body": "hi", "NumMedia": "0"}
        with patch("requests.get", side_effect=requests.Timeout()), \
             patch("ConvAI.views.chat.process_received_message", return_value="hi") as local, \
             self.assertLogs("ConvAI.message_routing", level="WARNING") as logs:
            self.client.post(WEBHOOK_PATH, params, HTTP_X_TWILIO_SIGNATURE=_signed(params))
        local.assert_called_once()
        joined = "\n".join(logs.output)
        self.assertIn("handled here without an answer from Reco", joined)
        self.assertIn("+44…0099", joined)
        self.assertNotIn(STRANGER, joined)

    def test_failed_forwards_are_counted_and_shown(self):
        peer = _peer(last_contact=timezone.now())
        params = {"From": f"whatsapp:{STRANGER}", "Body": "hi"}
        with patch("requests.post", side_effect=requests.ConnectionError("refused")), \
             self.assertLogs("ConvAI.message_routing", level="ERROR"):
            message_routing.forward(peer, params)
            message_routing.forward(peer, params)
        peer.refresh_from_db()
        self.assertEqual(peer.forward_failures, 2)
        resp = self.page()
        self.assertContains(resp, '<span class="font-semibold text-gray-800" data-forward-failures>2</span>',
                            html=False)

    def test_a_peer_refusing_the_signature_is_a_counted_failure(self):
        peer = _peer(last_contact=timezone.now())
        with patch("requests.post", return_value=FakeResponse(status=403, content=b"Invalid signature",
                                                              content_type="text/html")), \
             self.assertLogs("ConvAI.message_routing", level="ERROR"):
            resp = message_routing.forward(peer, {"From": STRANGER, "Body": "hi"})
        self.assertEqual(resp.status_code, 403)
        peer.refresh_from_db()
        self.assertEqual(peer.forward_failures, 1)
        self.assertIn("auth token or webhook URL", peer.last_error)

    @patch.dict(os.environ, {"TWILIO_AUTH_TOKEN": AUTH_TOKEN})
    def test_the_peer_logs_a_forward_it_refuses(self):
        params = {"From": f"whatsapp:{STRANGER}", "Body": "hi"}
        bad = _signed(params, token="the-other-installations-token")
        with patch("ConvAI.views.chat.process_received_message") as local, \
             self.assertLogs("ConvAI.views.chat", level="WARNING") as logs:
            resp = self.client.post(WEBHOOK_PATH, params, HTTP_X_TWILIO_SIGNATURE=bad,
                                    HTTP_X_CC_ROUTED="1")
        self.assertEqual(resp.status_code, 403)
        local.assert_not_called()
        self.assertIn("forwarded by a hub", logs.output[0])
        # An ordinary bad request (not forwarded) is refused without that warning.
        with self.assertNoLogs("ConvAI.views.chat", level="WARNING"):
            self.client.post(WEBHOOK_PATH, params, HTTP_X_TWILIO_SIGNATURE=bad)

    # --- registration messages that cannot be told apart ---

    def test_the_same_message_on_hub_and_peer_is_a_collision(self):
        _configure(self_registration_enabled="1", self_reg_message="")
        peer = _peer(last_contact=timezone.now(), peer_registration_message="hi!  I want to REGISTER")
        self.assertEqual(message_routing.registration_collisions(), [("This installation", "Reco")])
        self.assertEqual(message_routing.peer_status(peer)["level"], "error")
        resp = self.page()
        # Once on the Linked installations card, once under Registrations,
        # and on the peer's own row.
        self.assertContains(resp, "wait for the same registration message", count=2)
        self.assertContains(resp, "Waits for the same registration message as This installation")
        self.assertContains(resp, 'data-tab-alert="registrations"')

    def test_one_message_starting_with_the_other_is_a_collision(self):
        _configure(self_registration_enabled="1", self_reg_message="Hi! I want to")
        _peer(peer_registration_message="Hi! I want to join RECO")
        self.assertEqual(len(message_routing.registration_collisions()), 1)

    def test_distinct_messages_are_fine(self):
        _configure(self_registration_enabled="1", self_reg_message="Hi! I want to register here")
        _peer(peer_registration_message="Hi! I want to join RECO")
        a = _peer("Third", api_token="t3", peer_registration_message="Quiero unirme")
        self.assertEqual(message_routing.registration_collisions(), [])
        a.enabled = False
        a.save()

    def test_peers_colliding_with_each_other_are_caught_too(self):
        _configure(self_registration_enabled="0")
        _peer("A", api_token="ta", peer_registration_message="Join")
        _peer("B", api_token="tb", peer_registration_message="join")
        self.assertEqual(message_routing.registration_collisions(), [("A", "B")])

    def test_a_hub_with_the_default_message_is_warned(self):
        _configure(self_registration_enabled="1", self_reg_message="")
        self.assertEqual(message_routing.registration_warnings(), [])
        _peer()
        self.assertTrue(any("default registration message" in w
                            for w in message_routing.registration_warnings()))
        _configure(self_reg_message="Hi! I want to join the platform")
        self.assertEqual(message_routing.registration_warnings(), [])

    def test_a_peer_with_the_default_message_is_warned_once_a_hub_has_asked(self):
        _configure(self_reg_message="")
        self.assertEqual(message_routing.registration_warnings(), [])
        SiteConfiguration.objects.filter(pk=1).update(routing_claimed_at=timezone.now())
        warnings = message_routing.registration_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("A hub asked this installation", warnings[0])
        self.assertContains(self.page(), "data-registration-warning")
        # A link long gone no longer counts.
        SiteConfiguration.objects.filter(pk=1).update(
            routing_claimed_at=timezone.now() - timedelta(days=60))
        self.assertEqual(message_routing.registration_warnings(), [])
