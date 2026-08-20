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
    """Invoke the configured default chat model with a system + user message."""
    from .llm_factory import make_llm  # local import avoids import-time cycles

    llm = make_llm(None, temperature=0.2)
    resp = llm.invoke([("system", system_prompt), ("human", user_text)])
    content = getattr(resp, "content", None)
    if content is None:
        content = str(resp)
    return content.strip()


# ─────────────────────────── meeting protocols ────────────────────────────
def build_meeting_protocol_text(meeting: Meeting) -> str:
    """Render a meeting's protocol(s) and recorded answers as plain text.

    A meeting can reference several protocols but usually only one is filled in.
    We include every protocol relevant to the meeting — the scheduled/executed
    protocol plus any protocol that has answers — with each question and its
    recorded answer (or a placeholder when blank).
    """
    answers = (
        Answer.objects
        .filter(meeting=meeting)
        .select_related("question", "question__protocol")
    )
    answer_by_qid = {a.question_id: (a.response or "").strip() for a in answers}

    numbers = []
    for n in (meeting.executed_protocol, meeting.scheduled_protocol):
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
def transcribe_recording(recording) -> str:
    """Transcribe a call recording's audio with Whisper and persist it."""
    from .utils import transcribe_audio  # local import avoids import-time cycles

    path = recording.filename
    if not path or not os.path.exists(path):
        raise FileNotFoundError("Recording audio file not found on disk.")
    transcript, segments = transcribe_audio(path, with_segments=True)
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
