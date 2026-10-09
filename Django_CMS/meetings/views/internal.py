"""The API the agent workers call back into.

Three locks on every request (see agent_tokens.py): the service key only the
agent containers hold, a signed run token naming one agent run, and a database
check that the run — and its room — are still live. A run that has been stopped
from the room UI is refused here on its very next call.
"""
from __future__ import annotations

import hmac
import json
import logging
import os

from django.conf import settings
from django.http import JsonResponse
from datetime import timedelta

from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .. import agent_tokens, config, rooms
from ..models import AgentRun, MeetingSession, RecordingSegment

logger = logging.getLogger(__name__)


def _deny(status=401, msg="unauthorized"):
    return JsonResponse({"ok": False, "error": msg}, status=status)


# How long after a room closes the recorder may still hand in its last
# segments. Ending a meeting closes the session first; the recorder only
# finishes its files as everyone's tracks end a moment later. Without this,
# the final stretch of every meeting — usually all of it — was refused.
RECORDING_GRACE = timedelta(minutes=10)


def _auth(request, scope: str):
    """``(run, claims)`` for a valid call, or ``(None, response)``."""
    expected = config.service_key()
    presented = request.headers.get("X-CC-Service-Key", "")
    if not expected:
        return None, _deny(503, "internal API disabled: MEETINGS_SERVICE_KEY is not set")
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        return None, _deny()
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    claims = agent_tokens.verify(token)
    if claims is None or not claims.allows(scope):
        return None, _deny(403, "forbidden")
    run = (AgentRun.objects.select_related("session", "session__meeting",
                                           "session__meeting__patient", "protocol")
           .filter(pk=claims.run_id).first())
    if run is None or str(run.session.uuid) != claims.session_uuid:
        return None, _deny(410, "run is no longer live")
    if not (run.is_live and run.session.is_live):
        late_recording = (
            scope == agent_tokens.RECORDING
            and run.role == AgentRun.Role.SCRIBE
            and run.state in (AgentRun.State.STOPPED, AgentRun.State.FINISHED, AgentRun.State.RUNNING)
            and run.session.ended_at is not None
            and timezone.now() - run.session.ended_at < RECORDING_GRACE
        )
        if not late_recording:
            return None, _deny(410, "run is no longer live")
    return run, claims


def _body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


# ── What the agent is for ──────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET"])
def run_config(request):
    """Everything an agent needs to do its job, resolved from live settings."""
    run, claims = _auth(request, agent_tokens.STATUS)
    if run is None:
        return claims
    session = run.session
    meeting = session.meeting
    patient = meeting.patient
    s = config.cfg()

    from ConvAI.site_config import get_setting

    out = {
        "run_id": run.pk,
        "role": run.role,
        "room": session.room_name,
        "session": str(session.uuid),
        "recording_enabled": session.recording_enabled,
        "max_minutes": s.max_minutes,
        "language": run.language or settings.LANGUAGE_CODE,
        "controller_identity": run.controller_identity,
        "respondent_identity": run.respondent_identity,
        "respondent_name": run.respondent_name,
        "client_first_name": patient.name,
        "navigator_name": (rooms.staff_name(patient.navigator) if patient.navigator else ""),
    }
    if run.role == AgentRun.Role.SCRIBE:
        from ..recording import session_dir
        out["recording_dir"] = session_dir(session)
    if run.role in (AgentRun.Role.INTERVIEWER, AgentRun.Role.ASSISTANT):
        endpoint = (get_setting("AZURE_REALTIME_ENDPOINT") or get_setting("AZURE_OPENAI_ENDPOINT") or "").strip()
        key = (get_setting("AZURE_REALTIME_API_KEY") or get_setting("AZURE_OPENAI_API_KEY") or "").strip()
        out["realtime"] = {
            "endpoint": endpoint.rstrip("/"),
            "api_key": key,
            "deployment": (get_setting("AZURE_REALTIME_DEPLOYMENT") or "").strip(),
            "voice": (s.interviewer_voice or get_setting("AZURE_REALTIME_VOICE") or "marin").strip(),
        }
    if run.role == AgentRun.Role.INTERVIEWER and run.protocol_id:
        out["protocol"] = {"number": run.protocol.number, "title": run.protocol.title}
        out["instructions"] = _interviewer_prompt()
    if run.role == AgentRun.Role.ASSISTANT:
        agent = s.assistant_agent
        out["instructions"] = (agent.system_prompt if agent else "") or ""
        out["assistant_name"] = agent.name if agent else ""
        out["search_enabled"] = bool(agent and agent.rag_enabled)
    return JsonResponse(out)


def _interviewer_prompt() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "prompts", "protocol_interviewer_voice.prompt")
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


