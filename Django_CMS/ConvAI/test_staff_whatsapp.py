"""Link Worker on WhatsApp: a navigator's own phone as a way into Link Worker v2.

*Off means WhatsApp as it was.* Nothing is routed, nothing is answered, no card.

*A number is proven, not typed in.* A code from the profile page, sent from the
phone itself; wrong, stale or over-tried codes do nothing. A number that is also
a client's or a caregiver's is never staff.

*One client, chosen by the link worker.* Questions answer about the loaded
client only; loading is logged; the loaded client is put down when idle.

*A voice note gets a voice back* — in the Brazilian Portuguese voice when they
spoke Portuguese — and the text still arrives if the voice cannot be made.

    python3 manage.py test ConvAI.test_staff_whatsapp --settings=test_settings
"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ConvAI import staff_whatsapp as sw
from ConvAI.models import (
    Agent, Answer, Caregiver, ConvAIUser, Meeting, Message, Patient, Protocol, Question,
    RecordAccess, SiteConfiguration, StaffWhatsAppLink,
)

PASSWORD = "test-pass-not-a-real-secret"
NAV_PHONE = "+5511912345678"


def _config(**kwargs):
    cfg = SiteConfiguration.load()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    cfg.save()
    cache.clear()


def _on():
    _config(link_worker_v2_enabled="1", link_worker_whatsapp_enabled="1")


def _navigator(username, phone=None):
    user = ConvAIUser.objects.create_user(username, password=PASSWORD, first_name="Rosa",
                                          last_name="Kim", phone_number=phone)
    user.groups.add(Group.objects.get_or_create(name="Navigator")[0])
    return user


class SetupMixin:
    def setUp(self):
        cache.clear()
        self.nav = _navigator("rosa", NAV_PHONE)
        self.other = _navigator("other")
        self.v2 = Agent.objects.create(name="Link Worker v2 (beta)", kind="native",
                                       native_key="link_worker_v2", tts_voice_id="agentVoice")
        self.maria = Patient.objects.create(name="Maria", lastname="Silva", navigator=self.nav,
                                            phone_number="+5511900000001")
        self.joao = Patient.objects.create(name="João", lastname="Souza", navigator=self.nav)
        self.bob = Patient.objects.create(name="Bob", lastname="Other", navigator=self.other)

    def tearDown(self):
        cache.clear()

    def _linked(self):
        """A verified link for Rosa's phone, as after a successful LINK."""
        return StaffWhatsAppLink.objects.create(user=self.nav, phone_number=NAV_PHONE,
                                                verified_at=timezone.now())


class Base(SetupMixin, TestCase):
    pass


# ----------------------------------------------------------------------------
# Off
# ----------------------------------------------------------------------------

class OffTests(Base):
    def test_off_nothing_is_routed_even_for_a_linked_number(self):
        self._linked()
        self.assertIsNone(sw.handle_text(NAV_PHONE, "hello"))
        self.assertIsNone(sw.voice_link(NAV_PHONE))

    def test_on_needs_v2_as_well(self):
        _config(link_worker_whatsapp_enabled="1")
        self._linked()
        self.assertIsNone(sw.handle_text(NAV_PHONE, "hello"))

    def test_off_an_unknown_number_goes_to_self_registration_as_before(self):
        from ConvAI.utils import process_received_message

        self._linked()
        with patch("ConvAI.utils._handle_self_registration_flow", return_value="SELFREG") as sr:
            self.assertEqual(process_received_message(f"whatsapp:{NAV_PHONE}", "hi"), "SELFREG")
        sr.assert_called_once()

    def test_off_the_profile_has_no_card(self):
        self.client.force_login(self.nav)
        body = self.client.get(reverse("profile")).content.decode()
        self.assertNotIn('id="whatsapp"', body)
        self.assertNotIn("Link Worker on WhatsApp", body)

    def test_off_the_link_views_refuse(self):
        self.client.force_login(self.nav)
        self.client.post(reverse("whatsapp_link_start"), {"phone_number": NAV_PHONE})
        self.assertFalse(StaffWhatsAppLink.objects.exists())


# ----------------------------------------------------------------------------
# Linking
# ----------------------------------------------------------------------------

