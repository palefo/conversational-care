"""Study enrolment: pre-enrolled participants, access codes and versioned consent.

*The switch is off unless somebody turns it on*, and while it is off none of
these pages exist — not the public join flow, not the staff pages, not the API.

*A claim is not an admission.* Typing a valid code fills in who somebody is; it
is **consent** that creates their client record. Anyone who abandons the consent
page has agreed to nothing and must not appear as a client.

*Consent is evidence, so it is append-only and it locks.* Re-consenting writes a
new row; the wording of a version somebody already signed cannot be rewritten,
only superseded by a new version.

*A wrong code says nothing.* One message for "no such code", "already claimed"
and "study closed" alike, and every miss is counted against the caller.

    python3 manage.py test ConvAI.test_enrolment --settings=test_settings
"""
import datetime as dt

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token

from ConvAI import enrolment as en
from ConvAI.enrolment.codes import generate_access_code, normalise_code
from ConvAI.models import (
    ConsentRecord, ConvAIUser, Enrolment, Patient, SiteConfiguration, Study,
)

PASSWORD = "test-pass-not-a-real-secret"


def _config(**kwargs):
    cfg = SiteConfiguration.load()
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    cfg.save()
    cache.clear()
    return cfg


def _admin(username="adm"):
    user = ConvAIUser.objects.create_user(username, password=PASSWORD,
                                          is_staff=True, is_superuser=True)
    user.user_permissions.add(Permission.objects.get(codename="access_configuration"))
    return user


def _study(**kwargs):
    defaults = {
        "slug": "trial",
        "display_name": "A Trial",
        "consent_version": "1.0",
    }
    defaults.update(kwargs)
    return Study.objects.create(**defaults)


def _all_yes(study):
    return {i["key"]: True for i in study.consent_items}


class EnrolmentSwitchTests(TestCase):
    """While the feature is off, none of it exists."""

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="")

    def test_off_by_default(self):
        self.assertFalse(en.enabled())

    def test_public_pages_404_while_off(self):
        for name in ("enrolment_landing", "enrolment_claim",
                     "enrolment_consent", "enrolment_done", "enrolment_resume"):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 404)

    def test_public_pages_exist_once_on(self):
        _config(study_enrolment_enabled="1")
        self.assertEqual(self.client.get(reverse("enrolment_landing")).status_code, 200)

    def test_staff_pages_404_while_off_even_for_an_admin(self):
        """404, not a redirect: an admin should not find a page for a feature
        nobody switched on."""
        _config(study_enrolment_enabled="")
        admin = _admin()
        self.client.force_login(admin)
        for name, args in (("participant_detail", [1]), ("study_editor", [1])):
            with self.subTest(route=name):
                self.assertEqual(
                    self.client.get(reverse(name, args=args)).status_code, 404)


