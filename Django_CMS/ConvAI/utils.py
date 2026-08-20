# ConvAI/utils.py

# Standard library
import io
import json
import logging
import os
from .site_config import get_setting, get_bool, brand_name
import uuid
import wave
import hmac
import time
import base64
import hashlib
from datetime import date, datetime, timedelta
from uuid import UUID
from typing import Tuple, Dict, Any, Optional

# Third-party
import requests
from dotenv import load_dotenv
from elevenlabs import VoiceSettings, save
from elevenlabs.client import ElevenLabs
from langgraph.pregel.remote import RemoteGraph
from langgraph_sdk import get_client, get_sync_client
from openai import OpenAI
from pyairtable import Api, Table
from twilio.rest import Client

# Django
from django.conf import settings
from django.core.cache import cache
from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

# Local apps
from .models import CallRecording, Caregiver, Conversation, Message, Patient, Agent

logger = logging.getLogger(__name__)

load_dotenv()  # Load environment variables from .env

def make_phone_conference(phone_numbers, record_ctn=True, record_dyad=True):
    '''
    phone_numbers is a dictionary that has:
      - CTN : Phone number for the Navigator
      - Platform : Phone number for the platform (must be registered in Twilio beforehand)
      - Dyad : Phone number for the Dyad (carer + PlWD)

    record is a boolean value that requests recording of the call in twilio
    '''
    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token = get_setting("TWILIO_AUTH_TOKEN")
    client = Client(account_sid, auth_token)
    conference_name = str(uuid.uuid4()) #to avoid collisions
    # Two channels rather than one mixed track. Whisper cannot tell voices
    # apart, but a dual-channel leg already has them apart: what the platform
    # sent is on one channel and what the person on the other end said is on the
    # other. That is what lets the transcript name a speaker at all. Mono
    # recordings still transcribe, just without anyone attributed — see
    # transcribe_audio. Applies to calls placed from here on; recordings already
    # on disk are mono and cannot be separated after the fact.
    call1 = client.calls.create(
      record=record_ctn,
      recording_channels="dual",
      to=phone_numbers["CTN"],
      from_=phone_numbers["Platform"],
      twiml=f'<Response><Dial><Conference endConferenceOnExit="true">{conference_name}</Conference></Dial></Response>'
    )
    call2 = client.calls.create(
      record=record_dyad,
      recording_channels="dual",
      to=phone_numbers["Dyad"],
      from_=phone_numbers["Platform"],
      twiml=f'<Response><Dial><Conference endConferenceOnExit="true">{conference_name}</Conference></Dial></Response>'
    )
    logger.info("Calls initiated; both participants join the conference on answer.")

def send_sms_with_template(to_e164: str | None, content_sid: str, content_variables: dict | None = None) -> bool:
    """
    Send an SMS via Twilio Content Template (ContentSid).
    - to_e164: destination in +E.164 (e.g., +51999999999)
    - content_sid: Twilio Content Template SID (e.g., HX...).
    - content_variables: dict of template variables (will be json.dumps'd); may be None for templates w/o params.
    Uses TWILIO_SMS_FROM (fallback PLATFORM_PHONE) as the sender.
    Returns True on success, False otherwise.
    """
    if not to_e164 or not content_sid:
        return False

    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token  = get_setting("TWILIO_AUTH_TOKEN")
    from_number = get_setting("TWILIO_SMS_FROM") or get_setting("PLATFORM_PHONE")

    if not (account_sid and auth_token and from_number):
        return False

    try:
        tw = Client(account_sid, auth_token)
        kwargs = {
            "from_": str(from_number),
            "to": str(to_e164),
            "content_sid": content_sid,
        }
        if content_variables:
            kwargs["content_variables"] = json.dumps(content_variables, ensure_ascii=False)

        tw.messages.create(**kwargs)
        return True
    except Exception:
        return False

def send_whatsapp_text(to_e164: str | None, body: str) -> bool:
    """
    Send a plain WhatsApp text via Twilio.
    - to_e164: destination in +E.164 (e.g., +51999999999)
    - body: message text
    Uses PLATFORM_PHONE as the sender (adds 'whatsapp:' automatically).
    Returns True on success, False otherwise.
    """
    if not to_e164 or not body:
        return False

    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token  = get_setting("TWILIO_AUTH_TOKEN")
    platform_phone = get_setting("PLATFORM_PHONE")  # e.g., +14155238886

    if not (account_sid and auth_token and platform_phone):
        return False

    try:
        client = Client(account_sid, auth_token)
        from_whatsapp = f"whatsapp:{platform_phone}" if not str(platform_phone).startswith("whatsapp:") else platform_phone
        to_whatsapp   = f"whatsapp:{to_e164}"     if not str(to_e164).startswith("whatsapp:")     else to_e164
        client.messages.create(
            from_=from_whatsapp,
            to=to_whatsapp,
            body=body.strip()
        )
        return True
    except Exception:
        return False

def get_platform_phone():
    return get_setting("PLATFORM_PHONE")


