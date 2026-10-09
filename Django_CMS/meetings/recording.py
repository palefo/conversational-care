"""From recorded segments to one CallRecording the rest of the platform reads.

The recorder (meeting_agents/cc_agents/scribe.py) writes one Ogg/Opus file per
person per stretch of audio and registers each as a RecordingSegment. After
the room closes, ``mixdown`` lays them on one timeline:

* one **mixed MP3** for listening (Safari does not reliably play Ogg/Opus),
* the **per-speaker files** kept as ``CallRecording.tracks`` — what the
  transcript is built from, one clean voice at a time, which is better than
  any attempt to separate voices from the mix afterwards,
* the **speakers' names and roles** as ``CallRecording.speakers``, so the
  transcript says "Ana (caregiver)" rather than "Speaker 2".

The CallRecording belongs to the core app; nothing here is needed to play or
re-transcribe it later.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone as dt_timezone

from django.conf import settings

logger = logging.getLogger(__name__)


def session_dir(session) -> str:
    base = getattr(settings, "CALL_RECORDINGS_DIR", os.path.join(settings.MEDIA_ROOT, "call_recordings"))
    return os.path.join(base, "meetings", session.uuid.hex)


ROLE_LABEL = {
    "navigator": "navigator",
    "caregiver": "caregiver",
    "client": "client",
    "other": "guest",
    "interviewer": "assistant",
    "assistant": "assistant",
}


def _speakers(segments):
    """Number the people in order of first speaking, with their label and role."""
    order, info = [], {}
    for seg in segments:
        if seg.identity not in info:
            order.append(seg.identity)
            info[seg.identity] = {
                "label": seg.label or seg.identity,
                "role": ROLE_LABEL.get(seg.role, seg.role or ""),
            }
    return {identity: (i + 1, info[identity]) for i, identity in enumerate(order)}


def adopt_unregistered(session) -> int:
    """Register segments the recorder wrote but never handed in.

    The recorder describes each file in a ``.json`` sidecar beside it as soon as
    the file starts. A recorder shut down mid-registration, or one that
    crashed, leaves files that exist and are described but are not rows; this
    turns them into rows so the mixdown includes them. Returns how many.
    """
    import json

    from .models import RecordingSegment

    base = os.path.realpath(session_dir(session))
    if not os.path.isdir(base):
        return 0
    adopted = 0
    for name in sorted(os.listdir(base)):
        if not name.endswith(".json"):
            continue
        audio = os.path.join(base, name[:-5] + ".ogg")
        if not os.path.isfile(audio) or os.path.getsize(audio) == 0:
            continue
        try:
            with open(os.path.join(base, name)) as fh:
                meta = json.load(fh)
            start = int(meta["start_utc_ms"])
            key = dict(session=session, identity=str(meta["identity"])[:96],
                       track_sid=str(meta["track_sid"])[:64], seq=int(meta.get("seq") or 0))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if RecordingSegment.objects.filter(**key).exists():
            continue
        end = meta.get("end_utc_ms")
        if not end:
            # Never closed (a crash): the file's own length says where it ends.
            from ConvAI.utils import _media_duration
            seconds = _media_duration(audio)
            end = start + int(seconds * 1000) if seconds else None
        RecordingSegment.objects.create(
            **key, label=str(meta.get("label") or "")[:120], role=str(meta.get("role") or "")[:20],
            path=audio, codec=str(meta.get("codec") or "ogg/opus")[:20],
            start_utc_ms=start, end_utc_ms=end, bytes=os.path.getsize(audio))
        adopted += 1
    if adopted:
        logger.info("Adopted %d unregistered segment(s) for session %s", adopted, session.pk)
    return adopted


def mixdown(session):
    """Build (or rebuild) the session's CallRecording. Returns it, or None if
    nothing was recorded."""
    from ConvAI.models import CallRecording

    from .models import MeetingSession

    adopt_unregistered(session)
    segments = [s for s in session.segments.all() if s.path and os.path.exists(s.path)]
    if not segments:
        logger.info("Session %s has no recorded audio.", session.pk)
        return None
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed, so the meeting cannot be mixed down.")

    t0 = min(s.start_utc_ms for s in segments)
    t_end = max((s.end_utc_ms or s.start_utc_ms) for s in segments)
    speakers = _speakers(segments)
    out_dir = session_dir(session)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "mix.mp3")

    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    filters, labels = [], []
    for i, seg in enumerate(segments):
        cmd += ["-i", seg.path]
        delay = max(0, seg.start_utc_ms - t0)
        filters.append(f"[{i}:a]aresample=24000,adelay={delay}:all=1[a{i}]")
        labels.append(f"[a{i}]")
    if len(segments) == 1:
        graph = filters[0] + f";[a0]alimiter=limit=0.95[out]"
    else:
        graph = ";".join(filters) + ";" + "".join(labels) + (
            f"amix=inputs={len(segments)}:normalize=0:dropout_transition=0,"
            "alimiter=limit=0.95[out]")
    cmd += ["-filter_complex", graph, "-map", "[out]", "-ac", "1", "-ar", "24000",
            "-b:a", "48k", out]
    subprocess.run(cmd, check=True, timeout=1800)

    tracks = [{
        "speaker": speakers[s.identity][0],
        "path": s.path,
        "offset_s": round((s.start_utc_ms - t0) / 1000.0, 3),
        "label": speakers[s.identity][1]["label"],
        "role": speakers[s.identity][1]["role"],
    } for s in segments]
    speaker_map = {str(num): meta for num, meta in speakers.values()}

    meeting = session.meeting
    start = datetime.fromtimestamp(t0 / 1000.0, tz=dt_timezone.utc)
    end = datetime.fromtimestamp(t_end / 1000.0, tz=dt_timezone.utc)
    recording, _created = CallRecording.objects.update_or_create(
        recording_sid=f"lk-{session.uuid.hex}",
        defaults={
            "source": CallRecording.Source.ONLINE,
            "meeting": meeting,
            "patient": meeting.patient,
            "from_number": "",
            "to_number": "",
            "start_time": start,
            "end_time": end,
            "duration": max(1, int(round((t_end - t0) / 1000.0))),
            "filename": out,
            "tracks": tracks,
            "speakers": speaker_map,
            "call_sid": "",
        },
    )
    MeetingSession.objects.filter(pk=session.pk).update(recording=recording)
    return recording
