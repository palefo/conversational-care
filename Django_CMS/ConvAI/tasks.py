# ConvAI/tasks.py
import os
import uuid
from django.conf import settings
from django.urls import reverse
from .site_config import get_setting
from django.utils import timezone
from django.db.models import Q

from .models import Message, Patient
from .utils import (
    process_received_message,
    download_twilio_media,
    is_audio_content_type,
    ext_for_content_type,
    save_bytes,
    transcribe_audio,
    generate_response_langgraph,
    synthesize_speech_elevenlabs,
    resolve_tts_voice_id,
    build_signed_download_token,
    send_whatsapp_text,
    send_sms_text,
    get_platform_phone,
)

def job_reply_text(channel: str, from_number_raw: str, body_text: str) -> None:
    """
    channel: 'whatsapp' or 'sms'
    from_number_raw: 'whatsapp:+NNN...' or '+NNN...'
    body_text: incoming text (already merged with any media note)
    """
    phone = from_number_raw.replace("whatsapp:", "").strip()
    reply = process_received_message(phone, body_text)
    if channel == "whatsapp":
        send_whatsapp_text(phone, reply)
    else:
        send_sms_text(phone, reply)

def job_process_whatsapp_audio(
    from_number_raw: str,
    media_url: str,
    content_type: str,
    body_text: str,
    site_root: str,   # <- NEW: passed from webhook, e.g. "https://platform.conversational-care.ai"
) -> None:
    """
    Download WA audio, transcribe, call agent, TTS, persist, and reply (text+audio).
    """
    phone = from_number_raw.replace("whatsapp:", "").strip()
    patient = (
        Patient.objects
        .filter(Q(phone_number=phone) | Q(caregiver__phone_number=phone))
        .select_related("agent")
        .first()
    )

    # Download audio if possible
    try:
        audio_bytes = download_twilio_media(media_url)
    except Exception:
        audio_bytes = b""

    transcript = ""
    in_name = ""
    if audio_bytes:
        ts = timezone.now().strftime("%Y%m%d%H%M%S")
        in_ext  = ext_for_content_type(content_type)
        in_name = f"{ts}_wa_in{in_ext}"
        voice_dir = settings.VOICE_RECORDINGS_DIR
        os.makedirs(voice_dir, exist_ok=True)
        in_path = os.path.join(voice_dir, in_name)
        save_bytes(in_path, audio_bytes)
        try:
            transcript = transcribe_audio(in_path)
        except Exception:
            transcript = "I could not transcribe the audio."

    # If no patient, just process as plain text using whatever transcript we got
    if not patient:
        body = (f"<number of attached files: 1> {body_text} {transcript}").strip() if transcript else (body_text or "")
        reply = process_received_message(phone, body)
        send_whatsapp_text(phone, reply)
        return

    # Reset command?
    text = (transcript or "").strip()
    if text.lower() in ("/quit", "/restart"):
        new_id = str(uuid.uuid4())
        patient.current_thread_id = new_id
        patient.save(update_fields=["current_thread_id"])
        send_whatsapp_text(phone, "Conversation reset. How can I help you now?")
        return

    # Active thread and agent reply
    thread_id = patient.current_thread_id or str(uuid.uuid4())
    if not patient.current_thread_id:
        patient.current_thread_id = thread_id
        patient.save(update_fields=["current_thread_id"])

    reply_text = generate_response_langgraph(patient, text or (body_text or ""), thread_id)

    # TTS
    out_name = f"{timezone.now().strftime('%Y%m%d%H%M%S')}_wa_out.mp3"
    voice_id = resolve_tts_voice_id(getattr(patient, "agent", None))
    synthesize_speech_elevenlabs(reply_text, out_name, voice_id=voice_id)

    # Persist message. This path writes the row itself rather than going
    # through save_message, so it has to scrub a spoken/typed Sensei passcode
    # on its own — see ConvAI.sensei.redact.
    from .sensei import redact as _redact_credentials
    msg = Message.objects.create(
        user=phone,
        conversation_id=thread_id,
        user_message=_redact_credentials(transcript or (body_text or "")),
        response_message=reply_text,
        input_audio_file=in_name or "",
        response_audio_file=out_name,
    )

    # Signed URL for Twilio to fetch the audio (via your download view)
    token = build_signed_download_token(msg.id, "output", ttl_seconds=600)
    rel = reverse("twilio_audio_download", args=[msg.id, "output"]) + f"?t={token}"
    media_url_signed = f"{site_root}{rel}"

    # Send WA with text + audio
    from twilio.rest import Client as TwClient
    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token  = get_setting("TWILIO_AUTH_TOKEN")
    platform_phone = get_platform_phone()

    if account_sid and auth_token and platform_phone and media_url_signed:
        try:
            tw = TwClient(account_sid, auth_token)
            tw.messages.create(
                from_=f"whatsapp:{platform_phone}",
                to=f"whatsapp:{phone}",
                body=reply_text,
                media_url=[media_url_signed],
            )
            return
        except Exception:
            pass

    # Fallback to plain text if media send fails
    send_whatsapp_text(phone, reply_text)