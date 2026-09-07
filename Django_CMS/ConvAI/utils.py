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
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Local apps
from .models import CallLeg, CallRecording, Caregiver, Conversation, Message, Patient, Agent

logger = logging.getLogger(__name__)

load_dotenv()  # Load environment variables from .env

def make_phone_conference(phone_numbers, record_ctn=True, record_dyad=True):
    '''
    phone_numbers is a dictionary that has:
      - CTN : Phone number for the Navigator
      - Platform : Phone number for the platform (must be registered in Twilio beforehand)
      - Dyad : Phone number for the Dyad (carer + PlWD)

    record is a boolean value that requests recording of the call in twilio

    Returns what was placed: the conference name, and for each leg its side of
    the conference and the Call SID Twilio answered with. Those SIDs are the
    only thing that ties the recordings arriving later back to this call. This
    function used to return nothing at all — the SIDs were created and dropped
    on the floor, views/calls.py reported {"conference": null} to the browser on
    every call, and whose recording it was had to be guessed from phone numbers
    afterwards. See CallLeg, which is what the caller writes them into.
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
    return {
        "conference": conference_name,
        "legs": [
            {
                "leg": int(CallRecording.Leg.CTN),
                "call_sid": call1.sid,
                "to_number": str(phone_numbers.get("CTN") or ""),
                "recorded": bool(record_ctn),
            },
            {
                "leg": int(CallRecording.Leg.DYAD),
                "call_sid": call2.sid,
                "to_number": str(phone_numbers.get("Dyad") or ""),
                "recorded": bool(record_dyad),
            },
        ],
    }

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

# WhatsApp delivery failures that a navigator can actually do something about.
# 63016 is the one that matters here: outside the 24-hour customer-service
# window WhatsApp only accepts an approved template, and this platform has none
# (the account is currently restricted from creating them). Twilio *accepts*
# such a message and fails it a moment later, so the reason only exists on the
# message resource — see send_whatsapp_text_result.
WHATSAPP_ERRORS = {
    63016: _("WhatsApp only allows a new conversation to be started with an "
             "approved template, and they have not replied in the last 24 hours."),
    63024: _("WhatsApp rejected the number."),
    63003: _("That number is not reachable on WhatsApp."),
    63015: _("That number is not reachable on WhatsApp."),
    21610: _("They have unsubscribed from messages from this number."),
}

# Statuses Twilio will not move off again.
_WA_FAILED = ("failed", "undelivered")
_WA_DONE = ("delivered", "read")


def send_whatsapp_text_result(to_e164: str | None, body: str,
                              *, wait_s: float = 8.0) -> dict:
    """Send a WhatsApp text and report what actually happened to it.

    ``send_whatsapp_text`` below returns True as soon as Twilio *accepts* the
    message, which is not the same as it arriving. The failure that matters most
    here — 63016, freeform text outside the 24-hour window — is reported
    asynchronously, seconds after a successful create(). Callers that acted on
    the bool therefore told the navigator the caregiver had been asked something
    they were never asked.

    So this creates the message and then watches it until Twilio settles on a
    status or ``wait_s`` runs out. Returns::

        {'ok': bool, 'sid': str|None, 'status': str,
         'error_code': int|None, 'reason': str}

    ``ok`` is True while nothing has gone wrong — including the still-in-flight
    case, where the message has been handed over and no failure has come back.
    A late failure after that is for the status callback to catch, not this.
    """
    blank = {'ok': False, 'sid': None, 'status': 'not-sent',
             'error_code': None, 'reason': ''}

    if not to_e164 or not body:
        return dict(blank, reason=str(_("There was nothing to send.")))

    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token  = get_setting("TWILIO_AUTH_TOKEN")
    platform_phone = get_setting("PLATFORM_PHONE")

    if not (account_sid and auth_token and platform_phone):
        return dict(blank, status='unconfigured',
                    reason=str(_("WhatsApp is not configured on this platform.")))

    try:
        client = Client(account_sid, auth_token)
        from_whatsapp = f"whatsapp:{platform_phone}" if not str(platform_phone).startswith("whatsapp:") else platform_phone
        to_whatsapp   = f"whatsapp:{to_e164}"     if not str(to_e164).startswith("whatsapp:")     else to_e164
        msg = client.messages.create(
            from_=from_whatsapp,
            to=to_whatsapp,
            body=body.strip(),
        )
    except Exception as exc:
        # A refusal at create() time — bad credentials, malformed number. The
        # exception is the only description of it that exists.
        logger.warning("WhatsApp create failed for %s: %s", to_e164, exc)
        return dict(blank, status='rejected', reason=str(exc))

    sid = msg.sid
    status = msg.status or 'queued'
    code = msg.error_code

    # Poll rather than trust the create(). Twilio rejects a 63016 within a
    # second or two, which is well inside the time this request already spends
    # generating the message it just sent.
    deadline = time.monotonic() + max(0.0, wait_s)
    while status not in _WA_FAILED + _WA_DONE and time.monotonic() < deadline:
        time.sleep(0.75)
        try:
            fetched = client.messages(sid).fetch()
        except Exception:
            break  # Sent; we simply cannot watch it. Not a failure.
        status = fetched.status or status
        code = fetched.error_code or code

    if status in _WA_FAILED:
        reason = WHATSAPP_ERRORS.get(code)
        if reason is None:
            reason = (
                str(_("WhatsApp could not deliver it (error %s).")) % code
                if code else str(_("WhatsApp could not deliver it."))
            )
        logger.warning("WhatsApp %s to %s: %s (%s)", status, to_e164, code, sid)
        return {'ok': False, 'sid': sid, 'status': status,
                'error_code': code, 'reason': str(reason)}

    return {'ok': True, 'sid': sid, 'status': status,
            'error_code': code, 'reason': ''}


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
        # prompt-based (plain, or the RAG subtype — run_prompt_agent decides
        # from the agent's rag_enabled flag)
        from .native_agents import run_prompt_agent
        return run_prompt_agent(agent, thread_id, user_message, configurable, model_name)

    # Sensei agents: one JSON POST to the external Sensei service. No host/port
    # of their own — the endpoint and key are installation-wide settings — so
    # this returns before the LangGraph host allow-list below, which has nothing
    # to check for them.
    if kind == "sensei":
        from . import sensei
        if not sensei.enabled():
            return "Sorry, Sensei agents are not enabled on this installation."
        return sensei.send(user_or_patient, user_message, thread_id)

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

    # Scrub a Sensei passcode before it reaches the database. This is the one
    # place every inbound turn is persisted from — WhatsApp, SMS, the external
    # chat and the tester chat all land here — which is what makes it the right
    # place: the agent has already been given the raw text by now, and nothing
    # downstream of this row (the classifier, the transcript a navigator reads,
    # the summariser) has any business seeing the passcode.
    from .sensei import redact as _redact_credentials
    user_message = _redact_credentials(user_message)

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


def _review_after_message(message, inbound_text: str) -> None:
    """Queue a classifier pass over the conversation this message landed in.

    Keyed off the saved Message rather than the thread id it was saved under.
    ``save_message`` normalises a thread id it cannot read as a UUID into a
    fresh one, so the two can differ — and a review aimed at the id that got
    replaced would find no conversation and quietly do nothing, which is the
    exact failure this whole path exists to stop happening.

    Skipped when the caregiver said nothing — outbound-only turns (reminders,
    templates) add no new evidence, and classifying them again would be one
    model call per reminder for a verdict that cannot have changed.
    """
    if message is None or not (inbound_text or "").strip():
        return
    try:
        from .conversation_alerts import review_conversation_async
        review_conversation_async(message.conversation_id)
    except Exception:
        # Detection is not allowed to break delivery. The message is already
        # saved and the reply already sent; the batch pass in Settings remains
        # the backstop for anything this drops.
        logger.exception("Could not queue conversation review for %s",
                         message.conversation_id)


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
        msg = save_message(label, text, "", _get_or_create_thread(patient), patient=patient)
        # Reviewed even though nothing answered — arguably especially then. The
        # switch suspends the agent replying, not the caregiver being heard,
        # and a crisis disclosed while the agent is off is the one nobody is
        # already reading.
        _review_after_message(msg, text)
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
    msg = save_message(label, text, reply, thread_id, patient=patient)

    # 6) hand the exchange to the classifier. Off the reply path deliberately:
    #    this is a second model call, and the caregiver should not wait behind
    #    it to be answered.
    _review_after_message(msg, text)
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

    # What is already on file, asked by Twilio's own id for it.
    #
    # This used to be a high-water mark on end_time: anything Twilio had not
    # touched since the newest recording stored was skipped. That answers a
    # different question from the one being asked. A recording that arrives late
    # — a long call whose audio Twilio finishes assembling after a shorter, later
    # one — is behind the mark on the very first sync that sees it, and the mark
    # only ever moves forward, so it is skipped not once but permanently. That is
    # why some recordings never appeared at all. Asking which SIDs are already
    # stored answers "is this new?" without a clock, and a late arrival is simply
    # picked up on the next run.
    known = set(CallRecording.objects.values_list("recording_sid", flat=True))

    # What each call was, written down when it was placed. This is what lets a
    # recording say whose it is instead of being matched by phone number; see
    # CallLeg and make_phone_conference.
    seen_call_sids = {getattr(rec, "call_sid", None) or "" for rec in recordings}
    seen_call_sids.discard("")
    legs = {
        leg.call_sid: leg
        for leg in CallLeg.objects.filter(call_sid__in=seen_call_sids)
                                  .select_related("meeting", "patient")
    } if seen_call_sids else {}

    for rec in recordings:
        rec_sid = rec.sid
        call_sid = getattr(rec, "call_sid", None) or ""  # associated Call SID
        leg = legs.get(call_sid)

        if rec_sid in known:
            # Already stored, so there is nothing to fetch again. A recording
            # that landed before its leg was written down — or before there was
            # anywhere to write it — can still be told what it belongs to now,
            # which is what carries rows through the deploy that adds this.
            # Guarded on patient being unset so this only ever fills a blank.
            if leg is not None:
                CallRecording.objects.filter(
                    recording_sid=rec_sid, patient__isnull=True,
                ).update(
                    call_sid=call_sid,
                    meeting=leg.meeting,
                    patient=leg.patient,
                    leg=leg.leg,
                )
            continue

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
            filename = file_path,
            # The numbers above are still stored — they are what the fallback
            # reads for anything placed outside this platform — but they are no
            # longer how ownership is decided when the leg is known.
            call_sid = call_sid,
            meeting = leg.meeting if leg else None,
            patient = leg.patient if leg else None,
            leg = leg.leg if leg else None,
        )
        known.add(rec_sid)


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


def reminder_recipient_missing(meeting):
    """Why this meeting cannot be reminded on the configured channel, or "".

    One place decides it so the button, the view and the send itself never
    disagree about whether a reminder is possible.
    """
    from .mailer import meeting_reminder_recipient, reminder_channel

    if reminder_channel() == "email":
        if not meeting_reminder_recipient(meeting)[0]:
            return "no-email"
        return ""
    caregiver = getattr(meeting.patient, "caregiver", None)
    if not (caregiver and caregiver.phone_number):
        return "no-phone"
    return ""


def can_send_reminder(meeting):
    """True when a reminder has somewhere to go on the configured channel."""
    return not reminder_recipient_missing(meeting)


def send_meeting_reminder(meeting):
    """Remind the caregiver about a meeting over whichever channel is configured.

    Returns the channel used, so the caller can say which one carried it.
    Raises ValueError when there is nobody to send to — the same failure the
    WhatsApp path has always raised — and lets provider errors through.
    """
    from .mailer import reminder_channel, send_meeting_reminder_email

    if reminder_channel() == "email":
        send_meeting_reminder_email(meeting)
        return "email"
    send_whatsapp_reminder(meeting)
    return "whatsapp"


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


def _whisper(file_path, client=None, prompt=None):
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
            # Words as well as segments. A segment is a window, and on a single
            # channel it happily spans half the call, because the other party
            # falling silent is not a boundary Whisper can see from inside one
            # track. Words carry their own timings, which is the only thing
            # that can put two channels back into the order they were spoken.
            timestamp_granularities=["segment", "word"],
            **({"prompt": prompt} if prompt else {}),
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

    words = []
    for w in (getattr(result, "words", None) or []):
        get = w.get if isinstance(w, dict) else lambda k, d=None: getattr(w, k, d)
        token = (get("word", "") or "").strip()
        if not token:
            continue
        words.append({
            "start": round(float(get("start", 0.0) or 0.0), 2),
            "end": round(float(get("end", 0.0) or 0.0), 2),
            "word": token,
        })

    return text, segments, words


def _join_word(text, word):
    """Append a Whisper word, which arrives bare and punctuation-first."""
    if not text:
        return word
    if word[0] in ",.!?;:%)]}" or word.startswith("'"):
        return text + word
    if text[-1] in "([{$\u00bf\u00a1":
        return text + word
    return text + " " + word


def _turns_from_words(per_channel):
    """Who was speaking when, rebuilt by interleaving both channels word by word.

    Segments cannot do this. Each channel is transcribed on its own, so a
    segment covers a window of *that track* — on the leg where one party was
    mostly listening, Whisper returned a single segment spanning 0 to 28
    seconds. Sorting spans like that by start time gives each side's monologue
    end to end, which is why the transcript did not follow the call even once
    the two speakers were correctly separated.

    Word timings are per-word and do not overlap, so ordering them across both
    channels reproduces the conversation, and a turn simply ends wherever the
    next word belongs to the other speaker.
    """
    words = []
    for speaker, channel in per_channel:
        for w in channel or []:
            words.append((w["start"], w["end"], speaker, w["word"]))
    if not words:
        return []
    words.sort(key=lambda w: (w[0], w[1]))

    turns = []
    for start, end, speaker, word in words:
        if turns and turns[-1]["speaker"] == speaker:
            turns[-1]["text"] = _join_word(turns[-1]["text"], word)
            turns[-1]["end"] = max(turns[-1]["end"], end)
        else:
            turns.append({"start": start, "end": end,
                          "speaker": speaker, "text": word})
    return turns


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
    except wave.Error as exc:
        # Not a RIFF/WAV file at all — an mp3, which is what every stored
        # recording actually is. "I cannot read this format" is a different
        # fact from "this recording is mono", and returning None for both is
        # exactly what hid speaker separation being broken on every Twilio
        # recording the platform has ever transcribed. Say which one it was.
        logger.info("%s is not readable as WAV (%s) — cannot split channels.",
                    file_path, exc)
        return None
    except (EOFError, FileNotFoundError):
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


def _fetch_stereo_wav(recording_sid):
    """Twilio's WAV rendering of a recording, in a temp file, or None.

    The stored file is an mp3: a quarter of the size, and what the player
    streams. But a conference leg is recorded with two channels — one party on
    each — and Python's ``wave`` module cannot open an mp3 at all, so the
    splitter was always handed a file it could never read. Twilio serves the
    same recording as WAV with the channels intact, so transcription fetches
    that, uses it, and throws it away. Nothing on disk changes.
    """
    import tempfile

    account_sid = get_setting("TWILIO_ACCOUNT_SID")
    auth_token = get_setting("TWILIO_AUTH_TOKEN")
    if not (account_sid and auth_token and recording_sid):
        return None

    url = (f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
           f"/Recordings/{recording_sid}.wav")
    try:
        resp = requests.get(url, auth=(account_sid, auth_token), timeout=60)
    except Exception as exc:
        logger.warning("Could not fetch WAV for %s: %s", recording_sid, exc)
        return None
    if resp.status_code != 200:
        logger.warning("Could not fetch WAV for %s: HTTP %s",
                       recording_sid, resp.status_code)
        return None

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with open(path, "wb") as out:
        out.write(resp.content)
    return path


def transcribe_audio(file_path, with_segments=False, recording_sid=None, prompt=None):
    """Transcribe a recording.

    Returns the plain text by default, so every existing caller keeps working.
    Pass ``with_segments=True`` for ``(text, segments)``.

    ``recording_sid`` lets a stored mp3 be transcribed from its WAV twin, which
    is the only way the two parties can be told apart — see _fetch_stereo_wav.
    Without it the mp3 is transcribed as one mixed track, which is what every
    recording got until now: both voices in one line, and Whisper cutting
    segments mid-sentence because it is segmenting an overlap as one stream.
    """
    client = _openai_client()
    channels = _split_stereo(file_path)

    borrowed = None
    if not channels and recording_sid:
        borrowed = _fetch_stereo_wav(recording_sid)
        if borrowed:
            channels = _split_stereo(borrowed)
            if not channels:
                logger.info("WAV for %s is mono; nothing to separate.", recording_sid)

    try:
        return _transcribe_channels(file_path, channels, client, with_segments,
                                    prompt=prompt)
    finally:
        if borrowed:
            try:
                os.remove(borrowed)
            except OSError:
                pass


def _transcribe_channels(file_path, channels, client, with_segments, prompt=None):
    if not channels:
        text, segments, _words = _whisper(file_path, client, prompt=prompt)
    else:
        # Two channels, so each side is transcribed on its own. Speaker 1 is
        # the party the platform dialled out to; speaker 2 is the other side of
        # the conference.
        merged, per_channel = [], []
        try:
            for number, path in enumerate(channels, start=1):
                _text, segs, words = _whisper(path, client, prompt=prompt)
                for seg in segs:
                    seg["speaker"] = number
                merged.extend(segs)
                per_channel.append((number, words))
        finally:
            for path in channels:
                try:
                    os.remove(path)
                except OSError:
                    pass

        # Word timings put the two sides back in the order they were spoken.
        # Segments cannot — see _turns_from_words. Sorted segments stay as the
        # fallback for a response that carries no words.
        segments = _turns_from_words(per_channel)
        if not segments:
            merged.sort(key=lambda s: s["start"])
            segments = merged
        text = "\n".join(s["text"] for s in segments)

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

    conv, _created = Conversation.objects.get_or_create(
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