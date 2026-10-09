"""The recorder's clock discipline: files stay aligned to real time through
mutes, packet loss and bursts, and decode to the expected length."""
import av
import numpy as np

from cc_agents.wallclock import TOLERANCE_MS, WallclockWriter

RATE = 16000
FRAME = 160  # 10 ms


def tone(n=FRAME):
    return (np.sin(np.arange(n) / 7.0) * 6000).astype(np.int16)


def decoded_seconds(path):
    with av.open(path) as c:
        samples = sum(f.samples for f in c.decode(audio=0))
        rate = c.streams.audio[0].rate
    return samples / rate


def test_steady_stream_matches_clock(tmp_path):
    w = WallclockWriter(str(tmp_path / "a.ogg"))
    t = 1_000_000
    for i in range(300):                  # 3 s, a frame every 10 ms
        t += 10
        w.write(tone(), t)
    w.close()
    assert w.padded == 0 and w.dropped == 0
    assert abs(w.duration_s - 3.0) < 0.02
    assert abs(decoded_seconds(w.path) - 3.0) < 0.1


def test_gap_is_filled_with_silence(tmp_path):
    """A 2-second mute: the file keeps time instead of closing the gap."""
    w = WallclockWriter(str(tmp_path / "b.ogg"))
    t = 5_000
    for _ in range(100):                  # 1 s
        t += 10
        w.write(tone(), t)
    t += 2_000                            # nothing for 2 s
    for _ in range(100):                  # 1 s more
        t += 10
        w.write(tone(), t)
    w.close()
    assert w.padded > 0
    # Total span is 4 s of wall clock; the file must be within tolerance of it.
    assert abs(w.duration_s - 4.0) < TOLERANCE_MS / 1000 + 0.02
    assert abs(decoded_seconds(w.path) - 4.0) < 0.35


def test_burst_after_stall_does_not_run_ahead(tmp_path):
    """Frames delivered all at once must not push the file past the clock."""
    w = WallclockWriter(str(tmp_path / "c.ogg"))
    t = 9_000
    for _ in range(50):
        t += 10
        w.write(tone(), t)
    # 100 frames (1 s of audio) arrive within the same 10 ms.
    t += 10
    for _ in range(100):
        w.write(tone(), t)
    w.close()
    wall = (t - w.start_ms) / 1000.0
    assert w.dropped > 0
    assert w.duration_s <= wall + TOLERANCE_MS / 1000 + 0.02


def test_start_time_accounts_for_first_frame(tmp_path):
    w = WallclockWriter(str(tmp_path / "d.ogg"))
    w.write(tone(FRAME), 2_000)
    assert w.start_ms == 2_000 - 10
    w.close()
    assert w.end_ms is not None and w.end_ms >= w.start_ms


def test_empty_writer_closes_cleanly(tmp_path):
    w = WallclockWriter(str(tmp_path / "e.ogg"))
    w.close()
    assert w.written == 0
