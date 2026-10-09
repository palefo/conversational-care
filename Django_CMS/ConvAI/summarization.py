"""Meeting-protocol and call-transcript summarization.

Both features post-process source material with an LLM using a base prompt that
admins can edit in Settings → Prompts (falling back to the shipped defaults in
``default_prompts``). The model is the platform's configured default agent
model, resolved through ``llm_factory.make_llm``.
"""
import logging
import os

from django.utils import timezone

from .default_prompts import (
    DEFAULT_MEETING_SUMMARY_PROMPT,
    DEFAULT_TRANSCRIPT_MOMENTS_PROMPT,
    DEFAULT_TRANSCRIPT_SUMMARY_PROMPT,
)
from .models import Answer, Meeting, Protocol
from .site_config import get_setting


logger = logging.getLogger(__name__)


def _run_llm(system_prompt: str, user_text: str) -> str:
    """Invoke the Summary model (or, if unset, the default) with a system + user message."""
    from .llm_factory import make_llm, summary_model  # local import avoids import-time cycles

    llm = make_llm(summary_model() or None, temperature=0.2)
    resp = llm.invoke([("system", system_prompt), ("human", user_text)])
    content = getattr(resp, "content", None)
    if content is None:
        content = str(resp)
    return content.strip()


# ─────────────────────────── meeting protocols ────────────────────────────
def build_meeting_protocol_text(meeting: Meeting) -> str:
    """Render a meeting's protocol(s) and recorded answers as plain text.

    A call can cover several protocols. We include every protocol relevant to
    it — the ones it was booked for, the ones recorded as covered, and any that
    has answers against this meeting — with each question and its recorded
    answer (or a placeholder when blank).
    """
    answers = (
        Answer.objects
        .filter(meeting=meeting)
        .select_related("question", "question__protocol")
    )
    answer_by_qid = {a.question_id: (a.response or "").strip() for a in answers}

    numbers = []
    for n in (
        list(meeting.executed_protocols.values_list("number", flat=True))
        + list(meeting.scheduled_protocols.values_list("number", flat=True))
    ):
        if n and n not in numbers:
            numbers.append(n)
    for a in answers:
        n = a.question.protocol.number
        if n not in numbers:
            numbers.append(n)

    lines = [
        f"Meeting with {meeting.patient} on "
        f"{timezone.localtime(meeting.scheduled_time):%Y-%m-%d %H:%M}",
        f"Type: {meeting.get_type_display()} · Status: {meeting.get_status_display()}",
        "",
    ]
    protocols = {p.number: p for p in Protocol.objects.filter(number__in=numbers)}
    for n in numbers:
        proto = protocols.get(n)
        if not proto:
            continue
        filled = any(
            answer_by_qid.get(q.id) for q in proto.questions.all()
        )
        lines.append(f"Protocol {proto.number}: {proto.title}"
                     f"{'' if filled else '  (no answers recorded)'}")
        for q in proto.questions.all():
            ans = answer_by_qid.get(q.id) or "(no answer)"
            lines.append(f"  Q: {q.prompt_md}")
            lines.append(f"  A: {ans}")
        lines.append("")

    return "\n".join(lines).strip()


def summarize_meeting(meeting: Meeting) -> str:
    """Summarize a meeting's protocol answers and persist the result."""
    prompt = get_setting("MEETING_SUMMARY_PROMPT") or DEFAULT_MEETING_SUMMARY_PROMPT
    body = build_meeting_protocol_text(meeting)
    summary = _run_llm(prompt, body)
    meeting.protocol_summary = summary
    meeting.protocol_summarized_at = timezone.now()
    meeting.save(update_fields=["protocol_summary", "protocol_summarized_at"])
    return summary


# ─────────────────────────── call transcripts ────────────────────────────
def recognition_hint(recording) -> str:
    """Names this call is likely to contain, as a spelling prior for Whisper.

    Whisper takes a short prompt and uses it as context. It cannot put words in
    the transcript that nobody said — it biases spelling and vocabulary, which
    is the difference between "Hi Pablo" and "Hi Pueblo", and between "IQCODE"
    and "IQ code". Names are what a care call gets wrong most and what a reader
    notices first.
    """
    from django.db.models import Q

    from .models import Patient

    # The recording's own client first — every online recording has one, and
    # attributed phone recordings do too. Matching numbers is the fallback for
    # rows from before recordings knew whose they were.
    patient = None
    if getattr(recording, "patient_id", None):
        patient = (Patient.objects.filter(pk=recording.patient_id)
                   .select_related("caregiver", "navigator").first())
    if patient is None:
        nums = {str(recording.to_number or ""), str(recording.from_number or "")}
        nums.discard("")
        if not nums:
            return ""
        patient = (Patient.objects
                   .filter(Q(phone_number__in=nums) | Q(caregiver__phone_number__in=nums))
                   .select_related("caregiver", "navigator")
                   .first())
    if not patient:
        return ""

    names = [patient.name, patient.lastname]
    if patient.caregiver:
        names += [patient.caregiver.name, patient.caregiver.lastname]
    if patient.navigator:
        names.append(patient.navigator.get_full_name() or patient.navigator.username)

    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return ""
    # dict.fromkeys rather than set: the order is the order they were added,
    # so the hint reads the same way twice for the same call.
    return "A care call. Names that may be spoken: %s." % ", ".join(dict.fromkeys(names))