def _get_or_create_thread(patient: Patient, *, stale_hours: int = 3) -> str:
    """
    Return the patient's stable thread id (UUID4 string).
    - Creates one if missing.
    - Optionally rolls over to a new id if the existing Conversation is stale
      (last_message_at older than `stale_hours`). To disable rollover, comment
      the block marked ROLLOVER LOGIC.
    """
    tid = (patient.current_thread_id or "").strip()
    if tid:
        conv = None
        try:
            conv = Conversation.objects.only("last_message_at", "started_at").get(id=UUID(tid))
        except (Conversation.DoesNotExist, ValueError, TypeError):
            conv = None

        # --- ROLLOVER LOGIC (comment this block to disable) -------------------
        if conv:
            last = conv.last_message_at or conv.started_at
            if last and (timezone.now() - last) >= timedelta(hours=stale_hours):
                new_id = str(uuid.uuid4())
                patient.current_thread_id = new_id
                patient.save(update_fields=["current_thread_id"])
                return new_id
        # --- /ROLLOVER LOGIC --------------------------------------------------

        return tid

    # No thread yet → create one
    new_id = str(uuid.uuid4())
    patient.current_thread_id = new_id
    patient.save(update_fields=["current_thread_id"])
    return new_id

def generate_response_langgraph(user_or_patient,
                                user_message: str,
                                thread_id: str,
                                extra_configurable: dict | None = None,
                                extra_state: dict | None = None) -> str:
    """
    Invoke the agent assigned to a patient/user (via its ``.agent``).
    Returns an English error if no agent / connection fails.
    """
    agent = getattr(user_or_patient, "agent", None)
    return generate_response_with_agent(
        agent, user_or_patient, user_message, thread_id,
        extra_configurable=extra_configurable, extra_state=extra_state,
    )


def generate_response_with_agent(agent,
                                 user_or_patient,
                                 user_message: str,
                                 thread_id: str,
                                 extra_configurable: dict | None = None,
                                 extra_state: dict | None = None) -> str:
    """
    Invoke a specific ``agent`` (native or remote) for the given user/patient.

    Native agents run in-process (async LangGraph graph + Postgres checkpointer
    keyed by ``thread_id``); remote agents run on a LangGraph server (host:port).
    """
    #if user then it has first_name and last_name attributes
    #if patient then it has name and lastname attributes
    name = getattr(user_or_patient, "first_name", None) or getattr(user_or_patient, "name", None)
    lastname = getattr(user_or_patient, "last_name", None) or getattr(user_or_patient, "lastname", None)
    user_name = f"{name} {lastname}".strip() if name or lastname else "User"

    if not agent:
        return "Sorry, no agent is configured for this user."

    kind = getattr(agent, "kind", "remote")

    # In-process agents (native + prompt-based): async graph, Postgres persistence.
    if kind in ("native", "prompt"):
        configurable = {
            "user_id": getattr(user_or_patient, "id", None),
            "user_name": user_name,
        }
        if isinstance(extra_configurable, dict) and extra_configurable:
            configurable.update(extra_configurable)
        model_name = (getattr(agent, "model", "") or "").strip() or None
        if kind == "native":
            from .native_agents import run_native
            return run_native(agent.native_key, thread_id, user_message, configurable, model_name)
        # prompt-based
        from .native_agents import run_prompt_agent
        return run_prompt_agent(agent.system_prompt, thread_id, user_message, configurable, model_name)

    # AS-06/F8 fix: deny SSRF to non-allow-listed agent hosts.
    if not agent_host_allowed(getattr(agent, "host", "")):
        return "Sorry, the configured agent host is not permitted."
    url = f"http://{agent.host}:{agent.port}"
    graph_name = agent.langgraph_name
    try:
        from langgraph_sdk import get_client, get_sync_client
        from langgraph.pregel.remote import RemoteGraph
        client = get_client(url=url)
        sync = get_sync_client(url=url)
        rg = RemoteGraph(graph_name, client=client, sync_client=sync)
    except Exception:
        return "Sorry, could not connect to the configured agent."
    try:
        configurable = {
            "thread_id": thread_id,
            "user_id":   user_or_patient.id,
            "user_name": user_name,
        }
        if isinstance(extra_configurable, dict) and extra_configurable:
            configurable.update(extra_configurable)  # merge/override

        state = {
            "messages": [{"role": "user", "content": user_message}]
        }
        if isinstance(extra_state, dict) and extra_state:
            state.update(extra_state)  # merge/override

        result = rg.invoke(
            state,
            config={"configurable": configurable},
        )
        logger.debug("LangGraph result: %s", result)
        return result["messages"][-1]["content"]
    except Exception:
        return "Sorry, something went wrong generating the response."