class LinkingTests(Base):
    def setUp(self):
        super().setUp()
        _on()

    def test_a_code_sent_from_the_phone_links_it(self):
        code = sw.start_link(self.nav, NAV_PHONE)
        self.assertIsNone(sw.voice_link(NAV_PHONE), "not linked until the code comes back")
        reply = sw.handle_text(NAV_PHONE, f"LINK {code}")
        self.assertIn("Linked", reply)
        self.assertEqual(sw.voice_link(NAV_PHONE).user, self.nav)

    def test_the_portuguese_word_works_too(self):
        code = sw.start_link(self.nav, NAV_PHONE)
        self.assertIn("Vinculado", sw.handle_text(NAV_PHONE, f"vincular {code}"))

    def test_the_code_is_stored_hashed(self):
        code = sw.start_link(self.nav, NAV_PHONE)
        self.assertNotIn(code, StaffWhatsAppLink.objects.get().code_hash)

    def test_a_code_from_another_phone_does_nothing(self):
        code = sw.start_link(self.nav, NAV_PHONE)
        self.assertIsNone(sw.handle_text("+5511999999999", f"LINK {code}"))
        self.assertIsNone(StaffWhatsAppLink.objects.get().verified_at)

    def test_a_wrong_code_counts_and_five_lock_it(self):
        code = sw.start_link(self.nav, NAV_PHONE)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(sw.MAX_CODE_TRIES):
            self.assertIn("not right", sw.handle_text(NAV_PHONE, f"LINK {wrong}"))
        self.assertIn("expired", sw.handle_text(NAV_PHONE, f"LINK {code}"),
                      "after five wrong tries even the right code is refused")

    def test_an_expired_code_is_refused(self):
        code = sw.start_link(self.nav, NAV_PHONE)
        StaffWhatsAppLink.objects.update(code_expires_at=timezone.now() - timedelta(minutes=1))
        self.assertIn("expired", sw.handle_text(NAV_PHONE, f"LINK {code}"))

    def test_a_clients_number_cannot_be_linked(self):
        with self.assertRaises(sw.LinkError):
            sw.start_link(self.nav, "+5511900000001")

    def test_a_caregivers_number_cannot_be_linked(self):
        self.joao.caregiver = Caregiver.objects.create(name="Ana", lastname="Souza",
                                                       phone_number="+5511900000002")
        self.joao.save()
        with self.assertRaises(sw.LinkError):
            sw.start_link(self.nav, "+5511900000002")

    def test_admins_use_the_web_app(self):
        admin = ConvAIUser.objects.create_user("boss", password=PASSWORD,
                                               is_staff=True, is_superuser=True)
        with self.assertRaises(sw.LinkError):
            sw.start_link(admin, "+5511955555555")

    def test_another_accounts_number_cannot_be_taken(self):
        self._linked()
        with self.assertRaises(sw.LinkError):
            sw.start_link(self.other, NAV_PHONE)

    def test_a_number_later_given_to_a_client_stops_being_staff(self):
        self._linked()
        self.bob.phone_number = NAV_PHONE
        self.bob.save()
        self.assertIsNone(sw.voice_link(NAV_PHONE))

    def test_the_profile_card_shows_the_code_once(self):
        self.client.force_login(self.nav)
        self.client.post(reverse("whatsapp_link_start"), {"phone_number": NAV_PHONE})
        body = self.client.get(reverse("profile")).content.decode()
        self.assertIn('id="whatsapp"', body)
        self.assertIn("LINK ", body)
        again = self.client.get(reverse("profile")).content.decode()
        self.assertNotIn("Send this from the phone", again, "the code is shown once")

    def test_unlinking(self):
        self._linked()
        self.client.force_login(self.nav)
        self.client.post(reverse("whatsapp_unlink"))
        self.assertFalse(StaffWhatsAppLink.objects.exists())


# ----------------------------------------------------------------------------
# Routing and the session
# ----------------------------------------------------------------------------

