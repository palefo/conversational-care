"""cc-voice: the agents that speak in a meeting.

Two roles, chosen by the run the platform dispatched:

* **interviewer** — works through one protocol with one person (the
  respondent), by voice, saving each answer as it is given. It only *hears* the
  respondent, so it does not answer the navigator talking in the background;
  the navigator steers it instead with room RPCs: ``cc.pause``, ``cc.resume``,
  ``cc.say`` (a typed instruction) and ``cc.stop``.
* **assistant** — push-to-talk for the navigator. Silent until a member of
  staff holds "Ask the assistant" (``cc.ptt.start`` … ``cc.ptt.end``); its
  answer is heard by everyone in the meeting.

Both use Azure OpenAI GPT Realtime through LiveKit's OpenAI plugin, with the
endpoint, key, deployment and voice the platform hands over in the run config
(so changing them in Settings → Agents takes effect on the next run). Tools run
here and call the platform's internal API.

    python -m cc_agents.voice start
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from livekit import rtc
from livekit.agents import (Agent, AgentSession, AutoSubscribe, JobContext, JobExecutorType,
                            JobRequest, RunContext, WorkerOptions, cli, function_tool, room_io)
from livekit.plugins import openai

from . import processors
from .api import PlatformAPI, RunGone

logger = logging.getLogger("cc_agents.voice")

AGENT_NAME = "cc-voice"
RESPONDENT_WAIT_S = 120
ASSISTANT_IDLE_S = int(os.getenv("CC_ASSISTANT_IDLE_S", "1200"))

LANGUAGE_NAMES = {
    "en": "English", "en-gb": "English", "es": "Spanish", "es-pe": "Spanish",
    "pt": "Portuguese", "pt-br": "Portuguese", "it": "Italian", "ko": "Korean",
    "zh-hans": "Chinese (Mandarin)", "zh": "Chinese (Mandarin)",
}

ASSISTANT_SUFFIX = """