class OffLooksUntouchedTests(TestCase):
    """With the switch off the platform must look exactly as it did before.

    Not just "the feature is unreachable" — nothing about it may appear
    anywhere: no menu entry, no admin section, no extra field on a form, no
    markup. The one exception is the switch itself, which has to be somewhere an
    admin can reach: Settings -> Participants lists it, and while enrolment is
    off it holds that switch and nothing else.
    """

    # Emitted only by the switched-on Participants panel. Mirrored in
    # test_enrolment_teeth so this cannot pass by the panel quietly not rendering.
    ON_ONLY = ("New study", "Join page", 'class="pt-caveat"', "joinUrlField",
               'type="number" name="enrolment_code_words"')

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="")
        self.admin = _admin()
        self.client.force_login(self.admin)

    def tearDown(self):
        cache.clear()

    def test_no_participants_entry_in_the_menu(self):
        body = self.client.get(reverse("patients")).content.decode()
        self.assertNotIn("Participants", body)

    def test_settings_offers_the_switch_and_nothing_else(self):
        body = self.client.get(reverse("config")).content.decode()
        self.assertIn('name="study_enrolment_enabled"', body,
                      "an admin must be able to switch enrolment on from Settings")
        for marker in self.ON_ONLY:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, body)

    def test_switching_it_on_and_off_keeps_the_options(self):
        """The options ride along unseen while off, so the switch cannot reset them."""
        _config(enrolment_code_attempt_limit=7, enrolment_landing_text="Hello")
        self.client.post(reverse("config_save"), {
            "section": "participants", "study_enrolment_enabled": "1",
            "enrolment_code_words": "3", "enrolment_code_attempt_limit": "7",
            "enrolment_landing_text": "Hello", "enrolment_require_dob": "",
            "enrolment_auto_approve": "",
        })
        body = self.client.get(reverse("config")).content.decode()
        # Off again, through the form as rendered while off: whatever it carries
        # in hidden inputs is what gets posted.
        import re
        hidden = dict(re.findall(
            r'<input type="hidden" name="(enrolment_[a-z_]+)" value="([^"]*)"', body))
        self.assertEqual(hidden, {}, "while on, the options are shown, not hidden")

        _config(study_enrolment_enabled="0")
        body = self.client.get(reverse("config")).content.decode()
        hidden = dict(re.findall(
            r'<input type="hidden" name="(enrolment_[a-z_]+)"[^>]*value="([^"]*)"', body))
        self.assertEqual(hidden.get("enrolment_code_attempt_limit"), "7")
        self.assertEqual(hidden.get("enrolment_landing_text"), "Hello")

        self.client.post(reverse("config_save"), {
            "section": "participants", "study_enrolment_enabled": "1", **hidden})
        cfg = SiteConfiguration.load()
        self.assertEqual(cfg.enrolment_code_attempt_limit, 7)
        self.assertEqual(cfg.enrolment_landing_text, "Hello")

    def test_the_clients_page_carries_no_enrolment_block(self):
        body = self.client.get(reverse("patients")).content.decode()
        self.assertNotIn("Study enrolment", body)
        self.assertNotIn("participant_enrol", body)

    def test_a_client_page_carries_no_study_section(self):
        patient = Patient.objects.create(name="Ordinary", lastname="Client")
        body = self.client.get(reverse("patient_detail", args=[patient.pk])).content.decode()
        self.assertNotIn("Manage enrolment", body)

    def test_the_models_are_hidden_from_django_admin(self):
        # follow=True: the project's URLs sit under i18n_patterns, so /admin/
        # redirects to a language-prefixed path. Its mirror in
        # test_enrolment_teeth asserts this page really renders.
        body = self.client.get("/admin/", follow=True).content.decode()
        for label in ("Studies", "Enrolments", "Consent records"):
            with self.subTest(model=label):
                self.assertNotIn(label, body)

    def test_admin_pages_refuse_even_by_url(self):
        """Hidden from the index is not enough: a bookmarked URL must not work."""
        _config(study_enrolment_enabled="1")
        study = _study()
        _config(study_enrolment_enabled="0")
        for url in (reverse("admin:ConvAI_study_change", args=[study.pk]),
                    reverse("admin:ConvAI_study_delete", args=[study.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url, follow=True).status_code, 403)

    def test_the_user_form_does_not_ask_which_study(self):
        """A platform with no studies should not ask which one somebody is in."""
        body = self.client.get(
            reverse("admin:ConvAI_convaiuser_change", args=[self.admin.pk]),
            follow=True,
        ).content.decode()
        self.assertNotIn('name="study"', body)

    def test_the_public_flow_does_not_exist(self):
        anon = self.client_class()
        for name in ("enrolment_landing", "enrolment_claim", "enrolment_consent",
                     "enrolment_done", "enrolment_resume"):
            with self.subTest(route=name):
                self.assertEqual(anon.get(reverse(name)).status_code, 404)

    def test_the_api_does_not_exist(self):
        token = Token.objects.create(user=self.admin)
        resp = self.client.get(reverse("api_enrolments") + "?phone=%2B447700900123",
                               HTTP_AUTHORIZATION=f"Token {token.key}")
        self.assertEqual(resp.status_code, 404)

    def test_the_registration_agent_is_not_given_the_code_tool(self):
        """Its prompt and tools must be what they were before this feature.

        Set AGENT_CALLBACK_URL so this cannot pass merely because it is unset.
        """
        from unittest.mock import patch

        from ConvAI.utils import _enrolment_join_url

        with patch.dict("os.environ", {"AGENT_CALLBACK_URL": "https://example.test"}):
            self.assertEqual(_enrolment_join_url(), "")

    def test_turning_it_off_hides_but_keeps_everything(self):
        _config(study_enrolment_enabled="1")
        study = _study()
        e = en.create_enrolment(study=study, name="Ada", lastname="L")
        en.claim(code=e.access_code, ip="1.1.1.1")
        e.refresh_from_db()
        en.record_consent(enrolment=e, answers=_all_yes(study))

        _config(study_enrolment_enabled="0")
        self.assertEqual(Enrolment.objects.count(), 1)
        self.assertEqual(ConsentRecord.objects.count(), 1)
        self.assertEqual(Study.objects.count(), 1)


class AccessCodeTests(TestCase):
    def test_codes_are_hyphenated_distinct_words(self):
        code = generate_access_code(3)
        parts = code.split("-")
        self.assertEqual(len(parts), 3)
        self.assertEqual(len(set(parts)), 3, "words in a code should not repeat")

    def test_word_count_is_clamped_to_something_sane(self):
        self.assertEqual(len(generate_access_code(1).split("-")), 2)
        self.assertEqual(len(generate_access_code(9).split("-")), 4)

    def test_normalisation_accepts_what_people_actually_type(self):
        """Retyped from a card: capitals, spaces, a trailing stop, a smart dash."""
        for typed in ("Maple-Crane-Frost", "  maple crane frost ",
                      "maple-crane-frost.", "MAPLE–CRANE–FROST"):
            with self.subTest(typed=typed):
                self.assertEqual(normalise_code(typed), "maple-crane-frost")

    def test_generation_avoids_codes_already_taken(self):
        taken = {generate_access_code(2) for _ in range(20)}
        fresh = generate_access_code(2, exclude=lambda c: c in taken)
        self.assertNotIn(fresh, taken)


class EnrolTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()

    def test_new_study_ships_with_consent_items(self):
        """A study with no tick-boxes would render a form consenting to nothing."""
        keys = [i["key"] for i in self.study.consent_items]
        self.assertIn("participation", keys)
        self.assertIn("safety_monitoring", keys,
                      "the classifier runs on every message; it has to be consented to")

    def test_enrolling_issues_a_code_and_starts_as_invited(self):
        e = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        self.assertTrue(e.access_code)
        self.assertEqual(e.status, Enrolment.Status.INVITED)
        self.assertIsNone(e.patient_id)
        self.assertIsNone(e.claimed_at)

    def test_no_placeholder_date_of_birth(self):
        """The implementation this replaces wrote '2000-01-01' and then tested
        against that string to tell whether a code had been claimed."""
        e = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        self.assertIsNone(e.date_of_birth)

    def test_a_closed_study_takes_nobody_new(self):
        self.study.is_open = False
        self.study.save()
        with self.assertRaises(ValueError):
            en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")

    def test_a_duplicate_code_is_refused_rather_than_silently_changed(self):
        first = en.create_enrolment(study=self.study, name="Ada", lastname="L")
        with self.assertRaises(ValueError):
            en.create_enrolment(study=self.study, name="Bea", lastname="M",
                                access_code=first.access_code)


class ClaimTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")

    def test_claiming_does_not_create_a_client(self):
        """The heart of it: a claim records who they are, consent admits them."""
        claimed = en.claim(code=self.enrolment.access_code, ip="1.1.1.1")
        self.assertIsNotNone(claimed.claimed_at)
        self.assertIsNone(claimed.patient_id)
        self.assertEqual(Patient.objects.count(), 0)

    def test_a_code_works_once(self):
        en.claim(code=self.enrolment.access_code, ip="1.1.1.1")
        with self.assertRaises(en.ClaimError):
            en.claim(code=self.enrolment.access_code, ip="1.1.1.1")

    def test_every_failure_gives_the_same_message(self):
        """Otherwise the form confirms which codes exist."""
        unknown = self._error_for("no-such-code-here")
        en.claim(code=self.enrolment.access_code, ip="1.1.1.1")
        used = self._error_for(self.enrolment.access_code)
        self.assertEqual(str(unknown), str(used))

    def _error_for(self, code):
        try:
            en.claim(code=code, ip="2.2.2.2")
        except en.ClaimError as exc:
            return exc
        self.fail(f"expected {code!r} to be refused")

    def test_a_closed_study_stops_an_unclaimed_code(self):
        self.study.is_open = False
        self.study.save()
        with self.assertRaises(en.ClaimError):
            en.claim(code=self.enrolment.access_code, ip="1.1.1.1")

    def test_a_study_that_needs_a_number_insists_on_one(self):
        self.study.require_phone = True
        self.study.save()
        with self.assertRaises(en.ClaimError):
            en.claim(code=self.enrolment.access_code, ip="1.1.1.1")
        ok = en.claim(code=self.enrolment.access_code,
                      phone_number="+447700900123", ip="1.1.1.1")
        self.assertEqual(str(ok.phone_number), "+447700900123")


    def test_date_of_birth_is_only_required_when_asked_for(self):
        en.claim(code=self.enrolment.access_code, ip="1.1.1.1")  # no DOB, fine

        _config(enrolment_require_dob="1")
        other = en.create_enrolment(study=self.study, name="Bea", lastname="M")
        with self.assertRaises(en.ClaimError):
            en.claim(code=other.access_code, ip="1.1.1.1")


class NoOracleTests(TestCase):
    """The form's questions must not depend on whether the code is real.

    Asking for a date of birth (or a number) only once a code has matched turns
    those messages into a lookup oracle: a guessed code answered with "please
    enter your date of birth" is a code that exists, told apart from a wrong one
    for free. Everything answerable without the code is answered first.
    """

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1", enrolment_require_dob="1")
        self.study = _study()
        self.real = en.create_enrolment(study=self.study, name="Ada", lastname="L")

    def tearDown(self):
        cache.clear()

    def _error(self, code, **kw):
        try:
            en.claim(code=code, ip="5.5.5.5", **kw)
        except en.ClaimError as exc:
            return str(exc)
        self.fail(f"expected {code!r} to be refused")

    def test_a_missing_dob_reads_the_same_for_a_real_and_a_fake_code(self):
        self.assertEqual(self._error(self.real.access_code),
                         self._error("not-a-real-code"))

    def test_a_missing_number_reads_the_same_for_a_real_and_a_fake_code(self):
        _config(study_enrolment_enabled="1", enrolment_require_dob="")
        self.study.require_phone = True
        self.study.save()
        self.assertEqual(self._error(self.real.access_code),
                         self._error("not-a-real-code"))

    def test_a_complete_form_still_tells_a_real_code_from_a_fake_one(self):
        """The guard must not have flattened the flow into refusing everything."""
        import datetime as _dt

        dob = _dt.date(1990, 1, 1)
        self.assertEqual(self._error("not-a-real-code", date_of_birth=dob),
                         str(en.GENERIC_CODE_ERROR))
        claimed = en.claim(code=self.real.access_code, date_of_birth=dob, ip="5.5.5.5")
        self.assertIsNotNone(claimed.claimed_at)

    def test_phone_is_required_whenever_any_open_study_needs_one(self):
        other = _study(slug="sms-arm", display_name="SMS arm", require_phone=True)
        self.assertTrue(en.phone_required())
        other.is_open = False
        other.save()
        self.assertFalse(en.phone_required())

class AttemptLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1", enrolment_code_attempt_limit=3)
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="L")

    def tearDown(self):
        cache.clear()

    def test_wrong_codes_lock_the_caller_out(self):
        for _ in range(3):
            with self.assertRaises(en.ClaimError):
                en.claim(code="not-a-real-code", ip="9.9.9.9")
        self.assertTrue(en.too_many_attempts("9.9.9.9"))

        # Even the right code is refused once they are locked out.
        with self.assertRaises(en.ClaimError):
            en.claim(code=self.enrolment.access_code, ip="9.9.9.9")

    def test_the_lockout_is_per_caller(self):
        for _ in range(3):
            with self.assertRaises(en.ClaimError):
                en.claim(code="not-a-real-code", ip="9.9.9.9")
        self.assertFalse(en.too_many_attempts("8.8.8.8"))
        self.assertIsNotNone(en.claim(code=self.enrolment.access_code, ip="8.8.8.8"))

    def test_a_success_clears_the_count(self):
        with self.assertRaises(en.ClaimError):
            en.claim(code="not-a-real-code", ip="7.7.7.7")
        en.claim(code=self.enrolment.access_code, ip="7.7.7.7")
        self.assertFalse(en.too_many_attempts("7.7.7.7"))


class ConsentTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        en.claim(code=self.enrolment.access_code, ip="1.1.1.1")
        self.enrolment.refresh_from_db()

    def test_consent_admits_the_participant(self):
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, Enrolment.Status.ACTIVE)
        self.assertIsNotNone(self.enrolment.patient_id)

    def test_the_study_agent_is_handed_to_the_new_client(self):
        from ConvAI.models import Agent
        agent = Agent.objects.create(name="Study agent", kind="native", native_key="loopback")
        self.study.default_agent = agent
        self.study.save()

        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.patient.agent_id, agent.pk)

    def test_partial_consent_is_not_consent(self):
        with self.assertRaises(en.ClaimError):
            en.record_consent(enrolment=self.enrolment, answers={"participation": True})
        self.assertEqual(ConsentRecord.objects.count(), 0)
        self.assertEqual(Patient.objects.count(), 0)

    def test_an_optional_item_declined_is_recorded_as_declined(self):
        self.study.consent_items = [
            {"key": "participation", "text": "I agree to take part.", "required": True},
            {"key": "wearable", "text": "I will share my wearable data.", "required": False},
        ]
        self.study.save()

        rec = en.record_consent(enrolment=self.enrolment, answers={"participation": True})
        self.assertEqual(rec.items, {"participation": True, "wearable": False},
                         "a declined optional item must read as declined, not missing")

    def test_the_wording_is_snapshotted_with_the_answers(self):
        rec = en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        for item in self.study.consent_items:
            self.assertEqual(rec.items_text[item["key"]], item["text"])

    def test_records_cannot_be_edited(self):
        rec = en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        rec.user_agent = "tampered"
        with self.assertRaises(ValidationError):
            rec.save()

    def test_re_consent_adds_a_row_and_keeps_the_old_one(self):
        first = en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))

        self.study.consent_version = "2.0"
        self.study.consent_items = [
            {"key": "participation", "text": "Reworded entirely.", "required": True},
        ]
        self.study.save()
        second = en.record_consent(enrolment=self.enrolment, answers={"participation": True})

        self.assertEqual(self.enrolment.consents.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.consent_version, "1.0")
        self.assertNotEqual(first.items_text["participation"],
                            second.items_text["participation"],
                            "the old record must still show the words that were signed")

    def test_re_consent_does_not_demote_somebody_already_taking_part(self):
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, Enrolment.Status.ACTIVE)

        self.study.consent_version = "2.0"
        self.study.save()
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, Enrolment.Status.ACTIVE,
                         "agreeing to a new version must not reset them to 'consented'")

    def test_re_consent_leaves_a_completed_participant_completed(self):
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        Enrolment.objects.filter(pk=self.enrolment.pk).update(status=Enrolment.Status.COMPLETED)
        self.enrolment.refresh_from_db()

        self.study.consent_version = "2.0"
        self.study.save()
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, Enrolment.Status.COMPLETED)