@csrf_exempt
@require_http_methods(["POST"])
def run_status(request):
    """The agent saying what it is doing: joined, running, paused, finished, failed."""
    run, claims = _auth(request, agent_tokens.STATUS)
    if run is None:
        return claims
    data = _body(request)
    state = data.get("state", "")
    now = timezone.now()
    updates = {"last_status_at": now}
    if data.get("identity"):
        updates["agent_identity"] = str(data["identity"])[:96]
    if state in (AgentRun.State.RUNNING, AgentRun.State.PAUSED):
        updates["state"] = state
        if not run.started_at:
            updates["started_at"] = now
    elif state in (AgentRun.State.FINISHED, AgentRun.State.STOPPED, AgentRun.State.FAILED):
        updates["state"] = state
        updates["ended_at"] = now
        updates["error"] = str(data.get("error") or "")[:300]
    AgentRun.objects.filter(pk=run.pk).update(**updates)

    if run.role == AgentRun.Role.SCRIBE:
        rec = {"running": MeetingSession.Recording.ON,
               "failed": MeetingSession.Recording.LOST,
               "stopped": MeetingSession.Recording.STOPPED,
               "finished": MeetingSession.Recording.STOPPED}.get(state)
        if rec:
            MeetingSession.objects.filter(pk=run.session_id).update(
                recording_state=rec, recorder_seen_at=now)
    return JsonResponse({"ok": True})


# ── Interview ──────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET"])
def protocol_questions(request):
    from ..protocol_data import questions_for

    run, claims = _auth(request, agent_tokens.PROTOCOL)
    if run is None:
        return claims
    if not run.protocol_id:
        return _deny(400, "no protocol on this run")
    return JsonResponse(questions_for(run.session.meeting, run.protocol.number))


@csrf_exempt
@require_http_methods(["POST"])
def protocol_answer(request):
    """Save one answer, against this run's meeting and protocol only.

    The agent cannot name a meeting or a protocol: both come from the run. It
    can only name a question, and one outside the protocol is refused with the
    list of valid ids (the same guard the WhatsApp automation has).
    """
    from ConvAI.native_agents.protocol_qa import _save_answer

    run, claims = _auth(request, agent_tokens.PROTOCOL)
    if run is None:
        return claims
    data = _body(request)
    try:
        question_id = int(data.get("question_id"))
    except (TypeError, ValueError):
        return _deny(400, "question_id is required")
    result = _save_answer(run.protocol.number, run.session.meeting_id, question_id,
                          str(data.get("response") or ""), source="voice")
    if result.get("ok") and result.get("saved"):
        AgentRun.objects.filter(pk=run.pk).update(answers_saved=run.answers_saved + 1,
                                                  last_status_at=timezone.now())
    return JsonResponse(result)


# ── Assistant ──────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
def search(request):
    """The assistant agent's document search (its RAG knowledge base)."""
    run, claims = _auth(request, agent_tokens.SEARCH)
    if run is None:
        return claims
    agent = config.cfg().assistant_agent
    if not (agent and agent.rag_enabled):
        return JsonResponse({"ok": True, "text": "This assistant has no documents to search."})
    query = str(_body(request).get("query") or "").strip()[:500]
    if not query:
        return _deny(400, "query is required")
    try:
        from ConvAI.rag.retrieve import format_hits, search as rag_search
        hits = rag_search(agent.pk, query, agent.rag_top_k)
        return JsonResponse({"ok": True, "text": format_hits(hits)})
    except Exception as exc:  # the same failures the chat tool reports
        logger.warning("Meeting assistant search failed: %s", exc)
        return JsonResponse({"ok": False, "text": "The documents could not be searched just now."})


# ── Recording ──────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
def recording_segment(request):
    """The recorder registering one finished stretch of one person's audio.

    The path must lie inside this session's recording folder — the recorder
    cannot point a recording at an arbitrary file on the shared volume.
    """
    from ..recording import session_dir

    run, claims = _auth(request, agent_tokens.RECORDING)
    if run is None:
        return claims
    data = _body(request)
    base = os.path.realpath(session_dir(run.session))
    path = os.path.realpath(str(data.get("path") or ""))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return _deny(400, "path is outside this session's recording folder or missing")
    try:
        seg, _created = RecordingSegment.objects.update_or_create(
            session=run.session, identity=str(data["identity"])[:96],
            track_sid=str(data["track_sid"])[:64], seq=int(data.get("seq") or 0),
            defaults={
                "label": str(data.get("label") or "")[:120],
                "role": str(data.get("role") or "")[:20],
                "path": path,
                "codec": str(data.get("codec") or "ogg/opus")[:20],
                "start_utc_ms": int(data["start_utc_ms"]),
                "end_utc_ms": int(data["end_utc_ms"]) if data.get("end_utc_ms") else None,
                "bytes": os.path.getsize(path),
            })
    except (KeyError, TypeError, ValueError):
        return _deny(400, "identity, track_sid and start_utc_ms are required")
    if run.session.is_live:
        MeetingSession.objects.filter(pk=run.session_id).update(
            recorder_seen_at=timezone.now(), recording_state=MeetingSession.Recording.ON)
    else:
        # A last segment arriving after the room closed: (re)build the mix so
        # it is included. Deduplicated while queued, so a burst of late
        # segments costs one mixdown.
        rooms.schedule_finalize(run.session, delay_s=15)
    return JsonResponse({"ok": True, "segment": seg.pk})
