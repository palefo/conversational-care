"""The recorder hands in every file, including those of people who leave as
the room closes."""
import asyncio
from types import SimpleNamespace

import pytest

from cc_agents import scribe


class SlowRecorder:
    def __init__(self, log, name, delay):
        self.log, self.name, self.delay = log, name, delay

    async def stop(self):
        await asyncio.sleep(self.delay)
        self.log.append(self.name)


def test_close_waits_for_recorders_already_finishing(tmp_path):
    async def go():
        log = []
        s = scribe.Scribe(ctx=SimpleNamespace(), api=SimpleNamespace(), directory=str(tmp_path))
        s.recorders["TR_staff"] = SlowRecorder(log, "staff", 0.01)
        s.recorders["TR_ana"] = SlowRecorder(log, "ana", 0.05)
        # Ana leaves as the room closes: her recorder starts finishing on its own…
        s.on_unsubscribed(SimpleNamespace(sid="TR_ana"), None, None)
        # …and shutdown must still wait for it.
        await s.close()
        return log

    assert sorted(asyncio.run(go())) == ["ana", "staff"]


def test_wants_skips_the_unrecordable(tmp_path):
    from livekit import rtc
    s = scribe.Scribe(ctx=SimpleNamespace(), api=SimpleNamespace(), directory=str(tmp_path))
    mic = SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO, source=rtc.TrackSource.SOURCE_MICROPHONE)
    screen = SimpleNamespace(kind=rtc.TrackKind.KIND_AUDIO, source=rtc.TrackSource.SOURCE_SCREENSHARE_AUDIO)
    cam = SimpleNamespace(kind=rtc.TrackKind.KIND_VIDEO, source=rtc.TrackSource.SOURCE_CAMERA)
    person = SimpleNamespace(attributes={"cc.role": "caregiver", "cc.record": "1"})
    declined = SimpleNamespace(attributes={"cc.role": "caregiver", "cc.record": "0"})
    itself = SimpleNamespace(attributes={"cc.role": "scribe"})
    assert s.wants(mic, person)
    assert not s.wants(screen, person)
    assert not s.wants(cam, person)
    assert not s.wants(mic, declined)
    assert not s.wants(mic, itself)


def test_stop_does_not_interrupt_a_file_being_handed_in(tmp_path):
    """Regression: when the room closed, a recorder already registering its
    last file was cancelled by shutdown, and the file was silently lost."""
    async def go():
        handed_in = []

        class API:
            async def segment(self, **fields):
                await asyncio.sleep(0.05)       # the platform takes a moment
                handed_in.append(fields["identity"])
                return {"ok": True}

        s = scribe.Scribe(ctx=SimpleNamespace(), api=API(), directory=str(tmp_path))
        rec = scribe.TrackRecorder.__new__(scribe.TrackRecorder)
        rec.scribe, rec.identity, rec.label, rec.role, rec.seq = s, "inv-ana", "Ana", "caregiver", 0
        rec.track = SimpleNamespace(sid="TR_1")
        rec.writer = None
        rec.finishing = False

        async def run():
            # The stream ended by itself (room deleted) and the file is being
            # handed in when shutdown arrives.
            rec.finishing = True
            from cc_agents.wallclock import WallclockWriter
            import numpy as np
            w = WallclockWriter(str(tmp_path / "a.ogg"))
            w.write(np.zeros(1600, dtype=np.int16), 10_000)
            rec.writer = w
            await rec._close_segment()

        rec.task = asyncio.create_task(run())
        await asyncio.sleep(0.01)               # mid hand-in
        await rec.stop()
        return handed_in

    assert asyncio.run(go()) == ["inv-ana"]


def test_unwritable_folder_is_reported_not_swallowed(tmp_path):
    """Production regression: the media folder belonged to root, the recorder
    joined, crashed on Permission denied, and nobody was told."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)

    calls = {}

    class API:
        async def run_config(self):
            return {"recording_dir": str(locked / "meetings" / "abc"), "room": "r"}

        async def status(self, state="", **kw):
            calls["status"] = (state, kw.get("error", ""))

        async def close(self):
            calls["closed"] = True

    class Ctx:
        job = SimpleNamespace(metadata="{}")
        connected = False

        async def connect(self, **kw):
            Ctx.connected = True

        def shutdown(self, reason=""):
            calls["shutdown"] = reason

    import cc_agents.scribe as sc
    orig = sc.PlatformAPI.from_metadata
    sc.PlatformAPI.from_metadata = staticmethod(lambda raw: (API(), {}))
    try:
        asyncio.run(sc.entrypoint(Ctx()))
    finally:
        sc.PlatformAPI.from_metadata = orig
        locked.chmod(0o700)
    assert calls["status"][0] == "failed"
    assert "cannot write" in calls["status"][1]
    assert calls.get("closed") and "shutdown" in calls
    assert not Ctx.connected            # never joined the room just to vanish