def save_message(phone: str, user_message: str, response_message: str, thread_id: str,
                 patient: Patient | None = None,
                 input_audio_file: str = "", response_audio_file: str = ""):
    """
    Persist a message pair and ensure there's a Conversation row.
    - Creates Conversation(id=thread_id) if missing.
    - Updates last_message_at.
    - If available, attaches patient and agent to Conversation.
    - Optional audio filenames are stored on the Message (voice turns).
    Returns the created Message.
    """
    now = timezone.now()
    agent = getattr(patient, "agent", None) if patient is not None else None

    with transaction.atomic():
        # Upsert Conversation by UUID primary key
        try:
            conv_uuid = UUID(str(thread_id))
        except Exception:
            # If thread_id isn't a UUID (shouldn't happen), normalize it:
            conv_uuid = uuid.uuid4()

        conv_defaults = {
            "started_at": now,
            "last_message_at": now,
        }
        # Attach patient/agent *if those fields exist on the model* (safe before migrations)
        if patient and hasattr(Conversation, "patient"):
            conv_defaults["patient"] = patient
        if agent and hasattr(Conversation, "agent"):
            conv_defaults["agent"] = agent

        conv, created = Conversation.objects.get_or_create(id=conv_uuid, defaults=conv_defaults)

        # If existing, update last_message_at and set patient/agent if missing
        if not created:
            updates = {"last_message_at": now}
            if patient and hasattr(conv, "patient_id") and not conv.patient_id:
                updates["patient"] = patient
            if agent and hasattr(conv, "agent_id") and not conv.agent_id:
                updates["agent"] = agent
            if updates:
                for k, v in updates.items():
                    setattr(conv, k, v)
                conv.save(update_fields=list(updates.keys()))

        # Finally, create the Message
        return Message.objects.create(
            user=phone,
            conversation_id=str(conv.id),  # ensure we use the canonical UUID string
            user_message=user_message,
            response_message=response_message,
            input_audio_file=input_audio_file,
            response_audio_file=response_audio_file,
        )


def patient_message_q(patient: Patient) -> Q:
    """Q filter selecting all Message rows that belong to ``patient``.

    Matches by EITHER of the two ways a message can be tied to a patient:
      1. ``Message.user`` is the patient's or caregiver's phone number
         (WhatsApp/webhook traffic stores the sender phone there), OR
      2. the message's conversation is linked to the patient via
         ``Conversation.patient`` (web tester chat and voice chat store a
         username in ``Message.user``, so phone matching alone misses them).
    """
    q = Q(pk__in=[])  # always-false base; OR'ed conditions below widen it
    nums = []
    if patient.phone_number:
        nums.append(str(patient.phone_number))
    caregiver = getattr(patient, "caregiver", None)
    if caregiver and caregiver.phone_number:
        nums.append(str(caregiver.phone_number))
    if nums:
        q |= Q(user__in=nums)
    conv_ids = [
        str(cid) for cid in
        Conversation.objects.filter(patient=patient).values_list("id", flat=True)
    ]
    if conv_ids:
        q |= Q(conversation_id__in=conv_ids)
    return q


# ---------------------------
# Automation lifecycle
# ---------------------------

def automation_context(patient: Patient) -> dict:
    """Build the ``extra_configurable`` context a running automation agent needs.

    Only the meeting + protocol are persisted on the Patient; the rest is
    recomputed from the patient/caregiver so it is always current.
    """
    caregiver = getattr(patient, "caregiver", None)
    caregiver_name = (
        f"{caregiver.name} {caregiver.lastname}".strip() if caregiver else None
    )
    phone_number = (
        str(caregiver.phone_number) if caregiver and caregiver.phone_number else None
    )
    return {
        "meeting_id": patient.automation_meeting_id,
        "protocol_id": patient.automation_protocol,
        "patient_id": patient.id,
        "user_name": f"{patient.name} {patient.lastname}".strip(),
        "caregiver_name": caregiver_name,
        "phone_number": phone_number,
    }


def start_automation(patient: Patient, agent, meeting, protocol_num: int,
                     *, ttl_hours: int = 3) -> str:
    """Assign an automation ``agent`` to ``patient`` and record its context.

    Captures the agent to revert to only if no automation is already running (so
    re-triggering doesn't overwrite the real previous agent with an automation
    agent). Opens a fresh thread and returns its id.
    """
    if not patient.automation_active:
        patient.automation_prev_agent = patient.agent
    patient.agent = agent
    patient.automation_meeting = meeting
    patient.automation_protocol = protocol_num
    patient.automation_expires_at = timezone.now() + timedelta(hours=ttl_hours)
    patient.current_thread_id = str(uuid.uuid4())
    patient.save(update_fields=[
        "agent", "automation_prev_agent", "automation_meeting",
        "automation_protocol", "automation_expires_at", "current_thread_id",
    ])
    return patient.current_thread_id


def end_automation(patient: Patient) -> None:
    """Revert ``patient`` to its pre-automation agent and clear automation state.

    Idempotent: safe to call when no automation is active. Rolls the thread so the
    restored agent starts a fresh conversation.
    """
    patient.agent = patient.automation_prev_agent
    patient.automation_prev_agent = None
    patient.automation_meeting = None
    patient.automation_protocol = None
    patient.automation_expires_at = None
    patient.current_thread_id = str(uuid.uuid4())
    patient.save(update_fields=[
        "agent", "automation_prev_agent", "automation_meeting",
        "automation_protocol", "automation_expires_at", "current_thread_id",
    ])


def touch_automation(patient: Patient, *, ttl_hours: int = 3) -> None:
    """Push the automation's sliding idle deadline forward from now."""
    patient.automation_expires_at = timezone.now() + timedelta(hours=ttl_hours)
    patient.save(update_fields=["automation_expires_at"])


