"""Text to speech: one entry point, two providers.

Every spoken reply on the platform — a client's WhatsApp voice note, the
external chat's voice mode, the agent test page, a link worker's voice note —
goes through ``synthesize_speech``. Which provider speaks is one platform
setting, ``TTS_PROVIDER``:

* ``elevenlabs`` — the original, and still the default, so an installation that
  sets nothing hears no change;
* ``azure`` — Azure AI Speech neural voices, over one REST call.

A voice name means nothing to the other provider, so an agent carries one of
each — ``Agent.tts_voice_id`` for ElevenLabs, ``Agent.azure_voice`` for Azure —
and switching provider never leaves an agent pointing at a voice the new one
cannot find. See text_to_speech.md.
"""
import logging
import os
import re
from urllib.parse import urlparse
from xml.sax.saxutils import escape, quoteattr

import requests
from django.conf import settings

from .site_config import get_setting

logger = logging.getLogger(__name__)

ELEVENLABS = "elevenlabs"
AZURE = "azure"
PROVIDERS = (ELEVENLABS, AZURE)

# Multilingual: it speaks whatever language the reply is in, so an agent with no
# voice of its own still sounds right to a Spanish or Portuguese speaker.
DEFAULT_AZURE_VOICE = "en-GB-AdaMultilingualNeural"
# Native Brazilian, for a link worker's Portuguese voice note when Settings has
# no other choice.
DEFAULT_AZURE_VOICE_PT_BR = "pt-BR-FranciscaNeural"

# MP3, like ElevenLabs' output: every caller names its file .mp3 and serves it
# as audio/mpeg, and Twilio forwards it to WhatsApp as is.
AZURE_OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
AZURE_TIMEOUT = 30

_REGION_RE = re.compile(r"^[a-z0-9]+$")


class TTSError(RuntimeError):
    """Speech could not be produced. Callers decide whether text alone will do."""


def provider() -> str:
    """The active provider. Anything unrecognised means ElevenLabs, as before."""
    value = (get_setting("TTS_PROVIDER") or "").strip().lower()
    return value if value in PROVIDERS else ELEVENLABS


def is_azure() -> bool:
    return provider() == AZURE


def agent_voice(agent=None) -> str:
    """The voice this agent has set for the active provider, or ``""``."""
    if agent is None:
        return ""
    field = "azure_voice" if is_azure() else "tts_voice_id"
    return (getattr(agent, field, "") or "").strip()


def voice_for_agent(agent=None) -> str:
    """The voice to speak an agent's reply in, for the active provider.

    The agent's own voice, then the platform default for that provider.
    """
    if is_azure():
        return (agent_voice(agent)
                or (get_setting("AZURE_SPEECH_VOICE") or "").strip()
                or DEFAULT_AZURE_VOICE)
    from .utils import resolve_tts_voice_id
    return resolve_tts_voice_id(agent)


def synthesize_speech(text: str, filename: str, *, agent=None, voice=None) -> str:
    """Speak ``text`` into ``VOICE_RECORDINGS_DIR/filename`` and return the path.

    ``voice`` overrides the agent's; it must be a voice of the active provider.
    Raises on failure — every caller already decides what a reply without a
    voice looks like.
    """
    voice = (voice or "").strip() or voice_for_agent(agent)
    if is_azure():
        return synthesize_speech_azure(text, filename, voice)
    from .utils import synthesize_speech_elevenlabs
    return synthesize_speech_elevenlabs(text, filename, voice_id=voice)


# Azure AI Speech
# ----------------------------------------------------------------------------

def azure_region() -> str:
    """The Speech resource's region, e.g. ``uksouth``.

    The portal shows the resource's endpoint next to its key, so a pasted
    endpoint (``https://uksouth.api.cognitive.microsoft.com/``) is read for the
    region it names rather than rejected.
    """
    raw = (get_setting("AZURE_SPEECH_REGION") or "").strip().lower()
    if "://" in raw:
        raw = (urlparse(raw).hostname or "").split(".")[0]
    return raw if _REGION_RE.match(raw) else ""


def azure_configured() -> bool:
    return bool((get_setting("AZURE_SPEECH_KEY") or "").strip() and azure_region())


# The voice fields stay free text — any voice name works, including ones newer
# than this list — but offer the region's voices as suggestions, so choosing one
# is a search for "Brazil" rather than a trip to the Azure docs.
VOICES_CACHE_TTL = 24 * 3600
VOICES_RETRY_TTL = 5 * 60
VOICES_TIMEOUT = 5