class ConsentLockTests(TestCase):
    """Consent wording locks once signed, the way a protocol locks once answered."""

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="L")
        en.claim(code=self.enrolment.access_code, ip="1.1.1.1")
        self.enrolment.refresh_from_db()

    def test_unsigned_wording_is_editable(self):
        self.assertFalse(self.study.consent_locked)

    def test_signed_wording_locks(self):
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.assertTrue(Study.objects.get(pk=self.study.pk).consent_locked)

    def test_the_form_refuses_to_rewrite_signed_wording(self):
        from ConvAI.forms import StudyForm

        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        form = StudyForm(self._post(version="1.0", text="Completely different words."),
                         instance=Study.objects.get(pk=self.study.pk))
        self.assertFalse(form.is_valid())

    def test_raising_the_version_unlocks_it(self):
        from ConvAI.forms import StudyForm

        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        form = StudyForm(self._post(version="2.0", text="Completely different words."),
                         instance=Study.objects.get(pk=self.study.pk))
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_bumped_version_leaves_participants_stale_until_they_re_consent(self):
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.study.consent_version = "2.0"
        self.study.save()

        self.enrolment.refresh_from_db()
        self.assertFalse(self.enrolment.consent_is_current)
        self.assertEqual(Study.objects.get(pk=self.study.pk).stale_consent_count, 1)

    def test_emptying_every_tick_box_is_refused_not_ignored(self):
        """Silently keeping the old wording would tell the admin nothing, and an
        empty list is re-seeded with the shipped defaults by Study.save()."""
        from ConvAI.forms import StudyForm

        fresh = _study(slug="fresh", display_name="Fresh")
        data = {
            "slug": fresh.slug, "display_name": fresh.display_name,
            "consent_version": "1.0", "consent_intro": "", "pis_url": "",
            "consent_survey_url": "", "baseline_url": "",
            "chief_investigator": "", "iras_project_id": "",
            "items_submitted": "1",          # the editor was the source
            "item_key": [], "item_text": [], "item_required": [],
        }
        form = StudyForm(data, instance=fresh)
        self.assertFalse(form.is_valid())

    def test_a_form_built_without_the_editor_keeps_its_wording(self):
        """Only the editor's own POST means "these are all the items"."""
        from ConvAI.forms import StudyForm

        fresh = _study(slug="fresh2", display_name="Fresh two")
        before = list(fresh.consent_items)
        form = StudyForm({
            "slug": fresh.slug, "display_name": "Renamed",
            "consent_version": "1.0", "consent_intro": "", "pis_url": "",
            "consent_survey_url": "", "baseline_url": "",
            "chief_investigator": "", "iras_project_id": "",
        }, instance=fresh)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.consent_items, before)

    def _post(self, *, version, text):
        return {
            "slug": self.study.slug,
            "display_name": self.study.display_name,
            "consent_version": version,
            "consent_intro": "",
            "pis_url": "",
            "consent_survey_url": "",
            "baseline_url": "",
            "chief_investigator": "",
            "iras_project_id": "",
            "items_submitted": "1",
            "item_key": ["participation"],
            "item_text": [text],
            "item_required": ["participation"],
        }


