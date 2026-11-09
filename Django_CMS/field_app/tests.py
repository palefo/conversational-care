"""Field App integration. Field App itself is never called: its HTTP is mocked.

    python3 manage.py test field_app --settings=test_settings
"""
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import TestCase

from ConvAI import extensions
from ConvAI.models import Agent, Conversation, ConvAIUser, Patient
from ConvAI.roles import NAVIGATOR

from .models import AgentDataSource, FieldAppConnection, FieldAppInstallation, PatientFieldAppLink

RECORD = "# Field App record: Ada Demo\n\n## Home visit check-in\n- How are you feeling today? — **Okay**\n"


def _resp(status=200, json=None, text=""):
    r = MagicMock()
    r.ok = status < 400
    r.status_code = status
    r.json.return_value = json if json is not None else {}
    r.text = text
    return r


def _navigator(username="nav"):
    user = ConvAIUser.objects.create_user(username, password="pw")
    user.groups.add(Group.objects.get_or_create(name=NAVIGATOR)[0])
    return user


def _admin(username="adm"):
    user = ConvAIUser.objects.create_user(username, password="pw", is_staff=True, is_superuser=True)
    user.user_permissions.add(Permission.objects.get(codename="access_configuration"))
    return user


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.inst = FieldAppInstallation.objects.create(
            name="Field App", base_url="http://localhost:8003",
            api_url="http://host.docker.internal:8003", client_id="app_x", client_secret="fas_y")
        self.nav = _navigator()
        self.agent = Agent.objects.create(name="Coach", kind="prompt", system_prompt="Be kind.")
        self.ada = Patient.objects.create(name="Ada", lastname="Demo", navigator=self.nav, agent=self.agent)
        self.other = Patient.objects.create(name="Bob", lastname="Other", agent=self.agent)


class SettingsTabTests(_Base):
    def test_admin_adds_an_installation(self):
        self.client.force_login(_admin())
        page = self.client.get("/app/config?tab=thirdparty")
        self.assertContains(page, "http://testserver/field-app/callback/")
        self.client.post("/app/config/save", {
            "section": "thirdparty", "name": "Second", "base_url": "https://field.example.org",
            "api_url": "", "client_id": "app_2", "client_secret": "s"})
        self.assertTrue(FieldAppInstallation.objects.filter(name="Second").exists())


