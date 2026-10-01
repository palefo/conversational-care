"""Link Worker on WhatsApp: a navigator's own phone as a way into Link Worker v2.

A link worker at a client's door can ask the assistant what they would otherwise
open the web app for — what she said last time, how her sleep has been across
the calls, who her caregiver is — by WhatsApp message or voice note.

Three things make that safe enough to offer:

* **The number is proven, not typed in.** A navigator asks for a code on their
  profile page and sends it from the phone itself (``start_link`` / ``LINK``).
  Only then is the number theirs. A number that is also on a client's or a
  caregiver's record is never treated as staff: the two cannot be told apart.
* **One client at a time, chosen by the link worker.** The assistant answers
  about the client they have *loaded* and no other. Today's meetings are offered
  as the obvious choices; any of their own clients can be loaded instead — to
  prepare for a later meeting, or for any other reason. Every load is logged,
  and the loaded client is put down after a few idle hours.
* **Navigators only.** An admin can see every client; that is too much to put
  behind a phone number, so admins use the web app.

Off by default (``LINK_WORKER_WHATSAPP_ENABLED``, which also needs Link Worker v2
on). While off, ``handle_text`` and ``voice_link`` answer None and WhatsApp
behaves exactly as it did. See link_worker_whatsapp.md.
"""
from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import timedelta

from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .site_config import get_bool, get_setting

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=15)
MAX_CODE_TRIES = 5
# A new conversation after this long without a message, as for clients.
SESSION_IDLE = timedelta(hours=2)
# The loaded client is put down after this long without a message: a visit and
# the journey home, not the rest of the week on a phone that might be lost.
LOAD_IDLE = timedelta(hours=4)

LINK_RE = re.compile(r"^\s*(?:link|vincular)\s+(\d{6})\s*$", re.IGNORECASE)
END_COMMANDS = {"/quit", "/restart", "/end", "/sair"}

NATIVE_KEY = "link_worker_whatsapp"


class LinkError(Exception):
    """A link that cannot be started, with wording safe to show the navigator."""


# ----------------------------------------------------------------------------
# Switches and eligibility
# ----------------------------------------------------------------------------

def enabled() -> bool:
    return get_bool("LINK_WORKER_WHATSAPP_ENABLED") and get_bool("LINK_WORKER_V2_ENABLED")


def eligible(user) -> bool:
    """Navigators, not admins — see the module docstring."""
    from .roles import is_admin, is_navigator

    return bool(user and user.is_active and is_navigator(user) and not is_admin(user))


def number_conflict(number) -> bool:
    """Whether this number is also a client's or a caregiver's."""
    from .models import Patient

    return (Patient.objects.filter(phone_number=number).exists()
            or Patient.objects.filter(caregiver__phone_number=number).exists())


def _normalise(number) -> str:
    from .message_attribution import normalise
    return normalise(number) or ""


# ----------------------------------------------------------------------------
# Linking a number
# ----------------------------------------------------------------------------

def _code_hash(code: str, number: str) -> str:
    return salted_hmac("staff-whatsapp-link", f"{code}:{number}").hexdigest()


def start_link(user, number) -> str:
    """Begin linking ``number`` to ``user``; return the code they must send."""
    from .models import StaffWhatsAppLink

    if not enabled():
        raise LinkError("Link Worker on WhatsApp is not switched on.")
    if not eligible(user):
        raise LinkError("Only navigators can use the Link Worker on WhatsApp.")
    number = _normalise(number)
    if not number or not number.startswith("+"):
        raise LinkError("Enter your mobile number in international form, e.g. +5511912345678.")
    if number_conflict(number):
        raise LinkError("That number is on a client's or a caregiver's record, so it cannot "
                        "also be a staff number. Ask an admin to sort out which it is.")
    if (StaffWhatsAppLink.objects.filter(phone_number=number, verified_at__isnull=False)
            .exclude(user=user).exists()):
        raise LinkError("That number is already linked to another account.")

    code = f"{secrets.randbelow(10 ** 6):06d}"
    StaffWhatsAppLink.objects.update_or_create(user=user, defaults={
        "phone_number": number, "verified_at": None,
        "code_hash": _code_hash(code, number),
        "code_expires_at": timezone.now() + CODE_TTL, "code_tries": 0,
        "thread_id": "", "loaded_patient": None, "loaded_at": None,
    })
    return code


def unlink(user) -> None:
    from .models import StaffWhatsAppLink
    StaffWhatsAppLink.objects.filter(user=user).delete()