class SignedStudyEditingTests(TestCase):
    """A study somebody has consented to must still be editable in every other way.

    Posted as the browser posts it: while the wording is locked the "required"
    boxes are disabled, and a disabled box is not sent at all.
    """

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        e = en.create_enrolment(study=self.study, name="Ada", lastname="L")
        en.claim(code=e.access_code, ip="1.1.1.1")
        e.refresh_from_db()
        en.record_consent(enrolment=e, answers=_all_yes(self.study))
        self.study = Study.objects.get(pk=self.study.pk)
        self.client.force_login(_admin())

    def _as_browser(self, *, required=False, **overrides):
        s = self.study
        data = {
            "slug": s.slug, "display_name": s.display_name, "is_open": "on",
            "consent_version": s.consent_version, "consent_intro": s.consent_intro,
            "pis_url": "", "consent_survey_url": "", "baseline_url": "",
            "chief_investigator": "", "iras_project_id": "", "items_submitted": "1",
            "item_key": [i["key"] for i in s.consent_items],
            "item_text": [i["text"] for i in s.consent_items],
        }
        if required:
            data["item_required"] = [i["key"] for i in s.consent_items if i["required"]]
        data.update(overrides)
        return self.client.post(reverse("study_editor_save", args=[s.pk]), data)

    def test_a_signed_study_can_be_closed(self):
        data = {"is_open": ""}
        resp = self._as_browser(**data)
        self.assertEqual(resp.status_code, 302, "closing a signed study must not be refused")
        self.assertFalse(Study.objects.get(pk=self.study.pk).is_open)

    def test_a_signed_study_keeps_its_required_items_required(self):
        """The disabled boxes were not posted; that must not read as "optional"."""
        self._as_browser(display_name="Renamed")
        study = Study.objects.get(pk=self.study.pk)
        self.assertEqual(study.display_name, "Renamed")
        self.assertEqual(study.consent_items, self.study.consent_items)

    def test_raising_the_version_and_rewording_in_one_save_keeps_the_new_words(self):
        texts = [i["text"] for i in self.study.consent_items]
        texts[0] = "Reworded for version two."
        resp = self._as_browser(required=True, consent_version="2.0", item_text=texts)
        self.assertEqual(resp.status_code, 302)

        study = Study.objects.get(pk=self.study.pk)
        self.assertEqual(study.consent_version, "2.0")
        self.assertEqual(study.consent_items[0]["text"], "Reworded for version two.",
                         "the wording is the reason the version was raised")

    def test_signed_wording_still_cannot_be_rewritten_in_place(self):
        texts = [i["text"] for i in self.study.consent_items]
        texts[0] = "Quietly different."
        resp = self._as_browser(item_text=texts)
        self.assertEqual(resp.status_code, 200)
        study = Study.objects.get(pk=self.study.pk)
        self.assertNotEqual(study.consent_items[0]["text"], "Quietly different.")

    def test_a_signed_version_cannot_be_reused_for_other_words(self):
        texts = [i["text"] for i in self.study.consent_items]
        texts[0] = "Version two wording."
        self._as_browser(required=True, consent_version="2.0", item_text=texts)
        self.study = Study.objects.get(pk=self.study.pk)

        # Back to 1.0 — which people signed under the original wording.
        resp = self._as_browser(required=True, consent_version="1.0")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Study.objects.get(pk=self.study.pk).consent_version, "2.0")

    def test_a_failed_save_shows_what_was_typed_and_what_is_saved(self):
        texts = [i["text"] for i in self.study.consent_items]
        texts[0] = "Words I just wrote."
        resp = self._as_browser(required=True, consent_version="2.0", item_text=texts,
                                slug="")  # the unrelated mistake
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Words I just wrote.", body, "typed wording must survive an error")
        self.assertIn("<b>v1.0</b>", body, "the header describes the saved version")


class WithdrawalTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="L")
        en.claim(code=self.enrolment.access_code, ip="1.1.1.1")
        self.enrolment.refresh_from_db()
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.enrolment.refresh_from_db()

    def test_withdrawing_actually_stops_the_messages(self):
        """Not just an annotation: the client's agent is switched off."""
        self.assertTrue(self.enrolment.patient.chatbot_enabled)
        en.withdraw(self.enrolment, reason="asked to stop")
        self.enrolment.refresh_from_db()
        self.enrolment.patient.refresh_from_db()

        self.assertEqual(self.enrolment.status, Enrolment.Status.WITHDRAWN)
        self.assertFalse(self.enrolment.patient.chatbot_enabled)

    def test_withdrawing_deletes_nothing(self):
        en.withdraw(self.enrolment)
        self.assertEqual(ConsentRecord.objects.filter(enrolment=self.enrolment).count(), 1)
        self.assertEqual(Patient.objects.count(), 1)

    def test_a_withdrawn_code_cannot_be_reused(self):
        other = en.create_enrolment(study=self.study, name="Bea", lastname="M")
        en.withdraw(other)
        with self.assertRaises(en.ClaimError):
            en.claim(code=other.access_code, ip="1.1.1.1")


class StatusChangeTests(TestCase):
    """Staff can mark somebody finished. They cannot mark somebody consented."""

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.client.force_login(_admin())
        self.waiting = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")

    def _set(self, enrolment, status):
        self.client.post(reverse("participant_status", args=[enrolment.pk]),
                         {"status": status})
        enrolment.refresh_from_db()
        return enrolment.status

    def _admitted(self):
        e = en.create_enrolment(study=self.study, name="Grace", lastname="Hopper")
        en.claim(code=e.access_code, ip="1.1.1.1")
        e.refresh_from_db()
        en.record_consent(enrolment=e, answers=_all_yes(self.study))
        e.refresh_from_db()
        return e

    def test_nobody_can_be_marked_consented_by_hand(self):
        for status in ("consented", "active"):
            with self.subTest(status=status):
                self.assertEqual(self._set(self.waiting, status), Enrolment.Status.INVITED)
        self.assertEqual(ConsentRecord.objects.count(), 0)

    def test_a_participant_can_be_marked_completed_and_back(self):
        e = self._admitted()
        self.assertEqual(self._set(e, "completed"), Enrolment.Status.COMPLETED)
        self.assertEqual(self._set(e, "active"), Enrolment.Status.ACTIVE)

    def test_nobody_taking_part_can_be_sent_back_to_invited(self):
        e = self._admitted()
        self.assertEqual(self._set(e, "invited"), Enrolment.Status.ACTIVE)

    def test_a_withdrawal_is_not_undone_from_the_status_list(self):
        e = self._admitted()
        en.withdraw(e)
        self.assertEqual(self._set(e, "active"), Enrolment.Status.WITHDRAWN)

    def test_the_form_only_offers_what_it_accepts(self):
        body = self.client.get(reverse("participant_detail", args=[self.waiting.pk])).content.decode()
        self.assertNotIn('name="status"', body, "an invited participant has no status to set")

        e = self._admitted()
        body = self.client.get(reverse("participant_detail", args=[e.pk])).content.decode()
        self.assertIn('value="completed"', body)
        self.assertNotIn('value="consented"', body)


class ManualApprovalTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1", enrolment_auto_approve="0")
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="L")
        en.claim(code=self.enrolment.access_code, ip="1.1.1.1")
        self.enrolment.refresh_from_db()

    def test_consent_alone_does_not_admit_when_approval_is_manual(self):
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, Enrolment.Status.CONSENTED)
        self.assertIsNone(self.enrolment.patient_id)

    def test_approving_creates_the_client(self):
        en.record_consent(enrolment=self.enrolment, answers=_all_yes(self.study))
        self.enrolment.refresh_from_db()
        en.approve(self.enrolment)
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, Enrolment.Status.ACTIVE)
        self.assertIsNotNone(self.enrolment.patient_id)

    def test_somebody_who_never_consented_cannot_be_admitted(self):
        with self.assertRaises(ValueError):
            en.approve(self.enrolment)


