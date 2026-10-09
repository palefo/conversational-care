"""Per-run tokens: what a remote agent may do, and only that.

*One conversation.* The token names it; the endpoints take no id.

*Only the tools' actions.* Accepted by ``/api/v1/run/`` and refused everywhere
else — including the per-conversation endpoints people use.

*Short-lived, and unforgeable* without the secret key.

    python3 manage.py test ConvAI.test_run_tokens --settings=test_settings
"""
import time

from django.contrib.auth.models import Permission
from django.core import signing
from django.core.cache import cache
from django.test import TestCase
from rest_framework.authtoken.models import Token

from ConvAI import run_tokens
from ConvAI.models import Agent, Conversation, ConvAIUser, Patient, SiteConfiguration


class RunTokenTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        cfg = SiteConfiguration.load()
        cfg.conversation_privacy_enabled = "0"
        cfg.save()
        cache.clear()
        self.agent = Agent.objects.create(name="RECO B", kind="remote", langgraph_name="reco_b",
                                          host="localhost", port=8123, allow_callbacks=True)
        self.ada = Patient.objects.create(name="Ada", lastname="Lovelace")
        self.bob = Patient.objects.create(name="Bob", lastname="Other")
        self.conv = Conversation.objects.create(patient=self.ada, agent=self.agent,
                                                summary="The classifier's words.")
        self.other = Conversation.objects.create(patient=self.bob, agent=self.agent)

    def token(self, conv=None, **kw):
        conv = conv or self.conv
        kw.setdefault("agent_id", self.agent.pk)
        kw.setdefault("patient_id", conv.patient_id)
        return run_tokens.mint(conv.id, **kw)

    def call(self, path="", method="get", body=None, token=None, keyword="RunToken"):
        token = token if token is not None else self.token()
        headers = {"HTTP_AUTHORIZATION": f"{keyword} {token}"}
        url = f"/api/v1/run/{path}"
        if method == "post":
            return self.client.post(url, data=body or {}, content_type="application/json",
                                    **headers)
        return self.client.get(url, **headers)

    def privacy(self, on):
        cfg = SiteConfiguration.load()
        cfg.conversation_privacy_enabled = "1" if on else "0"
        cfg.save()
        cache.clear()


class TheToken(RunTokenTestCase):
    def test_it_round_trips(self):
        claims = run_tokens.verify(self.token())
        self.assertEqual(claims.conversation_id, str(self.conv.id))
        self.assertEqual(claims.patient_id, self.ada.pk)
        self.assertEqual(claims.agent_id, self.agent.pk)
        self.assertEqual(set(claims.scopes), set(run_tokens.SCOPES))

    def test_an_altered_token_is_refused(self):
        token = self.token()
        self.assertIsNone(run_tokens.verify(token[:-2] + ("AA" if token[-2:] != "AA" else "BB")))

    def test_an_expired_token_is_refused(self):
        token = self.token(ttl=60, now=time.time() - 120)
        self.assertIsNone(run_tokens.verify(token))

    def test_a_value_signed_for_something_else_is_refused(self):
        forged = signing.dumps({"c": str(self.other.id), "e": int(time.time()) + 999})
        self.assertIsNone(run_tokens.verify(forged))

    def test_unknown_scopes_are_dropped(self):
        claims = run_tokens.verify(self.token(scopes=("summary", "patients:read")))
        self.assertEqual(claims.scopes, ("summary",))


class TheRunEndpoints(RunTokenTestCase):
    def test_get_describes_its_own_conversation(self):
        body = self.call().json()
        self.assertEqual(body["conversation_id"], str(self.conv.id))
        self.assertEqual(body["summary"], "The classifier's words.")
        self.assertEqual(body["source"], "classifier")

    def test_it_reports_a_summary(self):
        response = self.call("summary/", "post", {"summary": "What they wanted."})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "agent")
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.agent_summary, "What they wanted.")

    def test_it_can_only_ever_touch_its_own_conversation(self):
        """No id to change: the token for Ada's conversation writes Ada's."""
        self.call("summary/", "post", {"summary": "Ada's."})
        self.other.refresh_from_db()
        self.assertEqual(self.other.agent_summary, "")

    def test_a_blank_summary_is_refused(self):
        self.assertEqual(self.call("summary/", "post", {"summary": " "}).status_code, 400)

    def test_visibility_is_404_while_privacy_is_off(self):
        self.assertEqual(self.call("visibility/", "post", {"hidden": True}).status_code, 404)

    def test_visibility_works_while_privacy_is_on(self):
        self.privacy(True)
        response = self.call("visibility/", "post", {"hidden": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["hidden"])

    def test_a_scope_it_was_not_given_is_404(self):
        token = self.token(scopes=("visibility",))
        self.assertEqual(self.call("summary/", "post", {"summary": "x"}, token=token).status_code,
                         404)

    def test_a_missing_conversation_is_404(self):
        token = run_tokens.mint(Conversation().id, agent_id=self.agent.pk)
        self.assertEqual(self.call(token=token).status_code, 404)

    def test_a_conversation_on_another_clients_file_is_404(self):
        token = self.token(patient_id=self.bob.pk)
        self.assertEqual(self.call(token=token).status_code, 404)

    def test_an_expired_token_is_401(self):
        self.assertEqual(self.call(token=self.token(ttl=1, now=time.time() - 10)).status_code, 401)

    def test_no_token_is_401(self):
        self.assertEqual(self.client.get("/api/v1/run/").status_code, 401)

    def test_a_personal_token_is_not_accepted_here(self):
        admin = ConvAIUser.objects.create_user(username="boss", password="x")
        admin.user_permissions.add(Permission.objects.get(codename="access_configuration"))
        key = Token.objects.create(user=admin).key
        self.assertEqual(self.call(token=key, keyword="Token").status_code, 401)


class RefusedEverywhereElse(RunTokenTestCase):
    """The token's reach is the /run/ endpoints. Nothing else lists its auth class."""

    def elsewhere(self, url):
        return self.client.get(url, HTTP_AUTHORIZATION=f"RunToken {self.token()}")

    def test_the_client_list(self):
        self.assertEqual(self.elsewhere("/api/v1/patients/").status_code, 401)

    def test_the_alerts(self):
        self.assertEqual(self.elsewhere("/api/v1/alerts/").status_code, 401)

    def test_the_per_conversation_endpoints_people_use(self):
        self.assertEqual(self.elsewhere(f"/api/v1/conversations/{self.conv.id}/summary/").status_code,
                         401)

    def test_the_principal_is_nobody(self):
        from ConvAI.roles import is_admin
        principal = run_tokens._RunPrincipal(run_tokens.verify(self.token()))
        self.assertFalse(is_admin(principal))
        self.assertIsNone(principal.pk)