# Shown when the live list cannot be had (no key yet, or Azure unreachable).
COMMON_AZURE_VOICES = [
    ("en-GB-AdaMultilingualNeural", "Ada — English (United Kingdom), female, multilingual"),
    ("en-GB-OllieMultilingualNeural", "Ollie — English (United Kingdom), male, multilingual"),
    ("en-GB-SoniaNeural", "Sonia — English (United Kingdom), female"),
    ("en-GB-RyanNeural", "Ryan — English (United Kingdom), male"),
    ("en-US-AvaMultilingualNeural", "Ava — English (United States), female, multilingual"),
    ("en-US-AndrewMultilingualNeural", "Andrew — English (United States), male, multilingual"),
    ("pt-BR-FranciscaNeural", "Francisca — Portuguese (Brazil), female"),
    ("pt-BR-AntonioNeural", "Antonio — Portuguese (Brazil), male"),
    ("pt-BR-ThalitaMultilingualNeural", "Thalita — Portuguese (Brazil), female, multilingual"),
    ("pt-BR-MacerioMultilingualNeural", "Macerio — Portuguese (Brazil), male, multilingual"),
    ("es-ES-ElviraNeural", "Elvira — Spanish (Spain), female"),
    ("es-PE-CamilaNeural", "Camila — Spanish (Peru), female"),
    ("es-PE-AlexNeural", "Alex — Spanish (Peru), male"),
    ("ko-KR-SunHiNeural", "Sun-Hi — Korean (Korea), female"),
]


def _voice_label(v: dict) -> str:
    label = f"{v.get('DisplayName') or v.get('LocalName') or v['ShortName']} — {v.get('LocaleName', '')}"
    if v.get("Gender"):
        label += f", {v['Gender'].lower()}"
    if v.get("SecondaryLocaleList"):
        label += ", multilingual"
    return label


def azure_voices() -> list:
    """``[(voice name, label)]`` for the configured region, sorted by language.

    Fetched once a day and cached; the common voices above when it cannot be.
    Never raises — this only feeds suggestions on a settings page.
    """
    from django.core.cache import cache

    key, region = (get_setting("AZURE_SPEECH_KEY") or "").strip(), azure_region()
    if not (key and region):
        return COMMON_AZURE_VOICES
    cache_key = f"azure_tts_voices:{region}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        resp = requests.get(
            f"https://{region}.tts.speech.microsoft.com/cognitiveservices/voices/list",
            headers={"Ocp-Apim-Subscription-Key": key}, timeout=VOICES_TIMEOUT)
        resp.raise_for_status()
        voices = sorted(((v["ShortName"], _voice_label(v)) for v in resp.json() if v.get("ShortName")),
                        key=lambda nv: (nv[1].split(" — ", 1)[-1], nv[0]))
    except Exception as exc:
        logger.warning("Azure voice list unavailable (%s); offering the common voices",
                       exc.__class__.__name__)
        cache.set(cache_key, COMMON_AZURE_VOICES, VOICES_RETRY_TTL)
        return COMMON_AZURE_VOICES
    voices = voices or COMMON_AZURE_VOICES
    cache.set(cache_key, voices, VOICES_CACHE_TTL)
    return voices


def _clean(text: str) -> str:
    # The same clean-up the ElevenLabs path does: markdown emphasis and headings
    # are read out as symbols otherwise.
    return (text or "").replace("*", "").replace("#", "").strip()


def ssml(text: str, voice: str) -> str:
    """One voice speaking ``text``, which is escaped: a reply is model output."""
    parts = voice.split("-")
    locale = "-".join(parts[:2]) if len(parts) >= 3 else "en-US"
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang={quoteattr(locale)}><voice name={quoteattr(voice)}>'
            f'{escape(_clean(text))}</voice></speak>')


def synthesize_speech_azure(text: str, filename: str, voice: str = "") -> str:
    key = (get_setting("AZURE_SPEECH_KEY") or "").strip()
    region = azure_region()
    if not (key and region):
        raise TTSError("Azure Speech is not configured: set its key and region in "
                       "Settings → Agents → Text to speech.")
    if not _clean(text):
        raise TTSError("Nothing to speak.")
    voice = (voice or "").strip() or DEFAULT_AZURE_VOICE

    try:
        resp = requests.post(
            f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
            data=ssml(text, voice).encode("utf-8"),
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": AZURE_OUTPUT_FORMAT,
                "User-Agent": "conversational-care",
            },
            timeout=AZURE_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TTSError(f"Azure Speech could not be reached: {exc.__class__.__name__}") from exc

    if resp.status_code != 200 or not resp.content:
        hint = {401: " (check the key and region)",
                400: f" (is '{voice}' an Azure voice name?)",
                429: " (rate limited)"}.get(resp.status_code, "")
        raise TTSError(f"Azure Speech answered HTTP {resp.status_code}{hint}")

    outdir = settings.VOICE_RECORDINGS_DIR
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, filename)
    with open(path, "wb") as fh:
        fh.write(resp.content)
    return path