class PublicFlowTests(TestCase):
    """The pages themselves, including the things a form must not let through."""

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")

    def tearDown(self):
        cache.clear()

    def test_walking_the_whole_flow(self):
        resp = self.client.post(reverse("enrolment_claim"),
                                {"access_code": self.enrolment.access_code})
        self.assertRedirects(resp, reverse("enrolment_consent"))

        resp = self.client.post(reverse("enrolment_consent"),
                                {f"item_{k}": "on" for k in _all_yes(self.study)})
        self.assertRedirects(resp, reverse("enrolment_done"))

        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, Enrolment.Status.ACTIVE)
        self.assertEqual(self.client.get(reverse("enrolment_done")).status_code, 200)

    def test_the_form_cannot_rewrite_whose_enrolment_it_is(self):
        """A leaked code must not let somebody put their own name on it."""
        self.client.post(reverse("enrolment_claim"), {
            "access_code": self.enrolment.access_code,
            "name": "Mallory", "lastname": "Intruder",
        })
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.name, "Ada")
        self.assertEqual(self.enrolment.lastname, "Lovelace")

    def test_consent_without_a_claim_goes_back_to_the_start(self):
        resp = self.client.get(reverse("enrolment_consent"))
        self.assertRedirects(resp, reverse("enrolment_landing"))

    def test_done_without_consent_goes_back_to_consent(self):
        self.client.post(reverse("enrolment_claim"),
                         {"access_code": self.enrolment.access_code})
        resp = self.client.get(reverse("enrolment_done"))
        self.assertRedirects(resp, reverse("enrolment_consent"))

    def test_resume_lands_on_the_step_they_stopped_at(self):
        resp = self.client.post(reverse("enrolment_resume"),
                                {"access_code": self.enrolment.access_code})
        self.assertRedirects(resp, reverse("enrolment_claim"))

        self.client.post(reverse("enrolment_claim"),
                         {"access_code": self.enrolment.access_code})
        resp = self.client.post(reverse("enrolment_resume"),
                                {"access_code": self.enrolment.access_code})
        self.assertRedirects(resp, reverse("enrolment_consent"))

    def test_a_rejected_claim_does_not_start_a_session(self):
        self.client.post(reverse("enrolment_claim"), {"access_code": "nope-nope-nope"})
        resp = self.client.get(reverse("enrolment_consent"))
        self.assertRedirects(resp, reverse("enrolment_landing"))

    def test_resuming_an_unclaimed_code_cannot_skip_the_claim(self):
        """Resume is not a back door past the date of birth and phone number."""
        _config(enrolment_require_dob="1")
        self.client.post(reverse("enrolment_resume"),
                         {"access_code": self.enrolment.access_code})

        resp = self.client.get(reverse("enrolment_consent"))
        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(resp["Location"], reverse("enrolment_consent"))

        self.client.post(reverse("enrolment_consent"),
                         {f"item_{k}": "on" for k in _all_yes(self.study)})
        self.assertEqual(ConsentRecord.objects.count(), 0)
        self.assertEqual(Patient.objects.count(), 0)

    def test_an_unclaimed_enrolment_in_the_session_is_sent_to_claim(self):
        session = self.client.session
        session["enrolment_id"] = self.enrolment.pk
        session.save()

        resp = self.client.get(reverse("enrolment_consent"))
        self.assertRedirects(resp, reverse("enrolment_claim"))

    def test_consenting_twice_to_the_same_version_writes_one_record(self):
        self.client.post(reverse("enrolment_claim"),
                         {"access_code": self.enrolment.access_code})
        yes = {f"item_{k}": "on" for k in _all_yes(self.study)}
        self.client.post(reverse("enrolment_consent"), yes)

        resp = self.client.get(reverse("enrolment_consent"))
        self.assertRedirects(resp, reverse("enrolment_done"))
        self.client.post(reverse("enrolment_consent"), yes)
        self.assertEqual(self.enrolment.consents.count(), 1)

    def test_a_new_version_opens_the_consent_page_again(self):
        self.client.post(reverse("enrolment_claim"),
                         {"access_code": self.enrolment.access_code})
        self.client.post(reverse("enrolment_consent"),
                         {f"item_{k}": "on" for k in _all_yes(self.study)})

        self.study.consent_version = "2.0"
        self.study.save()
        self.assertEqual(self.client.get(reverse("enrolment_consent")).status_code, 200)


class StudyScopingTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.a = _study(slug="a", display_name="Study A")
        self.b = _study(slug="b", display_name="Study B")
        en.create_enrolment(study=self.a, name="Ann", lastname="A")
        en.create_enrolment(study=self.b, name="Bob", lastname="B")

        from django.contrib.auth.models import Group
        nav_group, _created = Group.objects.get_or_create(name="Navigator")
        self.nav = ConvAIUser.objects.create_user("nav", password=PASSWORD)
        self.nav.groups.add(nav_group)

    def test_a_navigator_without_a_study_sees_everybody_waiting(self):
        self.client.force_login(self.nav)
        body = self.client.get(reverse("patients")).content.decode()
        self.assertIn("Ann", body)
        self.assertIn("Bob", body)

    def test_a_navigator_scoped_to_a_study_sees_only_that_one(self):
        self.nav.study = self.a
        self.nav.save()
        self.client.force_login(self.nav)
        body = self.client.get(reverse("patients")).content.decode()
        self.assertIn("Ann", body)
        self.assertNotIn("Bob", body)

    def test_scoping_is_enforced_on_the_detail_page_too(self):
        """Not just hidden from the list — not reachable by guessing the URL."""
        self.nav.study = self.a
        self.nav.save()
        self.client.force_login(self.nav)
        other = Enrolment.objects.get(name="Bob")
        resp = self.client.get(reverse("participant_detail", args=[other.pk]))
        self.assertEqual(resp.status_code, 404)


