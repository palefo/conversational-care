"""Self-registration by SMS as well as WhatsApp.

The Settings page offers a QR code per channel, and a person who writes in on
either is answered by the same registration agent, told which channel it is on.
The registration it files records the channel, for whoever approves it.

    python3 manage.py test ConvAI.test_self_registration_channels --settings=test_settings
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ConvAI.models import ConvAIUser, SelfRegistration, SiteConfiguration


def _configure(**fields):
    cfg = SiteConfiguration.load()
    for name, value in fields.items():
        setattr(cfg, name, value)
    cfg.save()
    cache.clear()


class TheQrCodes(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def context(self):
        from ConvAI.views.settings_views import _selfreg_qr_context
        return _selfreg_qr_context(None)

    def test_sms_goes_to_the_sms_sender_number(self):
        _configure(platform_phone="+14155550100", twilio_sms_from="+447700900123")
        ctx = self.context()
        self.assertTrue(ctx["whatsapp"]["url"].startswith("https://wa.me/14155550100?text="))
        self.assertTrue(ctx["sms"]["url"].startswith("sms:+447700900123?&body="))
        self.assertFalse(ctx["sms"]["from_platform"])
        self.assertIn('id="srQrSvgSms"', ctx["sms"]["qr_svg"])
        self.assertIn('id="srQrSvgWhatsapp"', ctx["whatsapp"]["qr_svg"])

    def test_without_one_it_falls_back_to_the_platform_number(self):
        # As send_sms_text does, so the QR points where replies come from.
        _configure(platform_phone="+14155550100", twilio_sms_from="")
        ctx = self.context()
        self.assertTrue(ctx["sms"]["url"].startswith("sms:+14155550100?"))
        self.assertTrue(ctx["sms"]["from_platform"])

    def test_the_qr_holds_a_text_to_send(self):
        _configure(platform_phone="+14155550100", twilio_sms_from="")
        with patch("ConvAI.views.settings_views._qr_svg", return_value="<svg/>") as qr:
            self.context()
        payloads = [c.args[0] for c in qr.call_args_list]
        self.assertIn(f"SMSTO:+14155550100:{self.context()['message']}", payloads)

    def test_no_number_no_codes(self):
        _configure(platform_phone="", twilio_sms_from="")
        ctx = self.context()
        self.assertEqual(ctx["whatsapp"]["url"], "")
        self.assertEqual(ctx["sms"]["url"], "")

    def test_the_settings_page_shows_both(self):
        _configure(platform_phone="+14155550100", twilio_sms_from="+447700900123")
        admin = ConvAIUser.objects.create_user(
            username="admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        response = self.client.get(reverse("config"))
        self.assertContains(response, 'id="srQrSvgWhatsapp"')
        self.assertContains(response, 'id="srQrSvgSms"')
        self.assertContains(response, "sms:+447700900123?&amp;body=")


class TheChannel(TestCase):
    def test_read_off_the_sender(self):
        from ConvAI.utils import process_received_message
        with patch("ConvAI.utils._handle_self_registration_flow", return_value="hi") as flow:
            process_received_message("whatsapp:+447700900999", "hello")
            process_received_message("+447700900999", "hello")
        self.assertEqual([c.kwargs["channel"] for c in flow.call_args_list], ["whatsapp", "sms"])
        # The number is normalised the same way on both, so it is one person.
        self.assertEqual({c.args[0] for c in flow.call_args_list}, {"+447700900999"})

    def test_the_async_path_says_which(self):
        # It strips the "whatsapp:" prefix before calling, so it must say.
        from ConvAI import tasks
        with patch("ConvAI.tasks.process_received_message", return_value="hi") as prm, \
                patch("ConvAI.tasks.send_sms_text"), patch("ConvAI.tasks.send_whatsapp_text"):
            tasks.job_reply_text("sms", "+447700900999", "hello")
            tasks.job_reply_text("whatsapp", "whatsapp:+447700900999", "hello")
        self.assertEqual([c.kwargs["channel"] for c in prm.call_args_list], ["sms", "whatsapp"])

    def test_the_registration_records_it(self):
        from ConvAI.native_agents.self_registration import _create_self_registration
        _create_self_registration("Cho", "Yoon", "+821089019271", channel="sms")
        _create_self_registration("Ada", "Lovelace", "+447700900123", channel=None)
        self.assertEqual(SelfRegistration.objects.get(name="Cho").details["channel"], "sms")
        self.assertNotIn("channel", SelfRegistration.objects.get(name="Ada").details)