def automation_turn(patient: Patient) -> dict | None:
    """Advance the automation lifecycle for one inbound turn.

    - No automation active -> returns None (normal routing).
    - Active but idle past the deadline -> reverts to the prior agent, returns None.
    - Active and live -> slides the deadline and returns the ``extra_configurable``
      context the automation agent needs this turn.

    Call this before generating a reply so the (native) automation agent always
    sees ``meeting_id``/``protocol_id`` and idle conversations auto-revert.
    """
    if not patient.automation_active:
        return None
    if timezone.now() >= patient.automation_expires_at:
        end_automation(patient)  # sliding idle timeout -> restore prior agent
        return None
    touch_automation(patient)
    return automation_context(patient)


def process_message_for_patient(patient: Patient, raw_message: str, *, user_label: str | None = None) -> str:
    """
    Process a chat message for an already-resolved patient.

    Used by the authenticated external chat (patient is derived from the
    logged-in test user, not from a phone number). ``user_label`` is only a
    display/identifier string stored on the Message; it defaults to a stable,
    non-phone value.
    """
    text = raw_message.strip()
    label = user_label or f"patient:{patient.pk}"

    # 0) the navigator's switch. Both inbound paths land here, so this is the
    # one place that has to honour it. The message is still recorded — what is
    # suspended is the agent answering, not the caregiver being heard.
    if not patient.chatbot_enabled:
        save_message(label, text, "", _get_or_create_thread(patient), patient=patient)
        return ""

    # 1) reset thread on command
    if text.lower() in ("/quit", "/restart"):
        new_uuid = str(uuid.uuid4())
        patient.current_thread_id = new_uuid
        patient.save(update_fields=["current_thread_id"])
        return "Current conversation has been reset."

    # 2) automation lifecycle: revert on idle timeout, else re-inject its context
    #    so the (native) automation agent sees meeting_id/protocol_id every turn.
    extra_configurable = automation_turn(patient)

    # 3) get active (or freshly rolled-over) thread
    thread_id = _get_or_create_thread(patient)

    # 4) call agent
    reply = generate_response_langgraph(patient, text, thread_id,
                                        extra_configurable=extra_configurable)

    # 5) persist both sides and upsert Conversation metadata
    save_message(label, text, reply, thread_id, patient=patient)
    return reply


def process_received_message(phone_number: str, raw_message: str) -> str:
    """
    Main entry for inbound SMS/WhatsApp.
    Resolves the patient by phone, then delegates to
    :func:`process_message_for_patient`.
    """
    phone = phone_number.replace("whatsapp:", "").strip()
    text = raw_message.strip()

    # 1) resolve patient by phone (patient or caregiver)
    patient = (
        Patient.objects
        .filter(Q(phone_number=phone) | Q(caregiver__phone_number=phone))
        .select_related("agent")
        .first()
    )
    if not patient:
        sr_reply = _handle_self_registration_flow(phone, text)
        if sr_reply is not None:
            return sr_reply
        return "Sorry, we could not find a patient matching this number."

    return process_message_for_patient(patient, raw_message, user_label=phone)


def download_recording_mp3(recording_sid):
    account_sid = get_setting("TWILIO_ACCOUNT_SID") 
    auth_token = get_setting("TWILIO_AUTH_TOKEN")
    client = Client(account_sid, auth_token)
    output_dir = settings.CALL_RECORDINGS_DIR

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    if not account_sid or not auth_token:
        logger.error("Twilio credentials not found in environment.")
        return
    try:
        client.recordings(recording_sid).fetch()  # fetch metadata to verify
    except Exception as e:
        logger.error("Unable to fetch recording %s: %s", recording_sid, e)
        return

    recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.mp3"

    try:
        response = requests.get(recording_url, auth=(account_sid, auth_token))
        if response.status_code != 200:
            logger.error("Failed to download recording: HTTP %s", response.status_code)
            return
    except Exception as e:
        logger.error("Network error downloading recording: %s", e)
        return

    file_path = os.path.join(output_dir, f"{recording_sid}.mp3")
    try:
        with open(file_path, "wb") as f:
            f.write(response.content)
        logger.info("Recording %s saved to %s", recording_sid, file_path)
        return file_path
    except Exception as e:
        logger.error("Error saving file %s: %s", file_path, e)




def get_recordings_from_twilio():
    account_sid = get_setting("TWILIO_ACCOUNT_SID") 
    auth_token = get_setting("TWILIO_AUTH_TOKEN") 
    client = Client(account_sid, auth_token)
    last_updated = CallRecording.objects.aggregate(max_value=Max('end_time'))['max_value']
    try:
        calls = client.calls.list() 
    except Exception as e:
        logger.error("Error fetching calls: %s", e)
        calls = []  # proceed with empty list if calls cannot be retrieved
    try:
        recordings = client.recordings.list()  # fetches all recording records
    except Exception as e:
        logger.error("Error fetching recordings: %s", e)
        return  # cannot proceed without recordings data

    call_map = {call.sid: (call.from_formatted, call.to_formatted) for call in calls}

    for rec in recordings:
        if (last_updated is not None) and (rec.date_updated <= last_updated):
            #Only update new recordings!
            continue 
        rec_sid = rec.sid
        call_sid = getattr(rec, "call_sid", None)  # associated Call SID
        if call_sid and call_sid in call_map:
            from_num, to_num = call_map[call_sid]
        else:
            from_num, to_num = "N/A", "N/A"
        start_time = rec.date_created  # datetime object
        end_time = rec.date_updated    # datetime object
        duration = rec.duration or 0
        file_path = download_recording_mp3(rec_sid)
        
        recording = CallRecording.objects.create(
            recording_sid = rec_sid,
            from_number = from_num,
            to_number = to_num,
            start_time = start_time,
            end_time = end_time,
            duration = duration,
            filename = file_path)
        