class ClientsPageIntegrationTests(TestCase):
    """Enrolment lives on the Clients page, because a participant is a client."""

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.admin = ConvAIUser.objects.create_user("adm2", password=PASSWORD,
                                                    is_staff=True, is_superuser=True)
        self.admin.user_permissions.add(
            Permission.objects.get(codename="access_configuration"))
        self.client.force_login(self.admin)

    def test_a_study_with_no_agent_is_flagged_in_settings(self):
        """Its participants would be answered "no agent is configured"."""
        from ConvAI.models import Agent
        body = self.client.get(reverse("config") + "?tab=participants").content.decode()
        self.assertIn("No agent set", body)

        self.study.default_agent = Agent.objects.create(
            name="Study agent", kind="native", native_key="loopback")
        self.study.save()
        body = self.client.get(reverse("config") + "?tab=participants").content.decode()
        self.assertNotIn("No agent set", body)

    def test_the_clients_page_offers_enrolment(self):
        body = self.client.get(reverse("patients")).content.decode()
        self.assertIn("Study enrolment", body)

    def test_somebody_waiting_shows_on_the_clients_page(self):
        en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        body = self.client.get(reverse("patients")).content.decode()
        self.assertIn("Ada", body)

    def test_a_consented_participant_leaves_the_waiting_list(self):
        """They are a client now, so the client table is where they belong."""
        e = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        en.claim(code=e.access_code, ip="1.1.1.1")
        e.refresh_from_db()
        en.record_consent(enrolment=e, answers=_all_yes(self.study))

        body = self.client.get(reverse("patients")).content.decode()
        self.assertNotIn(e.access_code, body,
                         "a consented participant should no longer be listed as waiting")

    def test_consent_history_appears_on_the_client_page(self):
        e = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        en.claim(code=e.access_code, ip="1.1.1.1")
        e.refresh_from_db()
        en.record_consent(enrolment=e, answers=_all_yes(self.study))
        e.refresh_from_db()

        body = self.client.get(reverse("patient_detail", args=[e.patient_id])).content.decode()
        self.assertIn(e.access_code, body)
        self.assertIn("Version", body)

    def test_a_client_who_was_never_enrolled_shows_no_study_section(self):
        patient = Patient.objects.create(name="Ordinary", lastname="Client")
        body = self.client.get(reverse("patient_detail", args=[patient.pk])).content.decode()
        self.assertNotIn("Manage enrolment", body)


class SelfRegistrationBridgeTests(TestCase):
    """The join between an unknown number messaging in and a study enrolment."""

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="L")

    def test_a_matching_code_is_recognised_and_remembers_the_number(self):
        out = en.link_phone_to_enrolment(self.enrolment.access_code, "+447700900123")
        self.assertTrue(out["ok"])
        self.assertEqual(out["name"], "Ada")
        self.assertTrue(out["needs_consent"])

        self.enrolment.refresh_from_db()
        self.assertEqual(str(self.enrolment.phone_number), "+447700900123")

    def test_it_stops_short_of_creating_a_client(self):
        """Sending a code over WhatsApp is not consent."""
        en.link_phone_to_enrolment(self.enrolment.access_code, "+447700900123")
        self.enrolment.refresh_from_db()
        self.assertIsNone(self.enrolment.patient_id)
        self.assertEqual(Patient.objects.count(), 0)

    def test_an_unknown_code_simply_does_not_match(self):
        self.assertFalse(en.link_phone_to_enrolment("nope-nope-nope", "+44770090000")["ok"])

    def test_it_does_nothing_while_the_feature_is_off(self):
        _config(study_enrolment_enabled="0")
        out = en.link_phone_to_enrolment(self.enrolment.access_code, "+447700900123")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "disabled")

    def test_guessing_codes_by_message_is_limited_per_number(self):
        """A match hands back a name, so the message route needs the same limit
        as the web form — and a different number is not caught by it."""
        for _ in range(en.service.attempt_limit()):
            en.link_phone_to_enrolment("nope-nope-nope", "+447700900500")

        out = en.link_phone_to_enrolment(self.enrolment.access_code, "+447700900500")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "locked")
        self.assertNotIn("name", out)

        self.assertTrue(en.link_phone_to_enrolment(
            self.enrolment.access_code, "+447700900501")["ok"])

    def test_an_existing_number_is_not_overwritten(self):
        self.enrolment.phone_number = "+447700900999"
        self.enrolment.save()
        en.link_phone_to_enrolment(self.enrolment.access_code, "+447700900123")
        self.enrolment.refresh_from_db()
        self.assertEqual(str(self.enrolment.phone_number), "+447700900999")


class AdminDeletionTests(TestCase):
    """Consent is evidence, including against the enrolment it hangs from."""

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.client.force_login(_admin())

    def test_an_enrolment_with_consent_cannot_be_deleted(self):
        e = en.create_enrolment(study=self.study, name="Ada", lastname="L")
        en.claim(code=e.access_code, ip="1.1.1.1")
        e.refresh_from_db()
        en.record_consent(enrolment=e, answers=_all_yes(self.study))

        url = reverse("admin:ConvAI_enrolment_delete", args=[e.pk])
        self.assertEqual(self.client.get(url, follow=True).status_code, 403)
        self.client.post(url, {"post": "yes"}, follow=True)
        self.assertEqual(ConsentRecord.objects.count(), 1)

    def test_an_enrolment_nobody_consented_to_can_be_deleted(self):
        e = en.create_enrolment(study=self.study, name="Typo", lastname="Entry")
        url = reverse("admin:ConvAI_enrolment_delete", args=[e.pk])
        self.client.post(url, {"post": "yes"}, follow=True)
        self.assertFalse(Enrolment.objects.filter(pk=e.pk).exists())


