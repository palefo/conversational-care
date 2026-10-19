"""The web app chat: a client's chat on a tablet, opened by QR code.

    python3 manage.py test ConvAI.test_web_chat --settings=test_settings
"""
import datetime as dt
import json
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI import web_chat
from ConvAI.models import (
    Agent, ConvAIUser, Meeting, Message, Patient, Protocol, SiteConfiguration,
    WebChatDevice,
)
from ConvAI.roles import NAVIGATOR

UA = ("Mozilla/5.0 (Linux; Android 14; SM-X200) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def _config(**kwargs):
    cfg = SiteConfiguration.load()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    cfg.save()
    cache.clear()
    return cfg


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
        _config(web_chat_enabled="1")
        self.nav = _navigator()
        self.agent = Agent.objects.create(name="Helper", kind="native", native_key="loopback")
        self.ada = Patient.objects.create(name="Ada Grace", lastname="Lovelace",
                                          navigator=self.nav, agent=self.agent)
        self.bob = Patient.objects.create(name="Bob", lastname="Other", agent=self.agent)
        # No model calls, no classifier: the reply is canned and the review a no-op.
        p1 = patch("ConvAI.utils.generate_response_langgraph", return_value="Hello Ada")
        p2 = patch("ConvAI.conversation_alerts.review_conversation_async")
        self.gen = p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def join_url(self, patient):
        return web_chat.join_path(web_chat.link_for(patient))

    def join(self, patient, client=None):
        client = client or self.client
        res = client.post(self.join_url(patient), HTTP_USER_AGENT=UA)
        self.assertRedirects(res, reverse("webchat_home"), fetch_redirect_response=False)
        return res

    def send(self, text, client=None):
        client = client or self.client
        return client.post(reverse("webchat_send"), data=json.dumps({"message": text}),
                           content_type="application/json")


class OffMeansNothing(_Base):
    """Off, there is no public surface and nothing on the client page."""

    def test_every_route_404s(self):
        url = self.join_url(self.ada)
        _config(web_chat_enabled="0")
        for u in (reverse("webchat_home"), url, reverse("webchat_manifest"),
                  reverse("webchat_sw"), reverse("webchat_messages")):
            self.assertEqual(self.client.get(u).status_code, 404, u)
        self.assertEqual(self.send("hi").status_code, 404)

    def test_default_is_off(self):
        _config(web_chat_enabled="")
        with patch.dict("os.environ", {"WEB_CHAT_ENABLED": ""}):
            self.assertFalse(web_chat.enabled())

    def test_client_page_has_no_card(self):
        _config(web_chat_enabled="0")
        self.client.force_login(self.nav)
        body = self.client.get(reverse("patient_detail", args=[self.ada.pk])).content.decode()
        self.assertNotIn('id="webapp"', body)
        self.assertFalse(hasattr(self.ada, "web_chat_link") and
                         web_chat.WebChatLink.objects.filter(patient=self.ada).exists())

    def test_a_linked_device_stops_and_comes_back(self):
        self.join(self.ada)
        _config(web_chat_enabled="0")
        self.assertEqual(self.client.get(reverse("webchat_home")).status_code, 404)
        _config(web_chat_enabled="1")
        self.assertContains(self.client.get(reverse("webchat_home")), "Helper")

    def test_web_channel_is_ignored_while_off(self):
        self.ada.chat_channel = "web"
        self.ada.save()
        _config(web_chat_enabled="0")
        self.assertFalse(web_chat.delivers_to_web(self.ada))

    def test_every_settings_tab_can_be_opened(self):
        # The page's script keeps its own list of tabs and falls back to General
        # for anything missing from it — which is how the Web app tab first
        # shipped unreachable.
        import re
        self.client.force_login(_admin())
        res = self.client.get(reverse("config"))
        listed = re.search(r"const validTabs = \[([^\]]*)\]", res.content.decode()).group(1)
        for tab_id, _label, _icon in res.context["tabs"]:
            self.assertIn(f"'{tab_id}'", listed, tab_id)

    def test_settings_tab_keeps_the_switch(self):
        _config(web_chat_enabled="0")
        self.client.force_login(_admin())
        body = self.client.get(reverse("config") + "?tab=webapp").content.decode()
        self.assertIn('name="web_chat_enabled"', body)


class Joining(_Base):
    def test_the_page_names_the_client_and_creates_nothing(self):
        res = self.client.get(self.join_url(self.ada))
        self.assertContains(res, "Ada's chat")
        self.assertNotContains(res, "Lovelace")  # first name only
        self.assertFalse(WebChatDevice.objects.exists())

    def test_posting_links_the_device_with_a_cookie(self):
        res = self.join(self.ada)
        cookie = res.cookies[web_chat.COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/c/")
        device = WebChatDevice.objects.get()
        self.assertEqual(device.patient, self.ada)
        self.assertEqual(device.label, "Android · Chrome")
        self.assertNotEqual(device.key_hash, cookie.value)  # only the hash is kept
        self.assertContains(self.client.get(reverse("webchat_home")), "Helper")

    def test_a_wrong_token_is_refused(self):
        link = web_chat.link_for(self.ada)
        res = self.client.get(reverse("webchat_join", args=[link.public_id, "x" * 22]))
        self.assertEqual(res.status_code, 404)
        self.assertContains(res, "no longer works", status_code=404)

    def test_scanning_someone_elses_code_signs_the_first_out(self):
        self.join(self.ada)
        first = WebChatDevice.objects.get()
        self.join(self.bob)
        first.refresh_from_db()
        self.assertEqual(first.revoked_reason, WebChatDevice.RevokedBy.DEVICE)
        self.assertEqual(web_chat.current_device(self._req()).patient, self.bob)

    def _req(self):
        from django.test import RequestFactory
        req = RequestFactory().get("/c/")
        req.COOKIES = {k: v.value for k, v in self.client.cookies.items()}
        return req

    def test_an_unlinked_device_is_told_how_to_get_in(self):
        self.assertContains(self.client.get(reverse("webchat_home")), "Scan the QR code")


class Chatting(_Base):
    def test_a_message_is_stored_as_the_clients_and_answered(self):
        self.join(self.ada)
        res = self.send("hi there")
        self.assertEqual(res.status_code, 200)
        msg = Message.objects.get()
        self.assertEqual(msg.patient, self.ada)
        self.assertEqual(msg.sender_role, Message.SenderRole.CLIENT)
        self.assertEqual(msg.channel, Message.Channel.WEB)
        self.assertIsNone(msg.account)
        rows = res.json()["rows"]
        self.assertEqual(rows[0]["id"], msg.pk)
        self.assertEqual(rows[0]["bubbles"], [{"who": "user", "text": "hi there"},
                                              {"who": "bot", "text": "Hello Ada"}])

    def test_not_linked_is_403(self):
        self.assertEqual(self.send("hi").status_code, 403)
        self.assertEqual(self.client.get(reverse("webchat_messages")).status_code, 403)

    def test_history_includes_whatsapp_turns(self):
        # The chat is the client's, not the device's.
        from ConvAI.utils import process_message_for_patient
        process_message_for_patient(self.ada, "from whatsapp", user_label="+440000",
                                    sender_role=Message.SenderRole.CLIENT, channel="whatsapp")
        self.join(self.ada)
        self.assertContains(self.client.get(reverse("webchat_home")), "from whatsapp")

    def test_the_poll_brings_what_the_platform_sent(self):
        self.join(self.ada)
        self.send("hi")
        cursor = Message.objects.get().pk
        sent = web_chat.deliver(self.ada, "Question 1: how are you?")
        rows = self.client.get(reverse("webchat_messages"), {"after": cursor}).json()["rows"]
        self.assertEqual([r["id"] for r in rows], [sent.pk])
        self.assertEqual(rows[0]["bubbles"], [{"who": "bot", "text": "Question 1: how are you?"}])
        self.assertEqual(sent.channel, Message.Channel.WEB)
        self.assertEqual(sent.sender_role, Message.SenderRole.PLATFORM)

    def test_a_device_sees_only_its_own_client(self):
        from ConvAI.utils import process_message_for_patient
        process_message_for_patient(self.bob, "bob's secret", user_label="x",
                                    sender_role=Message.SenderRole.CLIENT)
        bob_msg = Message.objects.get()
        self.join(self.ada)
        rows = self.client.get(reverse("webchat_messages")).json()["rows"]
        self.assertEqual(rows, [])
        bob_msg.input_audio_file = "x.webm"
        bob_msg.save()
        self.assertEqual(self.client.get(
            reverse("webchat_audio", args=[bob_msg.pk, "input"])).status_code, 404)

    def test_quit_resets_without_a_message(self):
        self.join(self.ada)
        j = self.send("/quit").json()
        self.assertTrue(j["reset"])
        self.assertEqual(j["rows"], [])
        self.assertTrue(j["notice"])
        self.assertFalse(Message.objects.exists())

    def test_separate_messages_are_separate_bubbles(self):
        self.agent.send_separately = True
        self.agent.save()
        self.gen.return_value = "One\n\nTwo"
        self.join(self.ada)
        bubbles = self.send("hi").json()["rows"][0]["bubbles"]
        self.assertEqual([b["text"] for b in bubbles], ["hi", "One", "Two"])

    def test_voice_follows_the_whatsapp_audio_switch(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.join(self.ada)
        _config(whatsapp_audio_enabled="0")
        audio = SimpleUploadedFile("rec.webm", b"x", content_type="audio/webm")
        self.assertEqual(self.client.post(reverse("webchat_voice"), {"audio_data": audio}).status_code, 404)
        self.assertNotContains(self.client.get(reverse("webchat_home")), 'id="micButton" class="wa-iconbtn"')

    def test_voice_note_goes_through_the_full_pipeline(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        _config(whatsapp_audio_enabled="1")
        self.join(self.ada)
        audio = SimpleUploadedFile("rec.webm", b"x", content_type="audio/webm")
        with patch("ConvAI.views.webchat.transcribe_audio", return_value="spoken words"), \
                patch("ConvAI.views.webchat.synthesize_speech"):
            res = self.client.post(reverse("webchat_voice"), {"audio_data": audio})
        self.assertEqual(res.status_code, 200)
        msg = Message.objects.get()
        self.assertEqual(msg.user_message, "spoken words")
        self.assertEqual(msg.channel, Message.Channel.WEB)
        self.assertTrue(str(msg.input_audio_file).endswith("_web_in.webm"))
        self.assertTrue(str(msg.response_audio_file).endswith("_web_out.mp3"))


class EndingAccess(_Base):
    def test_sign_out_on_the_device(self):
        self.join(self.ada)
        self.client.post(reverse("webchat_signout"))
        device = WebChatDevice.objects.get()
        self.assertEqual(device.revoked_reason, WebChatDevice.RevokedBy.DEVICE)
        self.assertEqual(self.send("hi").status_code, 403)

    def test_staff_unlink(self):
        self.join(self.ada)
        device = WebChatDevice.objects.get()
        staff = self.client_class()
        staff.force_login(self.nav)
        staff.post(reverse("webchat_unlink_device", args=[self.ada.pk, device.pk]))
        device.refresh_from_db()
        self.assertEqual(device.revoked_reason, WebChatDevice.RevokedBy.STAFF)
        self.assertEqual(device.revoked_by, self.nav)
        self.assertEqual(self.send("hi").status_code, 403)

    def test_new_qr_code_kills_the_old_one_and_every_device(self):
        old_url = self.join_url(self.ada)
        self.join(self.ada)
        staff = self.client_class()
        staff.force_login(self.nav)
        staff.post(reverse("webchat_reset_link", args=[self.ada.pk]))
        self.assertEqual(self.send("hi").status_code, 403)
        self.assertEqual(self.client.get(old_url).status_code, 404)
        self.assertEqual(self.client.get(self.join_url(self.ada)).status_code, 200)

    def test_another_navigator_cannot_touch_it(self):
        other = _navigator("other")
        self.client.force_login(other)
        for name, args in (("webchat_reset_link", [self.ada.pk]),
                           ("webchat_set_channel", [self.ada.pk])):
            self.assertEqual(self.client.post(reverse(name, args=args),
                                              {"chat_channel": "web"}).status_code, 403)
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.chat_channel, "")


class ClientPage(_Base):
    def test_card_shows_qr_and_devices(self):
        self.join(self.ada)
        staff = self.client_class()
        staff.force_login(self.nav)
        body = staff.get(reverse("patient_detail", args=[self.ada.pk])).content.decode()
        self.assertIn('id="webapp"', body)
        self.assertIn('id="pdWebQr"', body)
        self.assertIn(self.join_url(self.ada), body)
        self.assertIn("Android · Chrome", body)

    def test_set_channel(self):
        self.client.force_login(self.nav)
        self.client.post(reverse("webchat_set_channel", args=[self.ada.pk]), {"chat_channel": "web"})
        self.ada.refresh_from_db()
        self.assertEqual(self.ada.chat_channel, "web")
        self.assertTrue(web_chat.delivers_to_web(self.ada))
        res = self.client.post(reverse("webchat_set_channel", args=[self.ada.pk]), {"chat_channel": "fax"})
        self.assertEqual(res.status_code, 400)


class PlatformMessages(_Base):
    """Protocol questions and reminders reach a web app client in their chat."""

    def setUp(self):
        super().setUp()
        self.ada.chat_channel = "web"
        self.ada.save()
        self.meeting = Meeting.objects.create(
            patient=self.ada, scheduled_time=timezone.now() + dt.timedelta(days=1))

    def test_reminder_goes_to_the_chat_without_a_phone(self):
        from ConvAI.utils import reminder_recipient_missing, send_meeting_reminder
        self.assertEqual(reminder_recipient_missing(self.meeting), "")
        with patch("ConvAI.utils.send_whatsapp_reminder") as wa:
            self.assertEqual(send_meeting_reminder(self.meeting), "web")
        wa.assert_not_called()
        msg = Message.objects.get()
        self.assertEqual(msg.channel, Message.Channel.WEB)
        self.assertIn("Reminder", msg.response_message)
        self.assertEqual(msg.conversation_id, self.ada.current_thread_id)

    def test_off_the_reminder_takes_the_old_road(self):
        from ConvAI.utils import reminder_recipient_missing
        _config(web_chat_enabled="0")
        self.assertEqual(reminder_recipient_missing(self.meeting), "no-phone")

    def test_protocol_question_goes_to_the_chat(self):
        _config(enable_automations="1")
        Agent.objects.get_or_create(kind="native", native_key="protocol_qa",
                                    defaults={"name": "Protocol QA"})
        protocol = Protocol.objects.create(number=91, title="Wellbeing")
        self.client.force_login(self.nav)
        self.gen.return_value = "First question?"
        with patch("ConvAI.views.protocols.send_whatsapp_text_result") as wa, \
                patch("ConvAI.views.protocols.generate_response_langgraph", return_value="First question?"):
            self.client.post(reverse("start_protocol_automation", args=[self.meeting.pk, protocol.number]))
        wa.assert_not_called()
        msg = Message.objects.get()
        self.assertEqual(msg.response_message, "First question?")
        self.assertEqual(msg.channel, Message.Channel.WEB)
        self.ada.refresh_from_db()
        self.assertTrue(self.ada.automation_active)
        self.assertEqual(msg.conversation_id, self.ada.current_thread_id)


class ChannelStamp(TestCase):
    def test_inbound_whatsapp_is_stamped(self):
        nav = _navigator()
        agent = Agent.objects.create(name="A", kind="native", native_key="loopback")
        Patient.objects.create(name="C", lastname="D", navigator=nav, agent=agent,
                               phone_number="+447700900123")
        from ConvAI.utils import process_received_message
        with patch("ConvAI.utils.generate_response_langgraph", return_value="ok"), \
                patch("ConvAI.conversation_alerts.review_conversation_async"):
            process_received_message("whatsapp:+447700900123", "hello")
        self.assertEqual(Message.objects.get().channel, Message.Channel.WHATSAPP)
