"""Proof that the "off looks untouched" assertions are not vacuous.

Each test here is the mirror of one in OffLooksUntouchedTests: with the switch
**on**, the very string that test requires to be absent must be present. Without
this, a typo in a selector or a page that quietly 404s would make those tests
pass while the feature leaked everywhere.

    python3 manage.py test ConvAI.test_enrolment_teeth --settings=test_settings
"""
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from ConvAI import enrolment as en
from ConvAI.models import ConvAIUser, SiteConfiguration, Study

PASSWORD = "test-pass-not-a-real-secret"


def _config(**kwargs):
    cfg = SiteConfiguration.load()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    cfg.save()
    cache.clear()


class OnIsVisibleTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.admin = ConvAIUser.objects.create_user(
            "adm", password=PASSWORD, is_staff=True, is_superuser=True)
        self.admin.user_permissions.add(
            Permission.objects.get(codename="access_configuration"))
        self.client.force_login(self.admin)
        self.study = Study.objects.create(slug="trial", display_name="A Trial")

    def tearDown(self):
        cache.clear()

    def test_settings_shows_everything_the_off_state_withholds(self):
        from ConvAI.test_enrolment import OffLooksUntouchedTests

        body = self.client.get(reverse("config")).content.decode()
        for marker in OffLooksUntouchedTests.ON_ONLY:
            with self.subTest(marker=marker):
                self.assertIn(marker, body)

    def test_the_clients_page_carries_the_enrolment_block(self):
        body = self.client.get(reverse("patients")).content.decode()
        self.assertIn("Study enrolment", body)

    def test_a_client_page_carries_the_study_section(self):
        e = en.create_enrolment(study=self.study, name="Ada", lastname="L")
        en.claim(code=e.access_code, ip="1.1.1.1")
        e.refresh_from_db()
        en.record_consent(enrolment=e, answers={i["key"]: True for i in self.study.consent_items})
        e.refresh_from_db()

        body = self.client.get(reverse("patient_detail", args=[e.patient_id])).content.decode()
        self.assertIn("Manage enrolment", body)

    def test_django_admin_lists_the_models(self):
        # follow=True because the project mounts its URLs under i18n_patterns, so
        # /admin/ redirects to the language-prefixed path.
        resp = self.client.get("/admin/", follow=True)
        self.assertEqual(resp.status_code, 200,
                         "the admin index must actually render, or the off-state "
                         "test that greps it proves nothing")
        body = resp.content.decode()
        for label in ("Studies", "Enrolments", "Consent records"):
            with self.subTest(model=label):
                self.assertIn(label, body)

    def test_the_user_form_asks_which_study(self):
        url = reverse("admin:ConvAI_convaiuser_change", args=[self.admin.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIn('name="study"', self.client.get(url).content.decode())

    def test_the_public_flow_exists(self):
        anon = self.client_class()
        self.assertEqual(anon.get(reverse("enrolment_landing")).status_code, 200)

    def test_the_api_exists(self):
        token = Token.objects.create(user=self.admin)
        resp = self.client.get(reverse("api_enrolments") + "?phone=%2B447700900123",
                               HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(resp.status_code, 200)

    def test_the_agent_is_offered_a_join_link(self):
        """AGENT_CALLBACK_URL is environment-only — there is no field for it on
        SiteConfiguration — so this sets it the way a deployment would."""
        from unittest.mock import patch

        from ConvAI.utils import _enrolment_join_url

        with patch.dict("os.environ", {"AGENT_CALLBACK_URL": "https://example.test"}):
            self.assertEqual(_enrolment_join_url(), "https://example.test/join/")