def transcribe_recording(recording) -> str:
    """Transcribe a call recording's audio with Whisper and persist it."""
    from .models import CallRecording
    from .utils import transcribe_audio, transcribe_tracks  # avoids import-time cycles

    # An online meeting keeps one clean track per speaker, which beats any
    # attempt to pull voices apart from the mix. Twilio is never asked about a
    # recording it did not make.
    tracks = [t for t in (recording.tracks or [])
              if t.get("path") and os.path.exists(t["path"])]
    if recording.source == CallRecording.Source.ONLINE and tracks:
        transcript, segments = transcribe_tracks(
            [(t.get("speaker"), t["path"], t.get("offset_s") or 0.0) for t in tracks],
            prompt=recognition_hint(recording) or None)
        transcript = (transcript or "").strip()
        recording.transcript = transcript
        recording.transcript_segments = segments
        recording.transcribed_at = timezone.now()
        recording.save(update_fields=["transcript", "transcript_segments", "transcribed_at"])
        return transcript

    path = recording.filename
    if not path or not os.path.exists(path):
        raise FileNotFoundError("Recording audio file not found on disk.")
    # The sid lets transcribe_audio reach the WAV twin of this mp3, which is
    # the only rendering that still has the two parties on separate channels.
    is_twilio = recording.source == CallRecording.Source.TWILIO
    transcript, segments = transcribe_audio(
        path, with_segments=True,
        recording_sid=recording.recording_sid if is_twilio else None,
        prompt=recognition_hint(recording) or None)
    transcript = (transcript or "").strip()
    recording.transcript = transcript
    recording.transcript_segments = segments
    recording.transcribed_at = timezone.now()
    recording.save(update_fields=["transcript", "transcript_segments", "transcribed_at"])
    return transcript


def summarize_transcript(recording) -> str:
    """Summarize an already-transcribed recording and persist the summary."""
    prompt = get_setting("TRANSCRIPT_SUMMARY_PROMPT") or DEFAULT_TRANSCRIPT_SUMMARY_PROMPT
    transcript = (recording.transcript or "").strip()
    if not transcript:
        summary = "No speech could be transcribed from this recording."
    else:
        summary = _run_llm(prompt, transcript)
    recording.transcript_summary = summary
    recording.save(update_fields=["transcript_summary"])
    return summary


def extract_moments(recording) -> list:
    """Pull the moments worth jumping to out of an already-segmented transcript.

    Each moment names a segment rather than writing its own timestamp. That is
    the whole point: a model asked for "key moments" will cheerfully invent one,
    and an invented moment has no segment to attach to, so it is dropped here
    rather than appearing in a clinical record pointing at silence.
    """
    segments = recording.transcript_segments or []
    if not segments:
        recording.transcript_moments = []
        recording.save(update_fields=["transcript_moments"])
        return []

    numbered = "\n".join(
        f"{i}. {seg.get('text', '')}" for i, seg in enumerate(segments)
    )
    prompt = get_setting("TRANSCRIPT_MOMENTS_PROMPT") or DEFAULT_TRANSCRIPT_MOMENTS_PROMPT
    raw = _run_llm(prompt, numbered) or ""

    moments = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        head, _, text = line.partition("|")
        digits = "".join(ch for ch in head if ch.isdigit())
        if not digits:
            continue
        index = int(digits)
        if not (0 <= index < len(segments)):
            continue  # a moment pointing at audio that is not there
        text = text.strip()
        if not text:
            continue
        moments.append({
            "text": text,
            "segment": index,
            "start": segments[index].get("start", 0.0),
        })

    recording.transcript_moments = moments
    recording.save(update_fields=["transcript_moments"])
    return moments


def queue_transcription(recording, *, by=None):
    """Queue transcribe → summarise → key moments for ``recording``.

    Returns the Job. Asking twice while one is queued or running returns the
    same job rather than paying Whisper twice for the same audio.
    """
    from .jobs import enqueue

    return enqueue(
        "transcribe_recording", {"recording_id": recording.pk},
        ref=f"callrecording:{recording.pk}",
        dedupe_key=f"transcribe:{recording.pk}",
        created_by=by,
    )


def transcription_state(recording):
    """What the panel should say about this recording's transcription job.

    ``None`` when no job was ever queued for it (older recordings transcribed
    in the request, or never). Otherwise a dict the templates can read.
    """
    from .jobs import latest_for, worker_seen

    if recording is None or not getattr(recording, "pk", None):
        return None
    job = latest_for(f"callrecording:{recording.pk}", "transcribe_recording")
    if job is None:
        return None
    live = job.is_live
    return {
        "job": job,
        "status": job.status,
        "live": live,
        "failed": job.status == job.Status.FAILED,
        "error": (job.last_error or "").split("\n\n", 1)[0][:300],
        "retrying": live and job.attempts > 0 and job.status == job.Status.QUEUED,
        # Queued with nobody to run it: say so instead of spinning forever.
        "no_worker": live and not worker_seen(),
    }


def transcribe_and_summarize_recording(recording):
    """Transcribe a recording, then post-process it into a summary.

    Returns ``(transcript, summary)``.
    """
    transcript = transcribe_recording(recording)
    summary = summarize_transcript(recording)
    # Best-effort: a transcript and a summary are worth keeping even if the
    # moments pass fails, so it must not take the other two down with it.
    try:
        extract_moments(recording)
    except Exception:
        logger.exception("Could not extract key moments for recording %s", recording.pk)
    return transcript, summary
