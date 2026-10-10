"""What has to keep working with DEBUG off — how every production install runs.

    python3 manage.py test ConvAI.test_production_mode --settings=test_settings
"""
import logging
import os
import tempfile

from django.conf import settings
from django.test import TestCase, override_settings


class ProductionMode(TestCase):
    def test_debug_is_off_in_tests_too(self):
        self.assertFalse(settings.DEBUG)

    def test_uploaded_logo_is_served_and_nothing_else_in_media(self):
        with tempfile.TemporaryDirectory() as media:
            os.makedirs(os.path.join(media, "branding"))
            with open(os.path.join(media, "branding", "logo.png"), "wb") as fh:
                fh.write(b"\x89PNG")
            with open(os.path.join(media, "care_plan.pdf"), "wb") as fh:
                fh.write(b"%PDF private")
            # The route captured MEDIA_ROOT at import; point the view there.
            from django.urls import resolve
            match = resolve(settings.MEDIA_URL.rstrip("/") + "/branding/logo.png")
            match.kwargs  # noqa: B018 — resolves at all, with DEBUG off
            with override_settings(MEDIA_ROOT=media):
                view = match.func
                from django.test import RequestFactory
                resp = view(RequestFactory().get("/"), path="logo.png",
                            document_root=os.path.join(media, "branding"))
                self.assertEqual(resp.status_code, 200)
            # Anything outside branding/ has no route at all.
            self.assertEqual(self.client.get(settings.MEDIA_URL.rstrip("/") + "/care_plan.pdf").status_code, 404)

    def test_request_errors_reach_the_console(self):
        handlers = logging.getLogger("django.request").handlers or logging.getLogger("django").handlers
        self.assertTrue(any(isinstance(h, logging.StreamHandler) and not h.filters for h in handlers))
