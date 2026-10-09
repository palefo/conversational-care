"""The API the agents call: three locks, and an agent can only touch its own
meeting."""
import json
import os
import tempfile

from django.test import override_settings
from django.urls import reverse

from ConvAI.models import Answer, Meeting
from meetings import agent_tokens, rooms
from meetings.models import AgentRun, RecordingSegment
from meetings.recording import session_dir

from .base import MeetingsTestCase


class InternalAPI(MeetingsTestCase):
    def setUp(self):
        super().setUp()
        self.session = rooms.start(self.meeting, self.nav)
        self.run = rooms.start_interview(self.session, protocol=self.protocol,
                                         respondent_identity="inv-x", respondent_name="Ana",
                                         language="es-pe", by=self.nav)
        self.token = agent_tokens.mint(self.run)

    def _h(self, token=None, key="svc-key"):
        h = {"HTTP_AUTHORIZATION": f"Bearer {token or self.token}"}
        if key is not None:
            h["HTTP_X_CC_SERVICE_KEY"] = key
        return h

    def test_needs_the_service_key(self):
        url = reverse("meetings:internal_run")
        self.assertEqual(self.client.get(url, **self._h(key=None)).status_code, 401)
        self.assertEqual(self.client.get(url, **self._h(key="nope")).status_code, 401)

    def test_needs_a_valid_token_with_the_scope(self):
        url = reverse("meetings:internal_questions")
        self.assertEqual(self.client.get(url, **self._h(token="forged")).status_code, 403)
        scribe = AgentRun.objects.get(role=AgentRun.Role.SCRIBE)
        self.assertEqual(self.client.get(url, **self._h(token=agent_tokens.mint(scribe))).status_code, 403)

    def test_stopped_runs_are_refused(self):
        rooms.stop_run(self.run)
        self.assertEqual(self.client.get(reverse("meetings:internal_run"), **self._h()).status_code, 410)

    def test_run_config_for_the_interviewer(self):
        with override_settings(AZURE_REALTIME_ENDPOINT="https://r.openai.azure.com",
                               AZURE_REALTIME_API_KEY="k", AZURE_REALTIME_DEPLOYMENT="gpt-realtime"):
            data = self.client.get(reverse("meetings:internal_run"), **self._h()).json()
        self.assertEqual(data["role"], "interviewer")
        self.assertEqual(data["respondent_identity"], "inv-x")
        self.assertEqual(data["protocol"]["number"], 1)
        self.assertIn("interviewer", data["instructions"].lower())
        self.assertEqual(data["realtime"]["deployment"], "gpt-realtime")

    def test_questions_and_voice_answers(self):
        q = self.client.get(reverse("meetings:internal_questions"), **self._h()).json()
        self.assertEqual(q["total"], 2)
        self.assertEqual(q["questions"][0]["prompt"], "How is sleep?")
        r = self.client.post(reverse("meetings:internal_answer"),
                             json.dumps({"question_id": self.q1.pk, "response": "Sleeps 6h"}),
                             content_type="application/json", **self._h()).json()
        self.assertTrue(r["ok"])
        a = Answer.objects.get(question=self.q1)
        self.assertEqual((a.meeting_id, a.source), (self.meeting.pk, Answer.Source.VOICE))
        self.run.refresh_from_db()
        self.assertEqual(self.run.answers_saved, 1)

    def test_cannot_answer_outside_its_protocol(self):
        from ConvAI.models import Protocol, Question
        other = Protocol.objects.create(number=2, title="Other")
        q = Question.objects.create(protocol=other, order=1, prompt_md="?")
        r = self.client.post(reverse("meetings:internal_answer"),
                             json.dumps({"question_id": q.pk, "response": "x"}),
                             content_type="application/json", **self._h()).json()
        self.assertFalse(r["ok"])
        self.assertFalse(Answer.objects.exists())

    def test_status_reports(self):
        self.client.post(reverse("meetings:internal_status"),
                         json.dumps({"state": "running", "identity": "agent-interviewer-1"}),
                         content_type="application/json", **self._h())
        self.run.refresh_from_db()
        self.assertEqual((self.run.state, self.run.agent_identity), ("running", "agent-interviewer-1"))


class RecordingSegments(MeetingsTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from django.test import override_settings as o
        self.o = o(CALL_RECORDINGS_DIR=self.tmp.name)
        self.o.enable(); self.addCleanup(self.o.disable)
        self.session = rooms.start(self.meeting, self.nav)
        self.scribe = AgentRun.objects.get(role=AgentRun.Role.SCRIBE)
        self.h = {"HTTP_AUTHORIZATION": f"Bearer {agent_tokens.mint(self.scribe)}",
                  "HTTP_X_CC_SERVICE_KEY": "svc-key"}

    def _post(self, path):
        return self.client.post(reverse("meetings:internal_segment"), json.dumps({
            "identity": "inv-x", "label": "Ana", "role": "caregiver", "track_sid": "TR_1",
            "seq": 0, "path": path, "start_utc_ms": 1000, "end_utc_ms": 61000}),
            content_type="application/json", **self.h)

    def test_segment_inside_the_session_folder(self):
        d = session_dir(self.session)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "ana-0.ogg")
        open(p, "wb").write(b"OggS")
        self.assertEqual(self._post(p).status_code, 200)
        self.assertEqual(self._post(p).status_code, 200)  # idempotent
        self.assertEqual(RecordingSegment.objects.count(), 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.recording_state, "on")

    def test_paths_outside_are_refused(self):
        outside = os.path.join(self.tmp.name, "elsewhere.ogg")
        open(outside, "wb").close()
        self.assertEqual(self._post(outside).status_code, 400)
        self.assertEqual(self._post(os.path.join(session_dir(self.session), "..", "..", "x.ogg")).status_code, 400)


class LateSegments(RecordingSegments):
    def test_last_segments_are_accepted_just_after_the_end(self):
        d = session_dir(self.session)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "ana-0.ogg")
        open(p, "wb").write(b"OggS")
        rooms.end(self.session, reason="test")
        self.assertEqual(self._post(p).status_code, 200)
        self.assertEqual(RecordingSegment.objects.count(), 1)

    def test_but_not_long_after(self):
        from datetime import timedelta
        from django.utils import timezone
        from meetings.models import MeetingSession
        d = session_dir(self.session)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "ana-0.ogg")
        open(p, "wb").write(b"OggS")
        rooms.end(self.session, reason="test")
        MeetingSession.objects.filter(pk=self.session.pk).update(ended_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self._post(p).status_code, 410)

    def test_other_roles_get_no_grace(self):
        run = rooms.start_interview(self.session, protocol=self.protocol, respondent_identity="inv-x",
                                    respondent_name="Ana", language="", by=self.nav)
        rooms.end(self.session, reason="test")
        h = {"HTTP_AUTHORIZATION": f"Bearer {agent_tokens.mint(run)}", "HTTP_X_CC_SERVICE_KEY": "svc-key"}
        resp = self.client.post(reverse("meetings:internal_answer"),
                                json.dumps({"question_id": self.q1.pk, "response": "x"}),
                                content_type="application/json", **h)
        self.assertEqual(resp.status_code, 410)