def get_path_audio(id):
    rec = (
        CallRecording.objects
        .filter(recording_sid=id)
        .first()
    )
    return rec.filename if rec and getattr(rec, "filename", None) else None
    

def send_whatsapp_reminder(meeting):
    #from django.utils import timezone
    """
    Envía un WhatsApp al cuidador de la reunión con fecha y hora de recordatorio.
    - meeting: instancia de Meeting (ya valida permisos fuera de aquí).
    """
    caregiver = meeting.patient.caregiver
    if not caregiver or not caregiver.phone_number:
        raise ValueError("El cuidador no tiene número de teléfono válido.")

    # Hora local formateada
    local_dt = timezone.localtime(meeting.scheduled_time)
    fecha_str = local_dt.strftime("%d/%m/%Y")
    hora_str  = local_dt.strftime("%H:%M")

    # Construir el cuerpo del mensaje
    body = (
        f"Recordatorio de reunión para el cuidador de {meeting.patient}:\n"
        f"🗓 Fecha: {fecha_str}\n"
        f"⏰ Hora: {hora_str}\n\n"
        "Por favor, esté disponible para la llamada."
    )

    # Numbers in WhatsApp format
    platform_phone = get_setting("PLATFORM_PHONE")
    if not platform_phone:
        raise ValueError("PLATFORM_PHONE is not configured (Settings or .env).")
    to_whatsapp   = f"whatsapp:{caregiver.phone_number}"
    from_whatsapp = f"whatsapp:{platform_phone}"

    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token  = get_setting("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise ValueError("Twilio credentials not configured (Settings or .env).")

    client = Client(account_sid, auth_token)
    # SID of the reminder template approved in the Twilio Content API (ContentSid)
    content_sid = get_setting("WHATSAPP_TEMPLATE_SID")
    if not content_sid:
        raise ValueError("WHATSAPP_TEMPLATE_SID is not configured (Settings or .env).")

    # Construir variables para la plantilla en formato JSON
    content_vars = json.dumps({
        "1": "el cuidador de " + str(meeting.patient),  # reemplaza {{1}} con el nombre del paciente
        "2": fecha_str,             # reemplaza {{2}} con la fecha (DD/MM/YYYY)
        "3": hora_str               # reemplaza {{3}} con la hora (HH:MM)
    })

    # Enviar WhatsApp usando ContentSid y ContentVariables
    msg = client.messages.create(
        from_=from_whatsapp,
        to=to_whatsapp,
        content_sid=content_sid,
        content_variables=content_vars
    )

    # Registrar en la base de datos únicamente la parte del sistema
    Message.objects.create(
        conversation_id  = f"reminder-{meeting.id}",
        user             = str(caregiver.phone_number),
        user_message     = "",
        response_message = body
    )
    
    return msg.sid  # devuelve el SID del mensaje si es exitoso

# Role decorators live in ConvAI/roles.py; re-exported here for existing importers.
from .roles import navigator_required, tester_required as patient_tester_required  # noqa: E402,F401



# def synthesize_speech_elevenlabs(text: str, filename: str) -> str:
#     elevenlabs_client = ElevenLabs()    
#     cleaned_text = text.replace("*", "")
#     cleaned_text = text.replace("#", "")
#     VOICE_RECORDINGS_DIR = os.getenv("VOICE_RECORDINGS_DIR")
#     output_path = os.path.join(VOICE_RECORDINGS_DIR, filename)
#     response = elevenlabs_client.text_to_speech.convert(
#         voice_id="rAtrafLSBaG2SsW9s1vJ",  
#         # british voice pFZP5JQG7iQjIQuC4Bku  
#         # newcastle UcpNmxxVKOpY3FlKl5Hz
#         # newcastle 2 rAtrafLSBaG2SsW9s1vJ
#         output_format="mp3_22050_32",
#         text=cleaned_text,
#         model_id="eleven_turbo_v2_5",
#         voice_settings=VoiceSettings(
#             stability=0.0,
#             similarity_boost=1.0,
#             style=0.0,
#             use_speaker_boost=True,
#         ),
#     )

#     # write out the bytes
#     save(response, output_path)
#     return output_path
DEFAULT_TTS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "rAtrafLSBaG2SsW9s1vJ")


def resolve_tts_voice_id(agent: Optional[Agent] = None, fallback: Optional[str] = None) -> str:
    """
    Decide which ElevenLabs voice to use:
      1) agent.tts_voice_id (or agent.voice_id), if present
      2) 'fallback' param (if provided)
      3) ELEVENLABS_VOICE_ID env var
      4) DEFAULT_TTS_VOICE_ID constant
    """
    if agent:
        vid = getattr(agent, "tts_voice_id", None) or getattr(agent, "voice_id", None)
        if isinstance(vid, str) and vid.strip():
            return vid.strip()

    if fallback and isinstance(fallback, str) and fallback.strip():
        return fallback.strip()

    return DEFAULT_TTS_VOICE_ID


