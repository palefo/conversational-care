"""The audio-processing seam.

Every stretch of audio an agent hears or records passes through
``input_processor()``. Today it returns None — no processing beyond what each
browser already applies (echo cancellation, noise suppression, gain control).

To add a denoiser later (DeepFilterNet, DTLN, …), return an
``rtc.FrameProcessor[rtc.AudioFrame]`` here: LiveKit applies it to the voice
agents' input (``AudioInputOptions.noise_cancellation``) and to the recorder's
streams (``rtc.AudioStream(noise_cancellation=...)``) with nothing else to
change. ``CC_DENOISE`` selects it, so a build can carry one switched off.
"""
from __future__ import annotations

import os


def input_processor(purpose: str = "agent"):
    """A FrameProcessor for ``purpose`` ("agent" or "recording"), or None."""
    choice = (os.getenv("CC_DENOISE") or "").strip().lower()
    if not choice or choice in ("0", "off", "none"):
        return None
    # Placeholder for a future processor. Unknown names fall back to none
    # rather than failing a meeting over an optional improvement.
    return None
