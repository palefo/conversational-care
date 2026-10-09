"""Protocol answers written by people, text and voice at the same time.

The panel used to post the whole protocol and delete any answer that arrived
blank — so a panel opened before an answer came in by text (or, now, from the
voice interviewer) erased it on the next save.

    python3 manage.py test ConvAI.test_answer_concurrency --settings=test_settings
"""
import importlib

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ConvAI.forms import ProtocolAnswerForm
from ConvAI.models import Answer, ConvAIUser, Meeting, Patient, Protocol, Question
from ConvAI.native_agents.protocol_qa import _save_answer


class Concurrency(TestCase):
    def setUp(self):
        self.nav = ConvAIUser.objects.create_user(username="nav", password="x")
        self.nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.p = Patient.objects.create(name="Manuel", lastname="Ortega", navigator=self.nav)
        self.m = Meeting.objects.create(patient=self.p, scheduled_time=timezone.now())
        self.proto = Protocol.objects.create(number=1, title="Check-in")
        self.q1 = Question.objects.create(protocol=self.proto, order=1, prompt_md="Sleep?")
        self.q2 = Question.objects.create(protocol=self.proto, order=2, prompt_md="Meals?")

    def _form(self, data):
        f = ProtocolAnswerForm(meeting=self.m, protocol=self.proto, data=data)
        self.assertTrue(f.is_valid(), f.errors)
        return f

    def test_unposted_field_is_left_alone(self):
        _save_answer(1, self.m.pk, self.q2.pk, "Eats well", source="voice")
        f = self._form({f"q_{self.q1.pk}": "Sleeps badly", f"base_q_{self.q1.pk}": ""})
        f.save()
        self.assertEqual(Answer.objects.get(question=self.q2).response, "Eats well")
        self.assertEqual(Answer.objects.get(question=self.q1).source, Answer.Source.NAVIGATOR)

    def test_stale_blank_does_not_delete(self):
        # Panel rendered with q2 empty; the interviewer then saved it; the
        # panel posts q2 still blank with base "".
        _save_answer(1, self.m.pk, self.q2.pk, "Eats well", source="voice")
        f = self._form({f"q_{self.q2.pk}": "", f"base_q_{self.q2.pk}": ""})
        f.save()
        self.assertEqual(Answer.objects.get(question=self.q2).response, "Eats well")
        self.assertEqual(f.conflicts[0]["current"], "Eats well")

    def test_stale_value_does_not_overwrite(self):
        _save_answer(1, self.m.pk, self.q1.pk, "Sleeps 6h", source="text")
        f = self._form({f"q_{self.q1.pk}": "Sleeps fine", f"base_q_{self.q1.pk}": ""})
        written = f.save()
        self.assertEqual(written, [])
        self.assertEqual(Answer.objects.get(question=self.q1).response, "Sleeps 6h")
        self.assertEqual(f.conflicts[0]["source"], "text")

    def test_matching_base_writes_and_takes_ownership(self):
        _save_answer(1, self.m.pk, self.q1.pk, "Sleeps 6h", source="voice")
        f = self._form({f"q_{self.q1.pk}": "Sleeps 6h, wakes at night",
                        f"base_q_{self.q1.pk}": "Sleeps 6h"})
        self.assertEqual(f.save(), [f"q_{self.q1.pk}"])
        a = Answer.objects.get(question=self.q1)
        self.assertEqual(a.source, Answer.Source.NAVIGATOR)
        self.assertFalse(a.by_text)

    def test_deliberate_clear_with_matching_base_deletes(self):
        _save_answer(1, self.m.pk, self.q1.pk, "Wrong", source="text")
        f = self._form({f"q_{self.q1.pk}": "", f"base_q_{self.q1.pk}": "Wrong"})
        f.save()
        self.assertFalse(Answer.objects.filter(question=self.q1).exists())

    def test_voice_and_text_sources(self):
        _save_answer(1, self.m.pk, self.q1.pk, "A", source="voice")
        a = Answer.objects.get(question=self.q1)
        self.assertEqual((a.source, a.by_text), (Answer.Source.VOICE, False))
        _save_answer(1, self.m.pk, self.q1.pk, "B")
        a.refresh_from_db()
        self.assertEqual((a.source, a.by_text), (Answer.Source.TEXT, True))

    def test_panel_save_reports_conflicts_as_json(self):
        self.client.force_login(self.nav)
        _save_answer(1, self.m.pk, self.q1.pk, "From voice", source="voice")
        resp = self.client.post(
            reverse("protocol_view", args=[self.m.pk, 1]),
            {f"q_{self.q1.pk}": "Typed", f"base_q_{self.q1.pk}": ""},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        data = resp.json()
        self.assertEqual(data["conflicts"][0]["current"], "From voice")

    def test_backfill_marks_text_answers(self):
        Answer.objects.create(meeting=self.m, question=self.q1, response="x", by_text=True)
        mig = importlib.import_module("ConvAI.migrations.0097_jobs_and_online_seams")
        from django.apps import apps
        mig.backfill_answer_source(apps, None)
        self.assertEqual(Answer.objects.get(question=self.q1).source, Answer.Source.TEXT)