class ConnectTests(_Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.nav)

    def start(self):
        res = self.client.post(f"/field-app/{self.inst.pk}/connect/")
        url = urlsplit(res["Location"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "http://localhost:8003/connect/authorize/")
        q = parse_qs(url.query)
        self.assertEqual(q["redirect_uri"], ["http://testserver/field-app/callback/"])
        return q["state"][0]

    @patch("field_app.client.requests.post")
    def test_connect_stores_the_token(self, post):
        post.return_value = _resp(json={"access_token": "fa_tok", "scope": "read",
                                        "user": {"id": 3, "username": "worker"}})
        state = self.start()
        self.client.get("/field-app/callback/", {"code": "c0de", "state": state})
        # Server-to-server, at the server address, with our secret.
        self.assertEqual(post.call_args.args[0], "http://host.docker.internal:8003/api/v1/connect/token/")
        self.assertEqual(post.call_args.kwargs["json"]["client_secret"], "fas_y")
        conn = FieldAppConnection.objects.get(user=self.nav)
        self.assertEqual((conn.token, conn.remote_username), ("fa_tok", "worker"))

    @patch("field_app.client.requests.post")
    def test_wrong_state_is_refused(self, post):
        self.start()
        self.client.get("/field-app/callback/", {"code": "c0de", "state": "forged"})
        post.assert_not_called()
        self.assertFalse(FieldAppConnection.objects.exists())

    def test_declined_stores_nothing(self):
        state = self.start()
        self.client.get("/field-app/callback/", {"error": "access_denied", "state": state})
        self.assertFalse(FieldAppConnection.objects.exists())

    def test_profile_shows_connect_button(self):
        self.assertContains(self.client.get("/profile/"), "Connect Field App")


class LinkTests(_Base):
    def setUp(self):
        super().setUp()
        FieldAppConnection.objects.create(installation=self.inst, user=self.nav, token="fa_tok")
        self.client.force_login(self.nav)

    @patch("field_app.client.requests.get")
    def test_search_then_link(self, get):
        get.return_value = _resp(json=[{"id": 1, "name": "Ada", "lastname": "Demo", "phone_number": ""}])
        page = self.client.get(f"/patients/{self.ada.pk}/", {"fa_inst": self.inst.pk, "fa_q": "Ada Demo"})
        self.assertContains(page, "Link")
        get.return_value = _resp(json={"id": 1, "name": "Ada", "lastname": "Demo"})
        self.client.post(f"/patients/{self.ada.pk}/field-app/{self.inst.pk}/link/", {"athlete_id": "1"})
        link = PatientFieldAppLink.objects.get(patient=self.ada)
        self.assertEqual((link.athlete_id, link.athlete_name, link.linked_by), (1, "Ada Demo", self.nav))

    def test_cannot_link_someone_elses_client(self):
        res = self.client.post(f"/patients/{self.other.pk}/field-app/{self.inst.pk}/link/", {"athlete_id": "1"})
        self.assertEqual(res.status_code, 403)


class DataSourceTests(_Base):
    def setUp(self):
        super().setUp()
        FieldAppConnection.objects.create(installation=self.inst, user=self.nav, token="fa_tok")
        PatientFieldAppLink.objects.create(patient=self.ada, installation=self.inst, athlete_id=1,
                                           athlete_name="Ada Demo", linked_by=self.nav)
        self.conv = Conversation.objects.create(id=uuid.uuid4(), patient=self.ada, agent=self.agent)

    def test_agent_form_saves_the_data_source(self):
        self.client.force_login(_admin())
        page = self.client.get(f"/agents/{self.agent.pk}/edit/")
        self.assertContains(page, "Data sources")
        form = page.context["form"]
        data = {k: v for k, v in form.initial.items() if v is not None and not isinstance(v, (list, dict))}
        data.update({"name": "Coach", "system_prompt": "Be kind.",
                     f"fieldapp{self.inst.pk}-enabled": "on", f"fieldapp{self.inst.pk}-rounds": "2",
                     f"fieldapp{self.inst.pk}-instructions": ""})
        self.client.post(f"/agents/{self.agent.pk}/edit/", data)
        source = AgentDataSource.objects.get(agent=self.agent)
        self.assertEqual((source.enabled, source.rounds), (True, 2))

    @patch("field_app.client.requests.get")
    def test_context_is_this_clients_record_only(self, get):
        AgentDataSource.objects.create(agent=self.agent, installation=self.inst, enabled=True)
        get.return_value = _resp(text=RECORD)
        ctx = extensions.agent_context(self.agent, str(self.conv.id))
        self.assertIn("<field_app_record", ctx)
        self.assertIn("**Okay**", ctx)
        self.assertIn("/api/v1/patients/1/context/", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer fa_tok")

        other_conv = Conversation.objects.create(id=uuid.uuid4(), patient=self.other, agent=self.agent)
        get.reset_mock()
        self.assertEqual(extensions.agent_context(self.agent, str(other_conv.id)), "")
        get.assert_not_called()

    @patch("field_app.client.requests.get")
    def test_disabled_source_adds_nothing(self, get):
        AgentDataSource.objects.create(agent=self.agent, installation=self.inst, enabled=False)
        self.assertEqual(extensions.agent_context(self.agent, str(self.conv.id)), "")
        get.assert_not_called()

    @patch("field_app.client.requests.get")
    def test_revoked_token_is_marked_and_the_turn_goes_on(self, get):
        AgentDataSource.objects.create(agent=self.agent, installation=self.inst, enabled=True)
        get.return_value = _resp(status=401, json={"detail": "Invalid or revoked token."})
        self.assertEqual(extensions.agent_context(self.agent, str(self.conv.id)), "")
        self.assertIsNotNone(FieldAppConnection.objects.get(user=self.nav).broken_at)

    @patch("field_app.client.requests.get")
    @patch("ConvAI.native_agents.prompt_agent.build_prompt_graph")
    def test_run_prompt_agent_sends_the_record(self, build, get):
        """The record reaches the system prompt the graph is built with."""
        from ConvAI import native_agents
        AgentDataSource.objects.create(agent=self.agent, installation=self.inst, enabled=True)
        get.return_value = _resp(text=RECORD)
        captured = {}

        def fake_arun(builder, *args):
            captured["builder"] = builder
            return "ok"

        with patch.object(native_agents, "_arun_graph", new=fake_arun), \
                patch.object(native_agents, "_run_sync", side_effect=lambda factory, label: factory()):
            native_agents.run_prompt_agent(self.agent, str(self.conv.id), "hi")
        captured["builder"]("checkpointer")
        prompt = build.call_args.args[0]
        self.assertTrue(prompt.startswith("Be kind."))
        self.assertIn("**Okay**", prompt)
