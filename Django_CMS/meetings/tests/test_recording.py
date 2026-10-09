"""From segments to one CallRecording with named speakers."""
import os
import tempfile
from unittest import mock

from django.test import override_settings

from ConvAI.models import CallRecording
from meetings import rooms
from meetings.job_handlers import finalize_recording
from meetings.models import RecordingSegment
from meetings.recording import mixdown, session_dir

from .base import MeetingsTestCase


class Mixdown(MeetingsTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.o = override_settings(CALL_RECORDINGS_DIR=self.tmp.name)
        self.o.enable(); self.addCleanup(self.o.disable)
        self.session = rooms.start(self.meeting, self.nav)
        d = session_dir(self.session)
        os.makedirs(d)
        for i, (ident, label, role, start) in enumerate([
                ("staff-1", "Sam Navigator", "navigator", 1_000),
                ("inv-x", "Ana", "caregiver", 3_500),
                ("agent-interviewer-1", "AI interviewer", "interviewer", 5_000),
                ("inv-x", "Ana", "caregiver", 70_000)]):   # Ana again after a reconnect
            p = os.path.join(d, f"{i}.ogg")
            open(p, "wb").write(b"x")
            RecordingSegment.objects.create(session=self.session, identity=ident, label=label,
                                            role=role, track_sid=f"TR_{i}", seq=0, path=p,
                                            start_utc_ms=start, end_utc_ms=start + 60_000)
        rooms.end(self.session)
        self.session.refresh_from_db()

    def test_one_recording_with_named_speakers_and_offsets(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("subprocess.run") as run:
            rec = mixdown(self.session)
        graph = run.call_args.args[0][run.call_args.args[0].index("-filter_complex") + 1]
        self.assertIn("amix=inputs=4", graph)
        self.assertIn("adelay=2500", graph)
        self.assertEqual(rec.source, CallRecording.Source.ONLINE)
        self.assertEqual(rec.meeting_id, self.meeting.pk)
        self.assertEqual(rec.patient_id, self.patient.pk)
        self.assertEqual(rec.speakers["2"], {"label": "Ana", "role": "caregiver"})
        self.assertEqual(rec.speakers["3"]["role"], "assistant")
        ana = [t for t in rec.tracks if t["speaker"] == 2]
        self.assertEqual([t["offset_s"] for t in ana], [2.5, 69.0])
        self.assertTrue(rec.recording_sid.startswith("lk-"))

    def test_rerun_updates_rather_than_duplicates(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), mock.patch("subprocess.run"):
            mixdown(self.session)
            mixdown(self.session)
        self.assertEqual(CallRecording.objects.count(), 1)

    def test_finalize_queues_transcription(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), mock.patch("subprocess.run"), \
             mock.patch("ConvAI.summarization.queue_transcription") as q:
            out = finalize_recording({"session_id": self.session.pk})
        self.assertTrue(out["recorded"])
        q.assert_called_once()

    def test_nothing_recorded(self):
        RecordingSegment.objects.all().delete()
        self.assertIsNone(mixdown(self.session))


class Adoption(MeetingsTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.o = override_settings(CALL_RECORDINGS_DIR=self.tmp.name)
        self.o.enable(); self.addCleanup(self.o.disable)
        self.session = rooms.start(self.meeting, self.nav)
        self.dir = session_dir(self.session)
        os.makedirs(self.dir)

    def _file(self, name, meta):
        import json
        open(os.path.join(self.dir, name + ".ogg"), "wb").write(b"OggS")
        with open(os.path.join(self.dir, name + ".json"), "w") as fh:
            json.dump(meta, fh)

    def test_described_but_unregistered_files_are_adopted(self):
        from meetings.recording import adopt_unregistered
        self._file("inv-x-TR_1-000", {"identity": "inv-x", "label": "Ana", "role": "caregiver",
                                      "track_sid": "TR_1", "seq": 0, "start_utc_ms": 5000,
                                      "end_utc_ms": 65000})
        self.assertEqual(adopt_unregistered(self.session), 1)
        self.assertEqual(adopt_unregistered(self.session), 0)  # once only
        seg = RecordingSegment.objects.get()
        self.assertEqual((seg.label, seg.role, seg.end_utc_ms), ("Ana", "caregiver", 65000))

    def test_a_crashed_file_gets_its_end_from_its_length(self):
        from meetings.recording import adopt_unregistered
        self._file("inv-x-TR_2-000", {"identity": "inv-x", "track_sid": "TR_2", "seq": 0,
                                      "start_utc_ms": 1000, "end_utc_ms": None})
        with mock.patch("ConvAI.utils._media_duration", return_value=12.5):
            adopt_unregistered(self.session)
        self.assertEqual(RecordingSegment.objects.get().end_utc_ms, 13500)

    def test_mixdown_includes_adopted_segments(self):
        self._file("inv-x-TR_3-000", {"identity": "inv-x", "label": "Ana", "role": "caregiver",
                                      "track_sid": "TR_3", "seq": 0, "start_utc_ms": 0,
                                      "end_utc_ms": 10000})
        rooms.end(self.session)
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), mock.patch("subprocess.run"):
            rec = mixdown(self.session)
        self.assertEqual(rec.speakers["1"]["label"], "Ana")