def synthesize_speech_elevenlabs(text: str, filename: str, voice_id: Optional[str] = None) -> str:
    """
    Generate TTS with ElevenLabs into VOICE_RECORDINGS_DIR/filename.
    'voice_id' overrides any defaults/resolution.
    """
    from elevenlabs import VoiceSettings, save
    from elevenlabs.client import ElevenLabs

    elevenlabs_client = ElevenLabs()

    # basic cleanup to avoid artifacts
    cleaned_text = text.replace("*", "").replace("#", "")

    outdir = settings.VOICE_RECORDINGS_DIR
    os.makedirs(outdir, exist_ok=True)
    output_path = os.path.join(outdir, filename)

    vid = (voice_id or DEFAULT_TTS_VOICE_ID).strip()

    response = elevenlabs_client.text_to_speech.convert(
        voice_id=vid,
        output_format="mp3_22050_32",
        text=cleaned_text,
        model_id="eleven_turbo_v2_5",
        voice_settings=VoiceSettings(
            stability=0.0,
            similarity_boost=1.0,
            style=0.0,
            use_speaker_boost=True,
        ),
    )

    save(response, output_path)
    return output_path

def _openai_client():
    # Resolve the OpenAI key the same way the chat models do (DB override → env),
    # so transcription works even when the key is only set in SiteConfiguration.
    api_key = get_setting("OPENAI_API_KEY")
    return OpenAI(api_key=api_key) if api_key else OpenAI()


def _whisper(file_path, client=None):
    """Whisper, asked for its segments instead of only the flat text.

    ``verbose_json`` costs nothing extra and returns every segment with a start
    and an end. Asking for plain text and throwing the timings away is what left
    the panel unable to run a timestamp down the side of the transcript or jump
    the player to a line.
    """
    client = client or _openai_client()
    with open(file_path, "rb") as audio_file:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    text = (getattr(result, "text", "") or "").strip()
    segments = []
    for seg in (getattr(result, "segments", None) or []):
        # The SDK hands back objects on some versions and dicts on others.
        get = seg.get if isinstance(seg, dict) else lambda k, d=None: getattr(seg, k, d)
        body = (get("text", "") or "").strip()
        if not body:
            continue
        segments.append({
            "start": round(float(get("start", 0.0) or 0.0), 2),
            "end": round(float(get("end", 0.0) or 0.0), 2),
            "text": body,
            "speaker": None,
        })
    return text, segments


def _split_stereo(file_path):
    """Split a two-channel recording into two mono files, or return None.

    A conference leg recorded with ``recording_channels="dual"`` puts each party
    on its own channel: what the platform sent on one, what the person on the
    other end said on the other. That is speaker separation for free — Whisper
    itself cannot tell voices apart. A mono recording has nothing to split, and
    is transcribed as one track with no speaker attributed.
    """
    import audioop
    import tempfile

    try:
        with wave.open(file_path, "rb") as src:
            if src.getnchannels() != 2:
                return None
            params = src.getparams()
            frames = src.readframes(params.nframes)
    except (wave.Error, EOFError, FileNotFoundError):
        return None

    width = params.sampwidth
    out = []
    for channel in (0, 1):
        mono = audioop.tomono(frames, width, 1 if channel == 0 else 0,
                              0 if channel == 0 else 1)
        fd, path = tempfile.mkstemp(suffix=f".ch{channel}.wav")
        os.close(fd)
        with wave.open(path, "wb") as dst:
            dst.setnchannels(1)
            dst.setsampwidth(width)
            dst.setframerate(params.framerate)
            dst.writeframes(mono)
        out.append(path)
    return out


def transcribe_audio(file_path, with_segments=False):
    """Transcribe a recording.

    Returns the plain text by default, so every existing caller keeps working.
    Pass ``with_segments=True`` for ``(text, segments)``.
    """
    client = _openai_client()
    channels = _split_stereo(file_path)

    if not channels:
        text, segments = _whisper(file_path, client)
    else:
        # Two channels, so each side is transcribed on its own and the two are
        # merged back in time order. Speaker 1 is the party the platform dialled
        # out to; speaker 2 is the other side of the conference.
        merged = []
        try:
            for number, path in enumerate(channels, start=1):
                _, segs = _whisper(path, client)
                for seg in segs:
                    seg["speaker"] = number
                merged.extend(segs)
        finally:
            for path in channels:
                try:
                    os.remove(path)
                except OSError:
                    pass
        merged.sort(key=lambda s: s["start"])
        segments = merged
        text = "\n".join(s["text"] for s in merged)

    return (text, segments) if with_segments else text

## Self registration logic ##

def _selfreg_enabled() -> bool:
    """
    Check env flag SELF_REGISTRATION_ENABLED. Truthy values: 1/true/yes/on.
    """
    return get_bool("SELF_REGISTRATION_ENABLED")

def _get_selfreg_agent() -> Agent | None:
    """
    Resolve the unique Agent by env var SELF_REG_AGENT_NAME.
    """
    name = (get_setting("SELF_REG_AGENT_NAME") or "").strip()
    if not name:
        return None
    return Agent.objects.filter(name=name).first()