class EnrolmentApiTests(TestCase):
    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1")
        self.study = _study()
        self.enrolment = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        self.enrolment.phone_number = "+447700900123"
        self.enrolment.save()

        self.user = ConvAIUser.objects.create_user("api", password=PASSWORD, is_staff=True)
        self.token = Token.objects.create(user=self.user)

    def _get(self, query, auth=True):
        # "Token", not "Bearer": ConvAI.api.views prefers a custom Bearer class
        # and falls back to DRF's TokenAuthentication when it is absent, which is
        # the case here. Same note as test_conversation_privacy.
        kwargs = {"HTTP_AUTHORIZATION": f"Token {self.token.key}"} if auth else {}
        return self.client.get(reverse("api_enrolments") + query, **kwargs)

    def test_lookup_by_phone(self):
        body = self._get("?phone=%2B447700900123").json()
        self.assertTrue(body["enrolled"])
        self.assertEqual(body["name"], "Ada")
        self.assertEqual(body["study"], "trial")

    def test_lookup_by_code(self):
        self.assertTrue(self._get(f"?code={self.enrolment.access_code}").json()["enrolled"])

    def test_an_unknown_person_is_simply_not_enrolled(self):
        self.assertFalse(self._get("?phone=%2B447700900999").json()["enrolled"])

    def test_the_access_code_is_never_returned(self):
        """A token that can look people up must not become one that can
        impersonate them."""
        body = self._get("?phone=%2B447700900123").json()
        self.assertNotIn("access_code", body)
        self.assertNotIn(self.enrolment.access_code, str(body))

    def test_it_needs_a_token(self):
        self.assertIn(self._get("?phone=%2B447700900123", auth=False).status_code, (401, 403))

    def test_it_404s_while_the_feature_is_off(self):
        _config(study_enrolment_enabled="0")
        self.assertEqual(self._get("?phone=%2B447700900123").status_code, 404)

    def test_a_query_with_neither_parameter_is_a_bad_request(self):
        self.assertEqual(self._get("").status_code, 400)


class NoTemplateLeakTests(TestCase):
    """Template syntax must never reach the page as text.

    Django's ``{# … #}`` is single-line only: spread over two lines it is not a
    comment at all, and prints verbatim to whoever is looking — which is how a
    note about the consent editor's hidden field once ended up in the middle of
    the study page. Multi-line notes need ``{% comment %}``.
    """

    # Scripts and styles may legitimately hold braces, and so may attributes —
    # Django admin keeps JSON in ``data-`` attributes. Comment and tag markers
    # have no business anywhere else; variable braces are checked in the text a
    # reader actually sees.
    _CODE_BLOCKS = r"<(script|style)\b.*?</\1>"
    _TAG_LEAKS = ("{#", "#}", "{%", "%}")
    _TEXT_LEAKS = ("{{", "}}")

    def setUp(self):
        cache.clear()
        _config(study_enrolment_enabled="1", enrolment_require_dob="1")
        self.study = _study(require_phone=True, pis_url="https://example.org/pis.pdf")
        self.admin = _admin()

    def tearDown(self):
        cache.clear()

    def _assert_clean(self, url, body):
        import re
        markup = re.sub(self._CODE_BLOCKS, "", body, flags=re.S | re.I)
        text = re.sub(r"<[^>]*>", "", markup)
        for token in self._TAG_LEAKS:
            self.assertNotIn(token, markup, f"{token!r} leaked into {url}")
        for token in self._TEXT_LEAKS:
            self.assertNotIn(token, text, f"{token!r} leaked into {url}")

    def _get(self, url):
        resp = self.client.get(url, follow=True)
        self.assertEqual(resp.status_code, 200, url)
        self._assert_clean(url, resp.content.decode())

    def test_no_template_has_a_comment_spread_over_lines(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent / "templates"
        offenders = []
        for path in root.rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for m in re.finditer(r"\{#(.*?)#\}", text, re.S):
                if "\n" in m.group(1):
                    line = text[:m.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(root)}:{line}")
        self.assertEqual(offenders, [], "use {% comment %} for multi-line notes")

    def test_the_public_pages_render_clean(self):
        waiting = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        for name in ("enrolment_landing", "enrolment_claim", "enrolment_resume"):
            self._get(reverse(name))

        self.client.post(reverse("enrolment_claim"), {
            "access_code": waiting.access_code,
            "date_of_birth": "1990-01-01",
            "phone_number": "+447700900001",
        })
        self._get(reverse("enrolment_consent"))
        self.client.post(reverse("enrolment_consent"),
                         {f"item_{k}": "on" for k in _all_yes(self.study)})
        self._get(reverse("enrolment_done"))

    def test_the_locked_out_pages_render_clean(self):
        for _ in range(en.service.attempt_limit()):
            en.note_failed_attempt("127.0.0.1")
        self._get(reverse("enrolment_claim"))
        self._get(reverse("enrolment_resume"))

    def test_the_staff_pages_render_clean(self):
        self.client.force_login(self.admin)
        waiting = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        admitted = en.create_enrolment(study=self.study, name="Grace", lastname="Hopper",
                                       phone_number="+447700900002")
        en.claim(code=admitted.access_code, date_of_birth=dt.date(1990, 1, 1),
                 phone_number="+447700900002", ip="1.1.1.1")
        admitted.refresh_from_db()
        en.record_consent(enrolment=admitted, answers=_all_yes(self.study))
        admitted.refresh_from_db()

        self._get(reverse("patients"))
        self._get(reverse("patient_detail", args=[admitted.patient_id]))
        self._get(reverse("participant_detail", args=[waiting.pk]))
        self._get(reverse("config") + "?tab=participants")
        # Once somebody has consented the wording locks, and the editor renders
        # differently; this study is locked, a fresh one is not.
        self._get(reverse("study_editor", args=[self.study.pk]))
        fresh = _study(slug="fresh", display_name="Fresh")
        self._get(reverse("study_editor", args=[fresh.pk]))

    def test_the_admin_pages_render_clean(self):
        self.client.force_login(self.admin)
        e = en.create_enrolment(study=self.study, name="Ada", lastname="Lovelace")
        en.claim(code=e.access_code, date_of_birth=dt.date(1990, 1, 1),
                 phone_number="+447700900003", ip="1.1.1.1")
        e.refresh_from_db()
        record = en.record_consent(enrolment=e, answers=_all_yes(self.study))
        for model, pk in (("study", self.study.pk), ("enrolment", e.pk),
                          ("consentrecord", record.pk)):
            self._get(reverse(f"admin:ConvAI_{model}_changelist"))
            self._get(reverse(f"admin:ConvAI_{model}_change", args=[pk]))
