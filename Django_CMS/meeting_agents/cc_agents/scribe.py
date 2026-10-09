"""cc-scribe: records each person's microphone into its own file.

Joined to a room by the platform when a meeting starts with recording on. It
publishes nothing and is not shown as a tile (the room UI hides ``cc.role =
scribe``); it subscribes to every microphone and to the voice agents' audio,
and writes one wall-clock-aligned Ogg/Opus file per person per stretch
(cc_agents.wallclock). Each closed file is registered with the platform, which
later mixes the meeting down and transcribes it speaker by speaker.

* Someone whose token says ``cc.record = 0`` is never recorded.
* Files rotate every ``ROTATE_S`` seconds, so a crash costs at most one stretch.
* A reconnect (a new track) starts a new file; a device switch keeps the track
  and the file.
* If this worker dies, the platform notices the recorder left and sends
  another; recording carries on in new files.

    python -m cc_agents.scribe start
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import numpy as np
from livekit import rtc
from livekit.agents import AutoSubscribe, JobContext, JobExecutorType, JobRequest, WorkerOptions, cli

from . import processors
from .api import PlatformAPI, RunGone
from .wallclock import WallclockWriter, now_ms

logger = logging.getLogger("cc_agents.scribe")

AGENT_NAME = "cc-scribe"
ROTATE_S = int(os.getenv("CC_RECORD_ROTATE_S", "600"))
RATE = 16000


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:60] or "x"


class TrackRecorder:
    """One subscribed audio track → a sequence of files."""

    def __init__(self, scribe: "Scribe", track: rtc.Track, participant: rtc.RemoteParticipant):
        self.scribe = scribe
        self.track = track
        self.participant = participant
        self.identity = participant.identity
        attrs = participant.attributes or {}
        self.role = attrs.get("cc.role") or ("assistant" if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT else "guest")
        self.label = participant.name or participant.identity
        self.seq = 0
        self.writer: WallclockWriter | None = None
        # Set once the audio has stopped and the last file is being closed and
        # handed in. From then on the task must be left to finish: cancelling
        # it would interrupt the hand-in, and lose the file silently.
        self.finishing = False
        self.task = asyncio.create_task(self._run(), name=f"rec-{self.identity}")

    def _path(self) -> str:
        return os.path.join(self.scribe.dir,
                            f"{_safe(self.identity)}-{_safe(self.track.sid)}-{self.seq:03d}.ogg")

    def _sidecar(self, writer: WallclockWriter, seq: int, *, final: bool) -> None:
        """Describe the file beside it, so it can be recovered unregistered.

        Registering a segment is a call to the platform at the very end of a
        file's life — exactly when a recorder that is being shut down, or has
        crashed, cannot be counted on. The sidecar is written as soon as the
        file has a start time and again when it closes; the platform's mixdown
        adopts any segment it finds described here but never registered.
        """
        if writer.start_ms is None:
            return
        meta = {"identity": self.identity, "label": self.label, "role": self.role,
                "track_sid": self.track.sid, "seq": seq, "codec": "ogg/opus",
                "start_utc_ms": writer.start_ms,
                "end_utc_ms": writer.end_ms if final else None}
        tmp = writer.path[:-4] + ".json.tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(meta, fh)
            os.replace(tmp, writer.path[:-4] + ".json")
        except OSError:
            logger.warning("Could not write the sidecar for %s", writer.path)

    async def _run(self):
        stream = rtc.AudioStream.from_track(
            track=self.track, sample_rate=RATE, num_channels=1,
            noise_cancellation=processors.input_processor("recording"))
        try:
            async for event in stream:
                frame = event.frame
                fresh = self.writer is None
                if fresh:
                    self.writer = WallclockWriter(self._path(), sample_rate=RATE)
                self.writer.write(np.frombuffer(frame.data, dtype=np.int16), now_ms())
                if fresh:
                    self._sidecar(self.writer, self.seq, final=False)
                if self.writer.duration_s >= ROTATE_S:
                    await self._close_segment()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Recording %s failed", self.identity)
        finally:
            self.finishing = True
            # Each step on its own: a stream that cannot be closed cleanly
            # (the room is already gone) must not cost the file its ending.
            try:
                await stream.aclose()
            except Exception:
                logger.debug("Closing the audio stream for %s failed", self.identity, exc_info=True)
            await self._close_segment()

    async def _close_segment(self):
        writer, self.writer = self.writer, None
        if writer is None:
            return
        writer.close()
        if not writer.written:
            try:
                os.remove(writer.path)
            except OSError:
                pass
            return
        seq = self.seq
        self.seq += 1
        self._sidecar(writer, seq, final=True)
        await self.scribe.register(self, writer, seq)

    async def stop(self):
        if self.task.done():
            return
        if not self.finishing:
            # Still reading audio: stop reading. The task then closes and
            # hands in its file itself.
            self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass


class Scribe:
    def __init__(self, ctx: JobContext, api: PlatformAPI, directory: str):
        self.ctx = ctx
        self.api = api
        self.dir = directory
        self.recorders: dict[str, TrackRecorder] = {}
        # Recorders finishing up (closing their file, registering it). Kept so
        # shutdown can wait for them: someone leaving as the room closes would
        # otherwise have their last file written but never handed in.
        self.finishing: set[asyncio.Task] = set()
        os.makedirs(self.dir, exist_ok=True)

    def wants(self, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant) -> bool:
        if publication.kind != rtc.TrackKind.KIND_AUDIO:
            return False
        attrs = participant.attributes or {}
        if attrs.get("cc.role") == "scribe":
            return False
        if attrs.get("cc.record") == "0":
            return False
        # Microphones only — never screen-share audio. Agents publish their
        # voice as a microphone too.
        return publication.source in (rtc.TrackSource.SOURCE_MICROPHONE, rtc.TrackSource.SOURCE_UNKNOWN)

    def on_subscribed(self, track: rtc.Track, publication: rtc.RemoteTrackPublication,
                      participant: rtc.RemoteParticipant):
        if not self.wants(publication, participant) or track.sid in self.recorders:
            return
        self.recorders[track.sid] = TrackRecorder(self, track, participant)

    def on_unsubscribed(self, track: rtc.Track, publication, participant):
        rec = self.recorders.pop(track.sid, None)
        if rec:
            task = asyncio.create_task(rec.stop())
            self.finishing.add(task)
            task.add_done_callback(self.finishing.discard)

    async def register(self, rec: TrackRecorder, writer: WallclockWriter, seq: int):
        try:
            await self.api.segment(
                identity=rec.identity, label=rec.label, role=rec.role,
                track_sid=rec.track.sid, seq=seq, path=writer.path,
                start_utc_ms=writer.start_ms, end_utc_ms=writer.end_ms, codec="ogg/opus")
        except RunGone:
            logger.info("Run ended; segment %s kept on disk", writer.path)
        except Exception:
            logger.exception("Could not register %s; it stays on disk for recovery", writer.path)

    async def close(self):
        # Until nothing is left: tracks can end *while* shutdown is under way
        # (everyone is disconnected at once when the room closes), and each of
        # those starts finishing a file of its own.
        for _ in range(20):
            recs = list(self.recorders.values())
            self.recorders.clear()
            pending = list(self.finishing)
            if not recs and not pending:
                break
            await asyncio.gather(*(r.stop() for r in recs), *pending, return_exceptions=True)
            await asyncio.sleep(0.05)


async def request_fnc(req: JobRequest):
    await req.accept(identity=f"agent-scribe-{req.id[-8:]}", name="Recorder",
                     attributes={"cc.role": "scribe"})


async def entrypoint(ctx: JobContext):
    api, meta = PlatformAPI.from_metadata(ctx.job.metadata)
    try:
        cfg = await api.run_config()
    except RunGone:
        await api.close()
        return
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    scribe = Scribe(ctx, api, cfg["recording_dir"])
    ctx.room.on("track_subscribed", scribe.on_subscribed)
    ctx.room.on("track_unsubscribed", scribe.on_unsubscribed)
    for p in ctx.room.remote_participants.values():
        for pub in p.track_publications.values():
            if pub.track is not None:
                scribe.on_subscribed(pub.track, pub, p)
    await api.status("running", identity=ctx.room.local_participant.identity)

    async def _shutdown(reason: str = ""):
        await scribe.close()
        await api.status("stopped")
        await api.close()

    ctx.add_shutdown_callback(_shutdown)
    logger.info("Recording %s into %s", cfg.get("room"), cfg["recording_dir"])


def main():
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        request_fnc=request_fnc,
        agent_name=AGENT_NAME,
        # A recorder does no inference: threads, not processes, keep one
        # worker's memory flat however many rooms it is recording.
        job_executor_type=JobExecutorType.THREAD,
        num_idle_processes=0,
        load_threshold=float(os.getenv("CC_SCRIBE_LOAD_THRESHOLD", "0.9")),
    ))


if __name__ == "__main__":
    main()