def agent_host_allowed(host: str) -> bool:
    """
    AS-06/F8 SSRF guard: only allow agent backends on an explicit allow-list
    (settings.AGENT_ALLOWED_HOSTS or SiteConfiguration). Empty/unlisted hosts are denied, so a
    DB-controlled Agent.host cannot point the server at internal/metadata targets.
    """
    allowed_hosts_str = get_setting("AGENT_ALLOWED_HOSTS", getattr(settings, "AGENT_ALLOWED_HOSTS", ""))
    if isinstance(allowed_hosts_str, list):
        allow = [a.strip().lower() for a in allowed_hosts_str if a.strip()]
    else:
        allow = [a.strip().lower() for a in allowed_hosts_str.split(",") if a.strip()]
    return (host or "").strip().lower() in allow


def _invoke_langgraph_for_agent(agent: Agent, user_message: str, thread_id: str, phone: str | None = None) -> str:
    # AS-06/F8 fix: refuse to contact a non-allow-listed agent host.
    if not agent_host_allowed(getattr(agent, "host", "")):
        return "Sorry, the configured agent host is not permitted."
    """
    Invoke a LangGraph agent directly (no Patient required).
    Sends caller phone as a system message and also in config (if provided).
    """
    try:
        from langgraph_sdk import get_client, get_sync_client
        from langgraph.pregel.remote import RemoteGraph
    except Exception:
        return "Sorry, the LangGraph client is not available on the server."

    url = f"http://{agent.host}:{agent.port}"
    graph_name = agent.langgraph_name

    try:
        client = get_client(url=url)
        sync = get_sync_client(url=url)
        rg = RemoteGraph(graph_name, client=client, sync_client=sync)
    except Exception:
        return "Sorry, could not connect to the configured agent."

    # Build messages: include phone as a system note if available
    msgs = []
    if phone:
        msgs.append({"role": "system", "content": f"caller_phone_e164={phone}"})
    msgs.append({"role": "user", "content": user_message})

    # Also pass phone through config so the graph can access it programmatically
    cfg = {"configurable": {"thread_id": thread_id}}
    if phone:
        cfg["configurable"]["phone_number"] = phone

    try:
        result = rg.invoke({"messages": msgs}, config=cfg)
        msgs_out = result.get("messages") if isinstance(result, dict) else None
        if isinstance(msgs_out, list) and msgs_out:
            last = msgs_out[-1]
            if isinstance(last, dict):
                content = last.get("content")
                if isinstance(content, str) and content.strip():
                    return content
        return "Sorry, something went wrong generating the response."
    except Exception:
        return "Sorry, something went wrong generating the response."

def _handle_self_registration_flow(phone: str, text: str) -> str | None:
    if not _selfreg_enabled():
        return None
    agent = _get_selfreg_agent()
    if not agent:
        return None

    # --- 24-hour deterministic bucket (rotates every 24h) --------------------
    ts = int(timezone.now().timestamp())
    bucket = ts // (24*60*60)  # 24 hours
    conv_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"selfreg:{phone}:{bucket}")
    thread_id = str(conv_uuid)
    # -------------------------------------------------------------------------

    # Native agents run in-process (async graph + Postgres checkpointer); remote
    # agents run on a LangGraph server. The caller's phone (and a little platform
    # context) is passed through the config so the agent can pre-fill it.
    if getattr(agent, "kind", "remote") == "native":
        from .native_agents import run_native
        configurable = {
            "phone_number": phone,
            "platform_language": getattr(settings, "LANGUAGE_CODE", None),
            "brand_name": brand_name(),
        }
        model_name = (getattr(agent, "model", "") or "").strip() or None
        reply = run_native(agent.native_key, thread_id, text, configurable, model_name)
    else:
        reply = _invoke_langgraph_for_agent(agent, text, thread_id, phone=phone)

    conv, _ = Conversation.objects.get_or_create(
        id=conv_uuid,
        defaults={"started_at": timezone.now(), "last_message_at": timezone.now()},
    )
    if hasattr(conv, "agent") and (not conv.agent_id or conv.agent_id != agent.id):
        conv.agent = agent
    conv.last_message_at = timezone.now()
    conv.save()

    Message.objects.create(
        user=phone,
        conversation_id=thread_id,
        user_message=text,
        response_message=reply,
    )
    return reply

def append_system_note_to_langgraph(patient: Patient, note: str, thread_id: str | None = None) -> bool:
    """
    Appends a system message (note) to the remote LangGraph conversation history,
    addressing the correct thread via thread_id. Returns True if it didn't error.
    """
    agent = getattr(patient, "agent", None)
    if not agent:
        return False

    try:
        from langgraph_sdk import get_client, get_sync_client
        from langgraph.pregel.remote import RemoteGraph
    except Exception:
        return False

    url = f"http://{agent.host}:{agent.port}"
    graph_name = agent.langgraph_name

    try:
        client = get_client(url=url)
        sync = get_sync_client(url=url)
        rg = RemoteGraph(graph_name, client=client, sync_client=sync)
    except Exception:
        return False

    # Target the remote thread (this is not "passing the alert" via configurable; just selecting the thread)
    cfg = {"configurable": {"thread_id": thread_id}} if thread_id else None

    try:
        # Append a *system* message, ignore whatever the graph returns
        rg.invoke({"messages": [{"role": "system", "content": str(note)}]}, config=cfg)
        return True
    except Exception:
        return False

# ---------------------------
# WhatsApp audio helpers
# ---------------------------

