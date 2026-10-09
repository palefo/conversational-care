"""Writing one person's audio against the wall clock.

A recorded track has to line up with everyone else's afterwards, or the
transcript interleaves turns in the wrong order and the mix plays people
talking over each other. But a WebRTC stream does not deliver audio evenly:
muting stops it, packet loss thins it, a reconnect pauses it, and frames arrive
in bursts. Writing samples back to back would let each file drift from real
time by however long those gaps add up to.

So this writer keeps every file pinned to the clock. It knows the wall-clock
time its first sample stands for; on each frame it compares where the file is
with where the clock says it should be, pads silence when the file has fallen
behind (a mute, a gap), and drops samples when it has run ahead (a burst after
a stall). Drift is held within ``TOLERANCE_MS``.

Output is Ogg/Opus, mono, at 16 kHz and ~24 kbit/s — about 11 MB an hour, so a
long meeting stays under Whisper's 25 MB upload limit without re-encoding.
"""
from __future__ import annotations

import os
import time

import av
import numpy as np

TOLERANCE_MS = 250
OPUS_FRAME = 320  # 20 ms at 16 kHz


def now_ms() -> int:
    return int(time.time() * 1000)


class WallclockWriter:
    def __init__(self, path: str, *, sample_rate: int = 16000, bitrate: int = 24000):
        self.path = path
        self.rate = sample_rate
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._container = av.open(path, mode="w", format="ogg")
        self._stream = self._container.add_stream("libopus", rate=sample_rate)
        self._stream.layout = "mono"
        self._stream.bit_rate = bitrate
        self._fifo = av.AudioFifo()
        self._pts = 0
        self.start_ms: int | None = None
        self.written = 0          # samples committed to the file (incl. padding)
        self.padded = 0
        self.dropped = 0
        self.closed = False

    # How far into the file the clock says we should be, in samples.
    def _expected(self, at_ms: int) -> int:
        return int((at_ms - self.start_ms) * self.rate / 1000)

    @property
    def duration_s(self) -> float:
        return self.written / float(self.rate)

    @property
    def end_ms(self) -> int | None:
        if self.start_ms is None:
            return None
        return self.start_ms + int(self.written * 1000 / self.rate)

    def write(self, pcm: np.ndarray, at_ms: int | None = None) -> None:
        """Append one frame of mono int16 PCM that arrived at ``at_ms``."""
        if self.closed:
            return
        at_ms = now_ms() if at_ms is None else at_ms
        pcm = np.asarray(pcm, dtype=np.int16).reshape(-1)
        n = pcm.shape[0]
        if self.start_ms is None:
            # The first frame's samples ended at at_ms, so it began n samples
            # earlier: that is the moment this file stands for.
            self.start_ms = at_ms - int(n * 1000 / self.rate)

        tol = int(TOLERANCE_MS * self.rate / 1000)
        # Where the end of this frame belongs on the clock.
        target_end = self._expected(at_ms)
        gap = target_end - (self.written + n)
        if gap > tol:
            # Fallen behind: the stream went quiet (mute, loss). Fill with
            # silence up to where this frame should start.
            self._push(np.zeros(gap, dtype=np.int16))
            self.padded += gap
        elif gap < -tol:
            # Ran ahead: a burst after a stall. Drop what would push past the
            # clock by more than the tolerance.
            over = -gap
            if over >= n:
                self.dropped += n
                return
            pcm = pcm[over:]
            self.dropped += over
        self._push(pcm)

    def pad_to(self, at_ms: int) -> None:
        """Extend with silence up to ``at_ms`` (used when a track ends)."""
        if self.closed or self.start_ms is None:
            return
        missing = self._expected(at_ms) - self.written
        if missing > 0:
            self._push(np.zeros(missing, dtype=np.int16))
            self.padded += missing

    def _push(self, pcm: np.ndarray) -> None:
        if pcm.size == 0:
            return
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = self.rate
        self._fifo.write(frame)
        self.written += pcm.shape[0]
        while self._fifo.samples >= OPUS_FRAME:
            self._encode(self._fifo.read(OPUS_FRAME))

    def _encode(self, frame) -> None:
        frame.pts = self._pts
        self._pts += frame.samples
        for packet in self._stream.encode(frame):
            self._container.mux(packet)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        rest = self._fifo.samples
        if rest:
            tail = self._fifo.read(rest)
            pad = OPUS_FRAME - rest
            if pad > 0:
                # Opus wants whole frames: finish the last one with silence.
                arr = np.concatenate([tail.to_ndarray().reshape(-1),
                                      np.zeros(pad, dtype=np.int16)])
                tail = av.AudioFrame.from_ndarray(arr.reshape(1, -1), format="s16", layout="mono")
                tail.sample_rate = self.rate
            self._encode(tail)
        for packet in self._stream.encode(None):
            self._container.mux(packet)
        self._container.close()
