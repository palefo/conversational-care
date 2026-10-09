"""Background job handlers owned by the core app.

Imported only by something about to run jobs (the ``run_jobs`` worker, or the
web process in eager/thread mode) — see ``ConvAI.jobs``.
"""
from __future__ import annotations

from .jobs import PermanentError, handler, periodic
from .jobs.queue import purge_finished


TRANSCRIBE = "transcribe_recording"


def recording_ref(recording) -> str:
    return f"callrecording:{recording.pk}"


@handler(TRANSCRIBE, max_attempts=3, backoff_s=60)
def transcribe_recording(payload: dict):
    """Transcribe a recording, then summarise it and pull out its key moments."""
    from .models import CallRecording
    from .summarization import transcribe_and_summarize_recording

    rec = CallRecording.objects.filter(pk=payload.get("recording_id")).first()
    if rec is None:
        raise PermanentError("The recording no longer exists.")
    try:
        transcript, _summary = transcribe_and_summarize_recording(rec)
    except FileNotFoundError as exc:
        raise PermanentError(str(exc)) from exc
    return {"chars": len(transcript or ""), "segments": len(rec.transcript_segments or [])}


@handler("purge_jobs", max_attempts=1)
@periodic("purge_jobs", every_s=24 * 3600)
def purge_jobs(payload: dict):
    """Forget finished jobs after a month; the work they did is on its own rows."""
    return {"deleted": purge_finished(int(payload.get("days") or 30))}
