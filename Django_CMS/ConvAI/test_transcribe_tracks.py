"""Transcription in the background, by speaker, without asking Twilio about
recordings it did not make, and past Whisper's size limit.

    python3 manage.py test ConvAI.test_transcribe_tracks --settings=test_settings
"""
import os
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ConvAI import utils
from ConvAI.models import CallRecording, Caregiver, ConvAIUser, Job, Meeting, Patient
from ConvAI.summarization import recognition_hint, transcribe_recording, transcription_state


def _words(*items):
    return [{"start": s, "end": s + 0.4, "word": w} for s, w in items]


class TracksInterleave(TestCase):
    def test_offsets_and_speaker_order(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.ogg"), os.path.join(d, "b.ogg")
            open(a, "wb").close(); open(b, "wb").close()
            fake = {
                a: ("hello there", [{"start": 0.0, "end": 1.0, "text": "hello there", "speaker": None}],
                    _words((0.0, "Hello"), (0.5, "there."))),
                b: ("hi", [{"start": 0.0, "end": 0.5, "text": "hi", "speaker": None}],
                    _words((0.0, "Hi!"))),
            }
            with mock.patch.object(utils, "_whisper", side_effect=lambda p, c, prompt=None: fake[p]), \
                 mock.patch.object(utils, "_openai_client", return_value=object()):
                text, segments = utils.transcribe_tracks([(1, a, 0.0), (2, b, 2.0)])
        self.assertEqual([s["speaker"] for s in segments], [1, 2])
        self.assertEqual(segments[0]["text"], "Hello there.")
        self.assertAlmostEqual(segments[1]["start"], 2.0)
        self.assertIn("Hi!", text)

    def test_missing_files_are_skipped(self):
        with mock.patch.object(utils, "_whisper") as w, \
             mock.patch.object(utils, "_openai_client", return_value=object()):
            text, segments = utils.transcribe_tracks([(1, "/nope.ogg", 0)])
        w.assert_not_called()
        self.assertEqual(segments, [])


class WhisperHallucinations(TestCase):
    def test_silence_segments_are_dropped(self):
        result = SimpleNamespace(
            text="Real words. Thanks for watching!",
            segments=[
                {"start": 0, "end": 2, "text": "Real words.", "no_speech_prob": 0.01, "avg_logprob": -0.2},
                {"start": 30, "end": 32, "text": "Thanks for watching!", "no_speech_prob": 0.9, "avg_logprob": -1.4},
            ],
            words=[{"start": 0.1, "end": 0.5, "word": "Real"}, {"start": 0.6, "end": 1.0, "word": "words."},
                   {"start": 30.2, "end": 30.6, "word": "Thanks"}],
        )
        client = mock.MagicMock()
        client.audio.transcriptions.create.return_value = result
        with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
            text, segments, words = utils._whisper_once(f.name, client)
        self.assertEqual(text, "Real words.")
        self.assertEqual(len(segments), 1)
        self.assertEqual([w["word"] for w in words], ["Real", "words."])


class WhisperChunking(TestCase):
    def test_large_files_are_cut_and_stitched(self):
        calls = []

        def fake_once(path, client, prompt=None):
            calls.append(path)
            i = len(calls) - 1
            # Each window hears one word of its own; every window after the
            # first also re-hears the 2 s overlap it started early for.
            words = _words((1.0, f"w{i}")) if not i else (
                _words((0.5, "overlap")) + _words((3.0, f"w{i}")))
            segs = [{"start": w["start"], "end": w["end"], "text": w["word"], "speaker": None} for w in words]
            return " ".join(w["word"] for w in words), segs, words

        with tempfile.NamedTemporaryFile(suffix=".mp3") as f, \
             mock.patch.object(utils, "WHISPER_MAX_BYTES", 0), \
             mock.patch.object(utils, "_media_duration", return_value=1500.0), \
             mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run") as run, \
             mock.patch.object(utils, "_whisper_once", side_effect=fake_once):
            f.write(b"x"); f.flush()
            text, segments, words = utils._whisper(f.name, client=object())
        self.assertEqual(len(calls), 3)            # 0–600, 600–1200, 1200–1500
        self.assertEqual(run.call_count, 3)
        starts = [w["start"] for w in words]
        self.assertEqual(starts, sorted(starts))
        self.assertNotIn("overlap", [w["word"] for w in words])
        # Window 2 starts 2 s early (598 s); its word at 3 s is at 601 s.
        self.assertAlmostEqual(words[1]["start"], 601.0, places=2)


class OnlineRecordings(TestCase):
    def setUp(self):
        self.carer = Caregiver.objects.create(name="Ana", lastname="Ortega")
        self.p = Patient.objects.create(name="Manuel", lastname="Ortega", caregiver=self.carer)
        self.m = Meeting.objects.create(patient=self.p, scheduled_time=timezone.now(),
                                        modality=Meeting.Modality.ONLINE)
        self.tmp = tempfile.TemporaryDirectory()
        self.track = os.path.join(self.tmp.name, "ana-0.ogg")
        open(self.track, "wb").close()
        self.mix = os.path.join(self.tmp.name, "mix.mp3")
        open(self.mix, "wb").close()
        self.rec = CallRecording.objects.create(
            recording_sid="lk-abc", start_time=timezone.now(), end_time=timezone.now(),
            duration=60, filename=self.mix, source=CallRecording.Source.ONLINE,
            meeting=self.m, patient=self.p,
            tracks=[{"speaker": 1, "path": self.track, "offset_s": 0.0}],
            speakers={"1": {"label": "Ana", "role": "caregiver"}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_uses_tracks_and_never_asks_twilio(self):
        with mock.patch("ConvAI.utils.transcribe_tracks", return_value=("hola", [{"start": 0, "end": 1, "text": "hola", "speaker": 1}])) as tt, \
             mock.patch("ConvAI.utils.requests.get") as get, \
             mock.patch("ConvAI.utils.transcribe_audio") as ta:
            transcribe_recording(self.rec)
        tt.assert_called_once()
        get.assert_not_called()
        ta.assert_not_called()
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.transcript, "hola")

    def test_hint_comes_from_the_recordings_own_client(self):
        self.assertIn("Ana", recognition_hint(self.rec))
        self.assertIn("Manuel", recognition_hint(self.rec))


def _navigator(username="nav"):
    user = ConvAIUser.objects.create_user(username=username, password="x")
    user.groups.add(Group.objects.get_or_create(name="Navigator")[0])
    return user


class TranscribeButtonQueues(TestCase):
    def setUp(self):
        self.nav = _navigator()
        self.p = Patient.objects.create(name="Manuel", lastname="Ortega", navigator=self.nav)
        self.rec = CallRecording.objects.create(
            recording_sid="RE123", start_time=timezone.now(), end_time=timezone.now(),
            duration=30, filename="/tmp/x.mp3", patient=self.p)
        self.client.force_login(self.nav)

    @override_settings(JOBS_EAGER=False, JOBS_RUNNER="worker")
    def test_button_queues_instead_of_running(self):
        with mock.patch("ConvAI.summarization.transcribe_and_summarize_recording") as run:
            resp = self.client.post(reverse("transcribe_recording", args=["RE123"]))
        self.assertEqual(resp.status_code, 302)
        run.assert_not_called()
        job = Job.objects.get()
        self.assertEqual(job.kind, "transcribe_recording")
        self.assertEqual(job.ref, f"callrecording:{self.rec.pk}")
        st = transcription_state(self.rec)
        self.assertTrue(st["live"])
        self.assertTrue(st["no_worker"])

    @override_settings(JOBS_EAGER=False, JOBS_RUNNER="worker")
    def test_pressing_twice_queues_once(self):
        self.client.post(reverse("transcribe_recording", args=["RE123"]))
        self.client.post(reverse("transcribe_recording", args=["RE123"]))
        self.assertEqual(Job.objects.count(), 1)

    @override_settings(JOBS_EAGER=False, JOBS_RUNNER="worker")
    def test_status_endpoint(self):
        self.client.post(reverse("transcribe_recording", args=["RE123"]))
        data = self.client.get(reverse("transcription_status", args=["RE123"])).json()
        self.assertEqual(data["status"], "queued")
        other = _navigator("other")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("transcription_status", args=["RE123"])).status_code, 403)