def _verify(number: str, code: str):
    """Answer a LINK message, or None when no link is waiting for this number."""
    from .models import StaffWhatsAppLink

    link = (StaffWhatsAppLink.objects.select_related("user")
            .filter(phone_number=number, verified_at__isnull=True).first())
    if link is None or not link.code_hash:
        return None
    if (link.code_expires_at is None or link.code_expires_at < timezone.now()
            or link.code_tries >= MAX_CODE_TRIES):
        return ("That code has expired. Ask for a new one on your profile page. / "
                "Esse código expirou. Peça um novo na sua página de perfil.")
    if not constant_time_compare(link.code_hash, _code_hash(code, number)):
        link.code_tries += 1
        link.save(update_fields=["code_tries"])
        return "That code is not right. / Esse código não está correto."
    if number_conflict(number) or not eligible(link.user):
        return "This number cannot be linked. Ask an admin. / Este número não pode ser vinculado."
    link.verified_at = timezone.now()
    link.code_hash, link.code_expires_at, link.code_tries = "", None, 0
    link.save(update_fields=["verified_at", "code_hash", "code_expires_at", "code_tries"])
    return ("Linked. Ask me about your clients — say which one to load, or ask what you "
            "have on today. / Vinculado. Pergunte sobre seus clientes — diga qual carregar, "
            "ou pergunte o que tem para hoje.")


# ----------------------------------------------------------------------------
# Recognising a staff number
# ----------------------------------------------------------------------------

def staff_link_for(number):
    """The verified link for this number, or None — while the feature is on."""
    from .models import StaffWhatsAppLink

    if not enabled():
        return None
    number = _normalise(number)
    if not number:
        return None
    link = (StaffWhatsAppLink.objects.select_related("user", "loaded_patient")
            .filter(phone_number=number, verified_at__isnull=False).first())
    if link is None or not eligible(link.user):
        return None
    if number_conflict(number):
        # Linked before the number turned up on a client's record. Refuse rather
        # than guess which person is writing, and say so in the log.
        logger.warning("Staff WhatsApp number of %s is also on a client record; not routed.",
                       link.user.get_username())
        return None
    return link


voice_link = staff_link_for


# ----------------------------------------------------------------------------
# The session
# ----------------------------------------------------------------------------

def _touch(link):
    """Roll the conversation and put the loaded client down after idle time."""
    now = timezone.now()
    idle = now - link.last_message_at if link.last_message_at else None
    fields = ["last_message_at"]
    if not link.thread_id or idle is None or idle > SESSION_IDLE:
        link.thread_id = str(uuid.uuid4())
        fields.append("thread_id")
    if link.loaded_patient_id and (idle is None or idle > LOAD_IDLE):
        link.loaded_patient, link.loaded_at = None, None
        fields += ["loaded_patient", "loaded_at"]
    link.last_message_at = now
    link.save(update_fields=fields)


def _end_session(link) -> str:
    link.thread_id = str(uuid.uuid4())
    link.loaded_patient, link.loaded_at = None, None
    link.last_message_at = timezone.now()
    link.save(update_fields=["thread_id", "loaded_patient", "loaded_at", "last_message_at"])
    return "Session ended; no client is loaded. / Sessão encerrada; nenhum cliente carregado."


def _v2_agent():
    from .models import Agent
    return Agent.objects.filter(kind=Agent.Kind.NATIVE, native_key="link_worker_v2").first()


def _ask(link, text: str, *, mode: str, language: str = "") -> str:
    """Run the WhatsApp assistant for this link worker and return its reply."""
    from .native_agents import run_native

    agent = _v2_agent()
    model_name = ((getattr(agent, "model", "") or "").strip() or None) if agent else None
    user = link.user
    configurable = {
        "staff_user_id": user.pk,
        "is_admin": False,
        "user_name": (user.get_full_name() or user.get_username()).strip(),
        "reply_mode": mode,
        "spoken_language": language,
    }
    reply = run_native(NATIVE_KEY, link.thread_id, text, configurable, model_name)
    link.refresh_from_db(fields=["loaded_patient", "loaded_at"])
    return reply


def _with_header(link, reply: str) -> str:
    """Which client is loaded, above every reply, so it is never in doubt."""
    p = link.loaded_patient
    if p is None:
        return reply
    return f"📋 {p.name} {p.lastname}\n\n{reply}"


