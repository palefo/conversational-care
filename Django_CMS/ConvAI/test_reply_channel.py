"""The reply goes back on the channel the message came in on.

WhatsApp and SMS arrive at the same Twilio webhook. Twilio says which is which
on every request: a WhatsApp sender is ``whatsapp:+44…``, an SMS sender is a
bare ``+44…``. A caregiver who texts gets a text back — never a WhatsApp they
may not have, or that the 24-hour window would refuse anyway.

    python3 manage.py test ConvAI.test_reply_channel --settings=test_settings
"""
import os
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from ConvAI.models import Caregiver, Patient

CAREGIVER = "+447700900002"
PLATFORM = "+447700900999"


def _run_now(func, *args, **kwargs):
    """The async pool, run inline so the test sees what the worker would send."""
    func(*args, **kwargs)


@patch.dict(os.environ, {"TWILIO_AUTH_TOKEN": "test-token"})
@patch("ConvAI.views.chat.RequestValidator.validate", return_value=True)
class ReplyChannel(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        caregiver = Caregiver.objects.create(name="Grace", lastname="Hopper",
                                             phone_number=CAREGIVER)
        Patient.objects.create(name="Ada", lastname="Lovelace", caregiver=caregiver)

    def post(self, sender, to):
        return self.client.post(reverse("whatsapp_webhook"), {
            "From": sender, "To": to, "Body": "hello", "NumMedia": "0"})

    @override_settings(ASYNC_WHATSAPP_REPLY=True)
    @patch("ConvAI.async_reply.submit", side_effect=_run_now)
    @patch("ConvAI.tasks.process_received_message", return_value="hi Grace")
    @patch("ConvAI.tasks.send_whatsapp_text")
    @patch("ConvAI.tasks.send_sms_text")
    def test_async_sms_in_is_sms_out(self, sms, wa, _agent, _submit, _valid):
        self.post(CAREGIVER, PLATFORM)
        self.assertEqual(sms.call_count, 1)
        wa.assert_not_called()

    @override_settings(ASYNC_WHATSAPP_REPLY=True)
    @patch("ConvAI.async_reply.submit", side_effect=_run_now)
    @patch("ConvAI.tasks.process_received_message", return_value="hi Grace")
    @patch("ConvAI.tasks.send_whatsapp_text")
    @patch("ConvAI.tasks.send_sms_text")
    def test_async_whatsapp_in_is_whatsapp_out(self, sms, wa, _agent, _submit, _valid):
        self.post(f"whatsapp:{CAREGIVER}", f"whatsapp:{PLATFORM}")
        self.assertEqual(wa.call_count, 1)
        sms.assert_not_called()

    @override_settings(ASYNC_WHATSAPP_REPLY=False)
    @patch("ConvAI.views.chat.process_received_message", return_value="hi Grace")
    def test_sync_answers_in_twiml_which_twilio_returns_on_the_same_channel(self, _agent, _valid):
        response = self.post(CAREGIVER, PLATFORM)
        self.assertContains(response, "<Message>hi Grace</Message>")