class RoutingTests(Base):
    def setUp(self):
        super().setUp()
        _on()
        self.link = self._linked()

    def test_a_staff_message_is_answered_by_the_whatsapp_assistant(self):
        with patch("ConvAI.native_agents.run_native", return_value="Olá!") as run:
            reply = sw.handle_text(NAV_PHONE, "oi")
        self.assertEqual(reply, "Olá!")
        key, _thread, text, cfg, _model = run.call_args.args
        self.assertEqual((key, text, cfg["staff_user_id"], cfg["reply_mode"]),
                         ("link_worker_whatsapp", "oi", self.nav.pk, "text"))

    def test_it_is_the_navigators_own_conversation(self):
        with patch("ConvAI.native_agents.run_native", return_value="Olá!"):
            sw.handle_text(NAV_PHONE, "oi")
        msg = Message.objects.get()
        self.assertEqual((msg.account, msg.patient, msg.sender_role),
                         (self.nav, None, Message.SenderRole.STAFF))

    def test_it_comes_in_through_the_ordinary_door(self):
        from ConvAI.utils import process_received_message

        with patch("ConvAI.native_agents.run_native", return_value="Olá!"):
            self.assertEqual(process_received_message(f"whatsapp:{NAV_PHONE}", "oi"), "Olá!")

    def test_sms_is_not_a_way_in(self):
        self.assertIsNone(sw.handle_text(NAV_PHONE, "oi", channel="sms"))

    def test_the_loaded_client_heads_every_reply(self):
        self.link.loaded_patient, self.link.loaded_at = self.maria, timezone.now()
        self.link.last_message_at = timezone.now()
        self.link.save()
        with patch("ConvAI.native_agents.run_native", return_value="Ela dormiu melhor."):
            reply = sw.handle_text(NAV_PHONE, "como ela está?")
        self.assertTrue(reply.startswith("📋 Maria Silva"))

    def test_the_loaded_client_is_put_down_when_idle(self):
        long_ago = timezone.now() - sw.LOAD_IDLE - timedelta(minutes=1)
        StaffWhatsAppLink.objects.filter(pk=self.link.pk).update(
            loaded_patient=self.maria, loaded_at=long_ago, last_message_at=long_ago,
            thread_id="old-thread")
        with patch("ConvAI.native_agents.run_native", return_value="ok"):
            reply = sw.handle_text(NAV_PHONE, "oi")
        self.link.refresh_from_db()
        self.assertIsNone(self.link.loaded_patient)
        self.assertNotEqual(self.link.thread_id, "old-thread")
        self.assertFalse(reply.startswith("📋"))

    def test_ending_the_session_unloads(self):
        StaffWhatsAppLink.objects.filter(pk=self.link.pk).update(
            loaded_patient=self.maria, last_message_at=timezone.now())
        self.assertIn("encerrada", sw.handle_text(NAV_PHONE, "/sair"))
        self.link.refresh_from_db()
        self.assertIsNone(self.link.loaded_patient)

    def test_no_classifier_and_no_client_file(self):
        with patch("ConvAI.native_agents.run_native", return_value="ok"), \
             patch("ConvAI.conversation_alerts.review_conversation") as review:
            sw.handle_text(NAV_PHONE, "oi")
        review.assert_not_called()
        self.assertFalse(Message.objects.filter(patient__isnull=False).exists())


# ----------------------------------------------------------------------------
# The assistant's tools (real graph, scripted model)
# ----------------------------------------------------------------------------