AUDIO_CT_PREFIXES = ("audio/",)
AUDIO_CT_KEYWORDS = ("ogg", "opus", "mpeg", "mp3", "wav", "amr")


def is_audio_content_type(ct: str | None) -> bool:
    """
    Return True if Content-Type looks like audio.
    """
    if not ct:
        return False
    ct = ct.lower()
    if ct.startswith(AUDIO_CT_PREFIXES):
        return True
    return any(k in ct for k in AUDIO_CT_KEYWORDS)


def ext_for_content_type(ct: str | None) -> str:
    """
    Guess a safe file extension from Content-Type.
    """
    ct = (ct or "").lower()
    if "ogg" in ct or "opus" in ct:
        return ".ogg"
    if "wav" in ct:
        return ".wav"
    if "mpeg" in ct or "mp3" in ct:
        return ".mp3"
    if "amr" in ct:
        return ".amr"
    return ".ogg"


def download_twilio_media(media_url: str) -> bytes:
    """
    Download media from Twilio MediaUrlN using Account SID/Token basic auth.
    """
    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token  = get_setting("TWILIO_AUTH_TOKEN")
    r = requests.get(media_url, auth=(account_sid, auth_token), timeout=30)
    r.raise_for_status()
    return r.content


def save_bytes(path: str, data: bytes) -> None:
    """
    Save bytes to disk, creating parent folders if needed.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


# ---------------------------
# Single-use signed download token
# ---------------------------

def build_signed_download_token(message_id: int, kind: str, *, ttl_seconds: int = 600) -> str:
    """
    Create a short-lived, single-use token for serving audio to Twilio.
    Encodes: message_id, kind, expiry, signature
    """
    # AS-09/F1 fix: sign download tokens with a dedicated, rotated key (not the
    # committed SECRET_KEY). Tokens forged with the public repo key no longer validate.
    secret = getattr(settings, "DOWNLOAD_TOKEN_KEY", None) or getattr(settings, "SECRET_KEY", os.getenv("DJANGO_SECRET_KEY", "change-me"))
    exp = int(time.time()) + int(ttl_seconds)
    base = f"{message_id}:{kind}:{exp}"
    sig  = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    raw  = f"{base}:{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_and_consume_download_token(token: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Verify signature & expiry & ensure single-use via cache.
    Returns (ok, payload)
    """
    if not token:
        return False, {}

    # restore base64 padding
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad).decode()
    except Exception:
        return False, {}

    parts = raw.split(":")
    if len(parts) != 4:
        return False, {}

    mid, kind, exp_s, sig = parts
    base = f"{mid}:{kind}:{exp_s}"

    # AS-09/F1 fix: sign download tokens with a dedicated, rotated key (not the
    # committed SECRET_KEY). Tokens forged with the public repo key no longer validate.
    secret = getattr(settings, "DOWNLOAD_TOKEN_KEY", None) or getattr(settings, "SECRET_KEY", os.getenv("DJANGO_SECRET_KEY", "change-me"))
    expected = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False, {}

    try:
        exp = int(exp_s)
    except Exception:
        return False, {}

    if time.time() > exp:
        return False, {}

    # single-use marker in cache (valid until expiry)
    cache_key = f"twilio-audio-token:{sig}"
    if cache.get(cache_key):
        return False, {}
    cache.set(cache_key, "used", timeout=max(exp - int(time.time()), 1))

    return True, {"message_id": mid, "kind": kind}

def send_whatsapp_template_with_media(
    *, to_e164: str, content_sid: str, content_variables: dict
) -> bool:
    """
    Send a WhatsApp Content Template (ContentSid) that can include media.
    Template must be approved for WhatsApp and expect variables (e.g., {{1}} = media URL).
    """

    if not (to_e164 and content_sid):
        return False

    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token  = get_setting("TWILIO_AUTH_TOKEN")
    platform_phone = get_setting("PLATFORM_PHONE")  # WhatsApp sender

    if not (account_sid and auth_token and platform_phone):
        return False

    try:
        tw = Client(account_sid, auth_token)
        from_whatsapp = f"whatsapp:{platform_phone}" if not str(platform_phone).startswith("whatsapp:") else platform_phone
        to_whatsapp   = f"whatsapp:{to_e164}"     if not str(to_e164).startswith("whatsapp:")     else to_e164

        tw.messages.create(
            from_=from_whatsapp,
            to=to_whatsapp,
            content_sid=content_sid,
            content_variables=json.dumps(content_variables or {}, ensure_ascii=False),
        )
        return True
    except Exception:
        return False


# --- Plain SMS helper (Twilio) ---
def send_sms_text(to_e164: str | None, body: str) -> bool:
    """
    Send a plain SMS via Twilio.
    Uses TWILIO_SMS_FROM (fallback PLATFORM_PHONE) as the sender.
    """
    if not to_e164 or not body:
        return False

    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token  = get_setting("TWILIO_AUTH_TOKEN")
    from_number = get_setting("TWILIO_SMS_FROM") or get_setting("PLATFORM_PHONE")

    if not (account_sid and auth_token and from_number):
        return False

    try:
        tw = Client(account_sid, auth_token)
        tw.messages.create(
            from_=str(from_number),
            to=str(to_e164),
            body=body.strip()
        )
        return True
    except Exception:
        return False