def handle_text(number, text: str, channel: str = "whatsapp"):
    """Staff routing for one inbound text. None: not a staff message; carry on."""
    from .message_attribution import normalise
    from .models import Message
    from .utils import save_message

    if channel != "whatsapp" or not enabled():
        return None
    number = normalise(number) or ""
    body = (text or "").strip()

    m = LINK_RE.match(body)
    if m:
        answer = _verify(number, m.group(1))
        if answer is not None:
            return answer

    link = staff_link_for(number)
    if link is None:
        return None
    _touch(link)
    if body.lower() in END_COMMANDS:
        return _end_session(link)

    reply = _with_header(link, _ask(link, body, mode="text"))
    save_message(link.user.get_username(), body, reply, link.thread_id,
                 account=link.user, sender_role=Message.SenderRole.STAFF)
    return reply


# ----------------------------------------------------------------------------
# Voice notes
# ----------------------------------------------------------------------------

def transcribe_with_language(path: str) -> tuple[str, str]:
    """Whisper's text and the language it heard ("portuguese", "english", …)."""
    from .utils import _openai_client

    with open(path, "rb") as f:
        result = _openai_client().audio.transcriptions.create(
            model="whisper-1", file=f, response_format="verbose_json")
    return ((getattr(result, "text", "") or "").strip(),
            (getattr(result, "language", "") or "").strip().lower())


def is_portuguese(language: str) -> bool:
    return (language or "").lower().startswith(("portug", "pt"))


def voice_for(language: str) -> str:
    """The voice for a spoken reply: the pt-BR one for Portuguese, else v2's own."""
    from .utils import resolve_tts_voice_id

    pt_br = (get_setting("LINK_WORKER_VOICE_PT_BR") or "").strip()
    if pt_br and is_portuguese(language):
        return pt_br
    return resolve_tts_voice_id(_v2_agent())


def handle_voice(link, in_path: str, in_name: str):
    """Answer a voice note: transcribe, ask, and speak the reply back.

    Returns ``(reply_text, out_name, message)``. ``out_name`` is empty when the
    reply could not be spoken — the text still goes back, because a link worker
    at a door with no answer at all is worse off than one reading it.
    """
    from .models import Message
    from .utils import save_message, synthesize_speech_elevenlabs

    _touch(link)
    try:
        transcript, language = transcribe_with_language(in_path)
    except Exception:
        logger.exception("Staff voice note could not be transcribed")
        transcript, language = "", ""
    if not transcript:
        reply = ("I could not make out that voice note — could you type it? / "
                 "Não consegui entender a mensagem de voz — pode escrever?")
        msg = save_message(link.user.get_username(), "", reply, link.thread_id,
                           input_audio_file=in_name, account=link.user,
                           sender_role=Message.SenderRole.STAFF)
        return reply, "", msg

    if transcript.strip().lower() in END_COMMANDS:
        reply, spoken = _end_session(link), ""
    else:
        spoken = _ask(link, transcript, mode="voice", language=language)
        reply = _with_header(link, spoken)

    out_name = ""
    if spoken:
        name = f"{timezone.now():%Y%m%d%H%M%S}_staff_{link.user_id}_out.mp3"
        try:
            synthesize_speech_elevenlabs(spoken, name, voice_id=voice_for(language))
            out_name = name
        except Exception:
            logger.exception("Staff voice reply could not be synthesised; sending text only")

    msg = save_message(link.user.get_username(), transcript, reply, link.thread_id,
                       input_audio_file=in_name, response_audio_file=out_name,
                       account=link.user, sender_role=Message.SenderRole.STAFF)
    return reply, out_name, msg


def voice_note_reply(link, media_url: str, content_type: str):
    """Fetch a WhatsApp voice note from Twilio and answer it (see handle_voice).

    Returns ``(reply_text, out_name, message)``; the callers only differ in how
    they send it back — TwiML inline, or the REST API from a worker.
    """
    import os

    from django.conf import settings

    from .utils import download_twilio_media, ext_for_content_type, save_bytes

    try:
        audio = download_twilio_media(media_url)
    except Exception:
        logger.exception("Staff voice note could not be downloaded")
        audio = b""
    if not audio:
        return ("I could not download that voice note — could you type it? / "
                "Não consegui baixar a mensagem de voz — pode escrever?"), "", None
    in_name = f"{timezone.now():%Y%m%d%H%M%S}_staff_{link.user_id}_in{ext_for_content_type(content_type)}"
    os.makedirs(settings.VOICE_RECORDINGS_DIR, exist_ok=True)
    in_path = os.path.join(settings.VOICE_RECORDINGS_DIR, in_name)
    save_bytes(in_path, audio)
    return handle_voice(link, in_path, in_name)