def _scripted(calls):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class Scripted(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    replies = [AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": f"c{i}"}])
               for i, (n, a) in enumerate(calls)] + [AIMessage(content="Pronto.")]
    return Scripted(messages=iter(replies))


class ToolTests(SetupMixin, TransactionTestCase):
    """TransactionTestCase: LangGraph runs tools in a worker thread (see test_client_records)."""

    def setUp(self):
        super().setUp()
        _on()
        self.link = self._linked()
        proto = Protocol.objects.create(number=2, title="Bem-estar semanal")
        q = Question.objects.create(protocol=proto, order=1, prompt_md="Como tem dormido?")
        now = timezone.now()
        m1 = Meeting.objects.create(patient=self.maria, scheduled_time=now - timedelta(days=14),
                                    status=Meeting.Status.COMPLETED)
        m2 = Meeting.objects.create(patient=self.maria, scheduled_time=now - timedelta(days=2),
                                    status=Meeting.Status.COMPLETED)
        Answer.objects.create(meeting=m1, question=q, response="Mal, acorda às 3h.")
        Answer.objects.create(meeting=m2, question=q, response="Melhor, dorme a noite toda.")
        # Today, earlier than now: still one of today's visits.
        self.today = Meeting.objects.create(patient=self.joao,
                                            scheduled_time=now - timedelta(minutes=30))

    def _run(self, calls):
        from langgraph.checkpoint.memory import MemorySaver

        from ConvAI.native_agents import link_worker_v2

        with patch("ConvAI.llm_factory.make_llm", return_value=_scripted(calls)):
            graph = link_worker_v2.build_whatsapp(MemorySaver())
        out = graph.invoke({"messages": [{"role": "user", "content": "?"}]},
                           {"configurable": {"thread_id": "t", "staff_user_id": self.nav.pk}})
        return [m.content for m in out["messages"] if m.type == "tool"]

    def test_nothing_about_a_client_until_one_is_loaded(self):
        results = self._run([("client_overview", {}), ("protocol_answers", {})])
        self.assertTrue(all(r.startswith("NO_CLIENT_LOADED") for r in results))

    def test_todays_visits_are_suggested_even_after_they_started(self):
        results = self._run([("suggest_clients", {})])
        today = json.loads(results[0])["today"]
        self.assertEqual([m["client"]["name"] for m in today], ["João Souza"])

    def test_the_link_worker_can_load_any_of_their_clients(self):
        """Maria has no meeting today; loading her to prepare is allowed."""
        results = self._run([("load_client", {"client_id": self.maria.pk,
                                              "reason": "preparar a visita de quinta"}),
                             ("protocol_history", {"protocol": "bem-estar"})])
        self.assertTrue(results[0].startswith("LOADED"))
        self.assertIn("Melhor, dorme a noite toda.", results[1])
        self.link.refresh_from_db()
        self.assertEqual(self.link.loaded_patient, self.maria)
        load = RecordAccess.objects.get(action="load")
        self.assertEqual((load.patient, load.via, load.detail),
                         (self.maria, "whatsapp", "preparar a visita de quinta"))

    def test_another_navigators_client_cannot_be_loaded(self):
        results = self._run([("load_client", {"client_id": self.bob.pk})])
        self.assertTrue(results[0].startswith("NOT_VISIBLE"))
        self.link.refresh_from_db()
        self.assertIsNone(self.link.loaded_patient)

    def test_questions_follow_the_loaded_client(self):
        results = self._run([("load_client", {"client_id": self.joao.pk}),
                             ("protocol_answers", {}),
                             ("load_client", {"client_id": self.maria.pk}),
                             ("protocol_answers", {})])
        self.assertNotIn("Melhor", results[1], "João has no answers")
        self.assertIn("Melhor", results[3])

    def test_there_is_no_caseload_search_on_whatsapp(self):
        from langgraph.checkpoint.memory import MemorySaver

        from ConvAI.native_agents import link_worker_v2

        with patch("ConvAI.llm_factory.make_llm", return_value=_scripted([])):
            graph = link_worker_v2.build_whatsapp(MemorySaver())
        tools = set(graph.nodes["tools"].bound.tools_by_name)
        self.assertNotIn("search_records", tools)
        self.assertIn("load_client", tools)


# ----------------------------------------------------------------------------
# Voice notes
# ----------------------------------------------------------------------------

@override_settings(VOICE_RECORDINGS_DIR="/tmp/cc-test-voice")
class VoiceTests(Base):
    def setUp(self):
        super().setUp()
        _on()
        _config(tts_provider="elevenlabs")  # pinned: the container's .env must not decide
        self.link = self._linked()

    def _voice(self, language, tts_fails=False):
        with patch("ConvAI.staff_whatsapp.transcribe_with_language",
                   return_value=("Como a Maria está dormindo?", language)), \
             patch("ConvAI.native_agents.run_native", return_value="Está dormindo melhor.") as run, \
             patch("ConvAI.tts.synthesize_speech",
                   side_effect=RuntimeError("payment required") if tts_fails else None) as tts:
            out = sw.handle_voice(self.link, "/tmp/in.ogg", "in.ogg")
        return out, run, tts

    def test_portuguese_gets_the_brazilian_voice(self):
        _config(link_worker_voice_pt_br="ptBrVoice")
        (reply, out_name, msg), run, tts = self._voice("portuguese")
        self.assertEqual(tts.call_args.kwargs["voice"], "ptBrVoice")
        self.assertEqual(run.call_args.args[3]["reply_mode"], "voice")
        self.assertEqual(run.call_args.args[3]["spoken_language"], "portuguese")
        self.assertTrue(out_name.endswith(".mp3"))
        self.assertEqual((msg.user_message, msg.response_audio_file), ("Como a Maria está dormindo?", out_name))

    def test_other_languages_get_the_agents_voice(self):
        _config(link_worker_voice_pt_br="ptBrVoice")
        _out, _run, tts = self._voice("english")
        self.assertEqual(tts.call_args.kwargs["voice"], "agentVoice")

    def test_no_brazilian_voice_set_uses_the_agents(self):
        _out, _run, tts = self._voice("portuguese")
        self.assertEqual(tts.call_args.kwargs["voice"], "agentVoice")

    def test_azure_portuguese_gets_a_native_brazilian_voice(self):
        _config(tts_provider="azure", link_worker_voice_pt_br="ptBrVoice")
        _out, _run, tts = self._voice("portuguese")
        self.assertEqual(tts.call_args.kwargs["voice"], "pt-BR-FranciscaNeural")
        _config(link_worker_azure_voice_pt_br="pt-BR-ThalitaMultilingualNeural")
        _out, _run, tts = self._voice("portuguese")
        self.assertEqual(tts.call_args.kwargs["voice"], "pt-BR-ThalitaMultilingualNeural")

    def test_azure_other_languages_get_the_agents_azure_voice(self):
        _config(tts_provider="azure")
        self.v2.azure_voice = "en-GB-SoniaNeural"
        self.v2.save()
        _out, _run, tts = self._voice("english")
        self.assertEqual(tts.call_args.kwargs["voice"], "en-GB-SoniaNeural")

    def test_if_the_voice_cannot_be_made_the_text_still_goes(self):
        with self.assertLogs("ConvAI.staff_whatsapp", level="ERROR"):
            (reply, out_name, msg), _run, _tts = self._voice("portuguese", tts_fails=True)
        self.assertEqual(reply, "Está dormindo melhor.")
        self.assertEqual(out_name, "")


class WebhookTests(Base):
    """Both ways a voice note arrives: the webhook inline, and the async worker."""

    def setUp(self):
        super().setUp()
        _on()
        self.link = self._linked()

    def _post_audio(self):
        with patch.dict("os.environ", {"TWILIO_AUTH_TOKEN": "t"}), \
             patch("ConvAI.views.chat.RequestValidator") as validator, \
             patch("ConvAI.views.chat.get_bool", return_value=True), \
             patch("ConvAI.staff_whatsapp.voice_note_reply",
                   return_value=("Está dormindo melhor.", "", None)) as reply:
            validator.return_value.validate.return_value = True
            resp = self.client.post(reverse("whatsapp_webhook"), {
                "From": f"whatsapp:{NAV_PHONE}", "Body": "", "NumMedia": "1",
                "MediaUrl0": "https://api.twilio.com/media/1", "MediaContentType0": "audio/ogg"})
        return resp, reply

    @override_settings(ASYNC_WHATSAPP_REPLY=False)
    def test_inline_the_staff_voice_note_is_answered(self):
        resp, reply = self._post_audio()
        reply.assert_called_once()
        self.assertIn("Está dormindo melhor.", resp.content.decode())

    def test_the_worker_answers_a_staff_voice_note(self):
        from ConvAI import tasks

        with patch("ConvAI.staff_whatsapp.voice_note_reply",
                   return_value=("Está dormindo melhor.", "", None)), \
             patch("ConvAI.tasks._send_staff_voice") as send, \
             patch("ConvAI.message_attribution.resolve_inbound") as resolve:
            tasks.job_process_whatsapp_audio(f"whatsapp:{NAV_PHONE}", "https://x/1", "audio/ogg",
                                             "", "https://platform.example")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "Está dormindo melhor.")
        resolve.assert_not_called()