You are now taking part in a live online care meeting, by voice.
- A member of the care team asks you questions using push-to-talk. Everyone in the meeting hears your answers — including the client and their family — so speak to the room, kindly and plainly.
- Be brief: two to four spoken sentences, no lists, no markdown. Offer more only if asked.
- Never mention other clients or anything not meant for the people in this meeting.
- If you are not sure, say so. You do not give medical advice; say the care team will follow up.
"""


def _staff(identity: str) -> bool:
    # Staff identities are issued by the platform ("staff-<user id>") in tokens
    # browsers cannot rewrite, so this is a sound test of who is asking.
    return (identity or "").startswith("staff-")


def make_model(rt: dict, *, manual_turns: bool = False):
    kwargs = dict(
        azure_deployment=rt["deployment"],
        azure_endpoint=rt["endpoint"],
        api_key=rt["api_key"],
        voice=rt.get("voice") or "marin",
    )
    base_url = os.getenv("CC_REALTIME_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    if manual_turns:
        # Push-to-talk: the platform decides when a turn ends, not voice
        # activity detection.
        kwargs["turn_detection"] = None
    return openai.realtime.RealtimeModel.with_azure(**kwargs)


def _staff_in_room(room: rtc.Room) -> list[str]:
    return [p.identity for p in room.remote_participants.values() if _staff(p.identity)]


async def _tell_staff(room: rtc.Room, payload: dict):
    to = _staff_in_room(room)
    if not to:
        return
    try:
        await room.local_participant.publish_data(json.dumps(payload), reliable=True,
                                                  destination_identities=to, topic="cc.interview")
    except Exception:
        logger.debug("Could not notify staff", exc_info=True)


# ── The interviewer ─────────────────────────────────────────────────────────

class InterviewerAgent(Agent):
    def __init__(self, run: "InterviewRun"):
        self.run = run
        super().__init__(instructions=run.instructions())

    @function_tool()
    async def get_protocol_questions(self, context: RunContext) -> dict:
        """Load the questions to ask, with any answers already saved for this
        meeting and answers carried over from an earlier meeting. Call this
        before asking anything, and again if you lose track."""
        data = await self.run.api.questions()
        return {
            "protocol": data.get("protocol"),
            "questions": [{"id": q["id"], "question": q["prompt"], "answer_saved": q["answer"],
                           "carried_from_earlier_meeting": q.get("carried")}
                          for q in data.get("questions", [])],
            "answered": data.get("answered"), "total": data.get("total"),
        }

    @function_tool()
    async def save_protocol_answer(self, context: RunContext, question_id: int, response: str) -> dict:
        """Save the answer to one question.

        Args:
            question_id: The id of the question, exactly as given by get_protocol_questions.
            response: Concise third-person notes for a clinician, e.g. "Caregiver reports …".
        """
        result = await self.run.api.save_answer(question_id, response)
        if result.get("ok") and result.get("saved"):
            await _tell_staff(self.run.ctx.room, {"type": "answer", "question_id": question_id})
        return result

    @function_tool()
    async def get_current_date(self, context: RunContext) -> str:
        """The current date, weekday and time, for working out relative dates."""
        return datetime.now(timezone.utc).astimezone().strftime("%A %d %B %Y, %H:%M %Z")

    @function_tool()
    async def flag_for_navigator(self, context: RunContext, description: str) -> str:
        """Alert the navigator in the meeting to something that needs a person
        now (for example a risk of harm). Stop asking questions after this.

        Args:
            description: One sentence on what was said and why it matters.
        """
        await _tell_staff(self.run.ctx.room, {"type": "flag", "text": description[:300]})
        return "The navigator has been alerted. Respond with care and wait for them."

    @function_tool()
    async def finish_interview(self, context: RunContext) -> str:
        """End the interview. Call once, last, after thanking the respondent."""
        asyncio.get_running_loop().call_later(4.0, self.run.finish, "finished")
        return "Finishing."


class InterviewRun:
    def __init__(self, ctx: JobContext, api: PlatformAPI, cfg: dict):
        self.ctx = ctx
        self.api = api
        self.cfg = cfg
        self.done = asyncio.Event()
        self.outcome = "finished"
        self.session: AgentSession | None = None
        self.paused = False

    def instructions(self) -> str:
        c = self.cfg
        lang = LANGUAGE_NAMES.get((c.get("language") or "").lower(), "")
        parts = [c.get("instructions") or "", "", "== This meeting =="]
        if c.get("protocol"):
            parts.append(f"Questionnaire: {c['protocol']['title']}.")
        if c.get("respondent_name"):
            parts.append(f"You are interviewing {c['respondent_name']}.")
        if c.get("client_first_name"):
            parts.append(f"The questions are about the care of {c['client_first_name']}.")
        if c.get("navigator_name"):
            parts.append(f"Their navigator, {c['navigator_name']}, is in the meeting.")
        if lang:
            parts.append(f"Start in {lang}; if they speak another language, switch to it.")
        return "\n".join(parts).strip()

    def finish(self, outcome: str = "finished"):
        self.outcome = outcome
        self.done.set()

    def _guard(self, data: rtc.RpcInvocationData) -> bool:
        return _staff(data.caller_identity)

    async def _set_paused(self, paused: bool):
        s = self.session
        self.paused = paused
        if paused:
            s.interrupt()
        s.input.set_audio_enabled(not paused)
        s.output.set_audio_enabled(not paused)
        await self.ctx.room.local_participant.set_attributes({"cc.paused": "1" if paused else "0"})
        await self.api.status("paused" if paused else "running")
        await _tell_staff(self.ctx.room, {"type": "status", "state": "paused" if paused else "running"})

    def _register_rpcs(self):
        lp = self.ctx.room.local_participant

        @lp.register_rpc_method("cc.pause")
        async def _pause(data: rtc.RpcInvocationData) -> str:
            if not self._guard(data):
                return json.dumps({"ok": False})
            await self._set_paused(True)
            return json.dumps({"ok": True})

        @lp.register_rpc_method("cc.resume")
        async def _resume(data: rtc.RpcInvocationData) -> str:
            if not self._guard(data):
                return json.dumps({"ok": False})
            await self._set_paused(False)
            self.session.generate_reply(instructions=(
                "The navigator has resumed you. In one short sentence, pick up where you left off."))
            return json.dumps({"ok": True})

        @lp.register_rpc_method("cc.say")
        async def _say(data: rtc.RpcInvocationData) -> str:
            if not self._guard(data):
                return json.dumps({"ok": False})
            try:
                text = (json.loads(data.payload or "{}").get("text") or "").strip()[:800]
            except ValueError:
                text = ""
            if text:
                if self.paused:
                    await self._set_paused(False)
                self.session.interrupt()
                self.session.generate_reply(instructions=(
                    "An instruction from the navigator, who is in the meeting. Follow it; "
                    "do not read it out word for word: " + text))
            return json.dumps({"ok": True})

        @lp.register_rpc_method("cc.stop")
        async def _stop(data: rtc.RpcInvocationData) -> str:
            if not self._guard(data):
                return json.dumps({"ok": False})
            self.session.interrupt()
            self.finish("stopped")
            return json.dumps({"ok": True})

    async def run(self):
        respondent = self.cfg.get("respondent_identity") or ""
        try:
            await asyncio.wait_for(self.ctx.wait_for_participant(identity=respondent),
                                   timeout=RESPONDENT_WAIT_S)
        except asyncio.TimeoutError:
            await self.api.status("failed", error="The person to interview was not in the meeting.")
            return

        self.session = AgentSession(llm=make_model(self.cfg["realtime"]))
        await self.session.start(
            InterviewerAgent(self), room=self.ctx.room,
            room_options=room_io.RoomOptions(
                participant_identity=respondent,
                # A dropped connection should pause the interview, not end it.
                close_on_disconnect=False,
                audio_input=room_io.AudioInputOptions(
                    noise_cancellation=processors.input_processor("agent")),
            ))
        self._register_rpcs()
        await self.ctx.room.local_participant.set_attributes({"cc.paused": "0"})
        await self.api.status("running")

        def _back(p: rtc.RemoteParticipant):
            if p.identity == respondent and not self.paused:
                self.session.generate_reply(instructions=(
                    "They have just reconnected after a dropped connection. Welcome them back "
                    "in one sentence and continue where you were."))
        self.ctx.room.on("participant_connected", _back)

        self.session.generate_reply(instructions=(
            "Begin now: greet them by first name and introduce yourself as instructed, "
            "then call get_protocol_questions before your first question."))
        try:
            await asyncio.wait_for(self.done.wait(), timeout=int(self.cfg.get("max_minutes") or 90) * 60)
        except asyncio.TimeoutError:
            self.outcome = "stopped"
        await self.api.status(self.outcome)
        await self.session.aclose()


# ── The assistant ───────────────────────────────────────────────────────────

def _search_tool(api: PlatformAPI):
    """The document search, built only for an assistant that has documents.

    A function, not a decorated method: the Agents SDK collects every decorated
    method of an Agent class, which would hand an assistant with no knowledge
    base a search tool that can only ever answer "nothing found".
    """
    @function_tool(name="search_documents")
    async def search_documents(context: RunContext, query: str) -> str:
        """Search the care team's documents for facts (services, policies, how-tos).

        Args:
            query: What to look for, in a few words.
        """
        data = await api.search(query)
        return data.get("text") or "Nothing relevant was found."
    return search_documents


class AssistantRun:
    def __init__(self, ctx: JobContext, api: PlatformAPI, cfg: dict):
        self.ctx = ctx
        self.api = api
        self.cfg = cfg
        self.done = asyncio.Event()
        self.session: AgentSession | None = None
        self.last_used = asyncio.get_event_loop().time()

    async def run(self):
        controller = self.cfg.get("controller_identity") or ""
        instructions = (self.cfg.get("instructions") or "You are a helpful care assistant.") + ASSISTANT_SUFFIX
        self.session = AgentSession(llm=make_model(self.cfg["realtime"], manual_turns=True),
                                    turn_detection="manual")
        tools = [_search_tool(self.api)] if self.cfg.get("search_enabled") else []
        await self.session.start(
            Agent(instructions=instructions, tools=tools),
            room=self.ctx.room,
            room_options=room_io.RoomOptions(
                participant_identity=controller, close_on_disconnect=False,
                audio_input=room_io.AudioInputOptions(
                    noise_cancellation=processors.input_processor("agent")),
            ))
        # Deaf until someone holds the button.
        self.session.input.set_audio_enabled(False)
        lp = self.ctx.room.local_participant

        @lp.register_rpc_method("cc.ptt.start")
        async def _start(data: rtc.RpcInvocationData) -> str:
            if not _staff(data.caller_identity):
                return json.dumps({"ok": False})
            self.last_used = asyncio.get_event_loop().time()
            room_io_ = self.session.room_io
            if room_io_ and room_io_.linked_participant and \
                    room_io_.linked_participant.identity != data.caller_identity:
                # Whoever is holding the button is who it listens to.
                room_io_.set_participant(data.caller_identity)
            self.session.interrupt()
            self.session.clear_user_turn()
            self.session.input.set_audio_enabled(True)
            return json.dumps({"ok": True})

        @lp.register_rpc_method("cc.ptt.end")
        async def _end(data: rtc.RpcInvocationData) -> str:
            if not _staff(data.caller_identity):
                return json.dumps({"ok": False})
            self.session.input.set_audio_enabled(False)
            self.session.commit_user_turn()
            return json.dumps({"ok": True})

        @lp.register_rpc_method("cc.stop")
        async def _stop(data: rtc.RpcInvocationData) -> str:
            if not _staff(data.caller_identity):
                return json.dumps({"ok": False})
            self.done.set()
            return json.dumps({"ok": True})

        await self.api.status("running")
        # Leave when unused for a while: an idle realtime session still costs.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + int(self.cfg.get("max_minutes") or 90) * 60
        while not self.done.is_set():
            try:
                await asyncio.wait_for(self.done.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            now = loop.time()
            if now > deadline or now - self.last_used > ASSISTANT_IDLE_S:
                break
        await self.api.status("stopped")
        await self.session.aclose()


# ── Worker plumbing ─────────────────────────────────────────────────────────

async def request_fnc(req: JobRequest):
    try:
        meta = json.loads(req.job.metadata or "{}")
    except ValueError:
        meta = {}
    role = meta.get("role", "assistant")
    name = "AI interviewer" if role == "interviewer" else "AI assistant"
    await req.accept(identity=f"agent-{role}-{req.id[-8:]}", name=name,
                     attributes={"cc.role": role})


async def entrypoint(ctx: JobContext):
    api, meta = PlatformAPI.from_metadata(ctx.job.metadata)
    try:
        cfg = await api.run_config()
    except RunGone:
        await api.close()
        ctx.shutdown("run is over")
        return
    # Checked before joining: an agent that cannot speak should not flash
    # into the meeting and out again.
    rt = cfg.get("realtime") or {}
    if not (rt.get("endpoint") and rt.get("api_key") and rt.get("deployment")):
        await api.status("failed", error="Azure OpenAI Realtime is not configured (Settings → Agents).")
        await api.close()
        ctx.shutdown("not configured")
        return

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    await api.status("", identity=ctx.room.local_participant.identity)

    try:
        if cfg.get("role") == "interviewer":
            await InterviewRun(ctx, api, cfg).run()
        else:
            await AssistantRun(ctx, api, cfg).run()
    except RunGone:
        logger.info("Run ended by the platform")
    except Exception as exc:
        logger.exception("Voice agent failed")
        await api.status("failed", error=str(exc)[:200])
    finally:
        await api.close()
        ctx.shutdown("done")


def main():
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        request_fnc=request_fnc,
        agent_name=AGENT_NAME,
        # One process per conversation, started on demand: nothing sits idle
        # holding memory while no meeting has an agent in it.
        job_executor_type=JobExecutorType.PROCESS,
        num_idle_processes=int(os.getenv("CC_VOICE_IDLE_PROCESSES", "0")),
        load_threshold=float(os.getenv("CC_VOICE_LOAD_THRESHOLD", "0.8")),
        job_memory_warn_mb=600,
    ))


if __name__ == "__main__":
    main()
