from ._base import *  # noqa: F401,F403
from django.views.decorators.csrf import ensure_csrf_cookie

from ..realtime import realtime_configured
from .agents import realtime_session_json

__all__ = ['create_chat_link', 'external_audio', 'external_chat', 'external_realtime_session', 'process_audio', 'send_chat_message', 'send_external_message', 'whatsapp_webhook']


def _chat_user_label(user):
    """Stable, non-phone identifier stored on Message.user for the external chat."""
    return user.get_username()


def _external_history(patient, limit=200):
    """
    Return the chat history for a patient as an ordered list of bubble rows.

    Messages are linked to the patient via their Conversation (keyed by
    thread_id), so history survives thread rollovers and does not depend on a
    phone number. Each row is a dict: {who, text|audio, ts}.
    """
    if patient is None:
        return []
    conv_ids = [
        str(cid) for cid in
        Conversation.objects.filter(patient=patient).values_list("id", flat=True)
    ]
    if not conv_ids:
        return []
    msgs = list(
        Message.objects
        .filter(conversation_id__in=conv_ids)
        .order_by("-timestamp")[:limit]
    )
    msgs.reverse()  # oldest first

    rows = []
    for m in msgs:
        # user turn
        if m.input_audio_file:
            rows.append({"who": "user", "audio": reverse("serve_audio_file", args=[m.id, "input"]), "ts": m.timestamp})
        elif m.user_message:
            rows.append({"who": "user", "text": m.user_message, "ts": m.timestamp})
        # assistant turn
        if m.response_audio_file:
            rows.append({"who": "bot", "audio": reverse("serve_audio_file", args=[m.id, "output"]), "ts": m.timestamp})
        elif m.response_message:
            rows.append({"who": "bot", "text": m.response_message, "ts": m.timestamp})
    return rows


@require_POST
@csrf_exempt
def whatsapp_webhook(request):
    # AS-13/F5 fix: fail closed if the Twilio auth token is missing/blank — never
    # validate against an empty (publicly-known) signing key.
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not auth_token:
        return HttpResponseForbidden("Webhook authentication is not configured.")
    validator = RequestValidator(auth_token)
    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.build_absolute_uri()
    params = request.POST.dict()
    if not validator.validate(url, params, signature):
        return HttpResponseForbidden("Invalid signature")

    from_num   = request.POST.get("From", "")         # 'whatsapp:+NNN...' or '+NNN...'
    body_text  = (request.POST.get("Body", "") or "").strip()
    num_media  = int(request.POST.get("NumMedia", "0") or "0")

    is_whatsapp = str(from_num).startswith("whatsapp:")
    channel = "whatsapp" if is_whatsapp else "sms"

    # -------------------- ASYNC BRANCH --------------------
    # Offload the (slow) reply to an in-process worker pool and ack Twilio right
    # away, so the webhook never risks the Twilio timeout. See async_replies.md.
    if getattr(settings, "ASYNC_WHATSAPP_REPLY", False):
        from ..async_reply import submit
        from .. import tasks

        # WhatsApp AUDIO (only if flag on, exactly one media, and it's audio)
        if (
            is_whatsapp
            and get_bool("WHATSAPP_AUDIO_ENABLED")
            and num_media == 1
        ):
            media_url = request.POST.get("MediaUrl0")
            content_t = request.POST.get("MediaContentType0", "")
            if media_url and is_audio_content_type(content_t):
                site_root = f"{request.scheme}://{request.get_host()}"

                submit(
                    tasks.job_process_whatsapp_audio,
                    from_number_raw=from_num,
                    media_url=media_url,
                    content_type=content_t,
                    body_text=body_text,
                    site_root=site_root,   # <- pass it here
                )
                return HttpResponse("<Response></Response>", content_type="text/xml")

        # Otherwise: treat as text (WhatsApp or SMS)
        msg_body = body_text
        if num_media > 0 and body_text:
            msg_body = f"<number of attached files: {num_media}> " + body_text

        submit(
            tasks.job_reply_text,
            channel=channel,
            from_number_raw=from_num,
            body_text=msg_body or "",
        )
        return HttpResponse("<Response></Response>", content_type="text/xml")

    # -------------------- SYNC BRANCH (existing behavior) --------------------
    phone_e164 = from_num.replace("whatsapp:", "").strip()
    patient = (
        Patient.objects
        .filter(Q(phone_number=phone_e164) | Q(caregiver__phone_number=phone_e164))
        .select_related("agent")
        .first()
    )

    # WhatsApp AUDIO (sync only; keep your current path)
    if is_whatsapp and get_bool("WHATSAPP_AUDIO_ENABLED") and num_media == 1:
        media_url = request.POST.get("MediaUrl0")
        content_t = request.POST.get("MediaContentType0", "")
        if media_url and is_audio_content_type(content_t) and patient:
            try:
                audio_bytes = download_twilio_media(media_url)
            except Exception:
                audio_bytes = b""

            if audio_bytes:
                ts = datetime.now().strftime("%Y%m%d%H%M%S")
                in_ext  = ext_for_content_type(content_t)
                in_name = f"{ts}_wa_in{in_ext}"
                in_path = os.path.join(VOICE_RECORDINGS_DIR, in_name)
                save_bytes(in_path, audio_bytes)

                try:
                    transcript = transcribe_audio(in_path)
                except Exception:
                    transcript = "No pude transcribir el audio. Por favor intenta de nuevo."

                if body_text.lower() in ("/quit", "/restart"):
                    new_id = str(uuid.uuid4())
                    patient.current_thread_id = new_id
                    patient.save(update_fields=["current_thread_id"])
                    reply_text = "All set! Your conversation has been reset. How can I help you now?"
                    thread_id  = new_id
                else:
                    # Keep the automation lifecycle in sync for voice-note replies
                    # too (inject context / revert on idle timeout).
                    extra_configurable = automation_turn(patient)
                    thread_id = patient.current_thread_id or _get_or_create_thread(patient)
                    reply_text = generate_response_langgraph(
                        patient, transcript.strip(), thread_id,
                        extra_configurable=extra_configurable,
                    )

                out_name = f"{ts}_wa_out.mp3"
                voice_id = resolve_tts_voice_id(getattr(patient, "agent", None))
                synthesize_speech_elevenlabs(reply_text, out_name, voice_id=voice_id)

                msg = Message.objects.create(
                    user=phone_e164,
                    conversation_id=thread_id,
                    user_message=transcript,
                    response_message=reply_text,
                    input_audio_file=in_name,
                    response_audio_file=out_name,
                )

                # Build single-use signed URL for Twilio to download
                token = build_signed_download_token(msg.id, "output", ttl_seconds=600)
                media_url_signed = request.build_absolute_uri(
                    reverse("twilio_audio_download", args=[msg.id, "output"]) + f"?t={token}"
                )

                # R3-04 fix: XML-escape agent output before embedding in TwiML.
                from xml.sax.saxutils import escape as _xml_escape, quoteattr as _xml_quoteattr
                twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>
    <Body>{_xml_escape(reply_text)}</Body>
    <Media>{_xml_escape(media_url_signed)}</Media>
  </Message>
</Response>"""
                return HttpResponse(twiml, content_type="text/xml")

    # Fallback: text path (sync) for both WhatsApp and SMS
    if num_media > 0 and body_text:
        body_text = f"<number of attached files: {num_media}> " + body_text

    response_text = process_received_message(from_num, body_text or "")
    # R3-04 fix: XML-escape agent output before embedding in TwiML.
    from xml.sax.saxutils import escape as _xml_escape
    twilio_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{_xml_escape(response_text)}</Message>
</Response>"""
    return HttpResponse(twilio_response, content_type="text/xml")


@csrf_exempt
@login_required
@require_POST
def send_chat_message(request):
    """Navigator chatbot bubble — backed by the built-in Link Worker native agent.

    The conversation is keyed by the client-supplied ``conversation_id`` (used as
    the LangGraph thread_id), so history persists per conversation. Both sides
    are stored as Message rows for the app's own records.
    """
    import json, uuid
    from django.http import JsonResponse, HttpResponseBadRequest

    if request.method != 'POST' or not request.user.is_authenticated:
        return HttpResponseBadRequest()
    data = json.loads(request.body)
    user_msg = data.get('message', '').strip()
    convo_id = data.get('conversation_id')
    if not user_msg or not convo_id:
        return HttpResponseBadRequest()

    # Normalize conversation_id to a stable UUID thread id (the frontend may send
    # an email/username). This same id keys both the LangGraph checkpointer and
    # the persisted Message/Conversation rows.
    try:
        thread_id = str(uuid.UUID(str(convo_id)))
    except (ValueError, TypeError, AttributeError):
        thread_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, 'chat:' + str(convo_id)))

    # /quit ends the conversation. The bubble starts a fresh conversation_id
    # client-side, so we just acknowledge without invoking the agent.
    if user_msg.lower() in ('/quit', '/restart'):
        return JsonResponse({'user_message': user_msg,
                             'bot_message': str(_("Conversation ended. Send a message to start a new one.")),
                             'reset': True})

    # The bubble always uses the built-in Link Worker agent.
    agent = Agent.objects.filter(kind=Agent.Kind.NATIVE, native_key='link_worker').first()
    if agent is None:
        return JsonResponse({'user_message': user_msg,
                             'bot_message': 'Link Worker agent is not configured.'})

    # Link Worker acts on the platform as the current user. Only navigators (and
    # admins) get a working token; testers/others fall through and the agent's
    # tools refuse. The token lets the tools authenticate as this user so the
    # usual per-object permissions apply (navigator → own clients, admin → all).
    extra_configurable = {}
    if is_navigator(request.user):
        token, _created = Token.objects.get_or_create(user=request.user)
        extra_configurable['user_token'] = token.key
        extra_configurable['is_admin'] = is_admin(request.user)

    bot_msg = generate_response_with_agent(
        agent, request.user, user_msg, thread_id,
        extra_configurable=extra_configurable,
    )
    # Persist both sides (thread_id == conversation_id).
    save_message(request.user.get_username(), user_msg, bot_msg, thread_id)
    return JsonResponse({'user_message': user_msg, 'bot_message': bot_msg})


def create_chat_link(request, patient_pk):
    patient = get_object_or_404(
        Patient.objects.select_related("caregiver"), pk=patient_pk
    )

    if not (patient.caregiver and patient.caregiver.phone_number):
        messages.error(request, _("The client has no caregiver phone number."))
        return redirect("patients")

    # Build link without leaking the number
    url = request.build_absolute_uri(reverse("external_chat"))

    messages.success(request, _("Share this link with the caregiver: %(url)s") % {"url": url})
    return redirect("patients")


@ensure_csrf_cookie
def external_chat(request):
    """
    Renders the chat page for the logged-in test user.
    The conversation is tied to the user's linked patient, not to a phone number.

    @ensure_csrf_cookie guarantees the `csrftoken` cookie is set on page load so
    the JS (which reads it via getCookie and sends X-CSRFToken) can post messages.
    """
    patient = getattr(request.user, "test_patient", None)
    # Assistant name shown in the chat header.
    agent = getattr(patient, "agent", None)
    chat_title = getattr(agent, "name", None) or _("Assistant")
    # Prompt agents flagged as real-time voice get the live voice-call UI. The
    # Realtime API is only contacted when the tester presses Start.
    if agent is not None and agent.kind == Agent.Kind.PROMPT and agent.realtime_enabled:
        return render(request, "chat/realtime_voice.html", {
            "chat_title": chat_title,
            "session_url": reverse("external_realtime_session"),
            "realtime_ready": realtime_configured(),
        })
    # Start each visit with a clean slate: the tester chat does not show the
    # conversation history that existed before the page was loaded.
    return render(request, "chat/external_chat_with_audio.html", {
        "chat_title": chat_title,
        "messages": [],
    })


def send_external_message(request):
    """
    AJAX endpoint to post a new user message, get a bot reply, save both and return JSON.
    The patient is resolved from the logged-in test user (no phone number needed).
    """
    patient = getattr(request.user, "test_patient", None)
    if patient is None:
        return HttpResponseForbidden(_("Not authorised."))
    if request.method != "POST":
        return HttpResponseBadRequest("Must POST")

    try:
        payload = json.loads(request.body)
        user_msg = payload.get("message", "").strip()
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    if not user_msg:
        return HttpResponseBadRequest("Empty message")

    bot_msg = process_message_for_patient(
        patient, user_msg, user_label=_chat_user_label(request.user)
    )

    return JsonResponse({
        "user_message": user_msg,
        "bot_message": bot_msg,
    })


@require_POST
def external_realtime_session(request):
    """Mint an ephemeral Realtime session key for the tester's voice call.

    The agent comes from the tester's linked patient (never from the request),
    so a tester can only ever talk to their assigned agent. Access is gated by
    ``patient_tester_required`` on the URL.
    """
    patient = getattr(request.user, "test_patient", None)
    agent = getattr(patient, "agent", None)
    if (agent is None or agent.kind != Agent.Kind.PROMPT
            or not agent.realtime_enabled):
        return HttpResponseForbidden(_("Not authorised."))
    return realtime_session_json(agent)


@login_required
def external_audio(request):
    """
    Shows the voice-chat page.
    """
    patient = request.user.test_patient
    # ensure a thread exists
    if not patient.current_thread_id:
        patient.current_thread_id = str(uuid.uuid4())
        patient.save(update_fields=["current_thread_id"])

    return render(request, "chat/external_audio.html", {
        "conversation_id": patient.current_thread_id,
    })


@login_required
def process_audio(request):
    """
    Receives a WebM/WAV blob, transcribes it, gets a LangGraph reply,
    TTS via ElevenLabs, persists input+output as Message with audio refs,
    and returns JSON with URLs.
    """
    # WB-03 fix: restrict to a linked PatientTester; handle the missing link
    # gracefully (was an unhandled 500 -> traceback under DEBUG for any other user).
    patient = getattr(request.user, "test_patient", None)
    if patient is None:
        return HttpResponseForbidden(_("Not authorised."))
    if request.method != "POST":
        return HttpResponseBadRequest("Must POST")

    if "audio_data" not in request.FILES:
        return HttpResponseBadRequest("No audio file provided")

    file = request.FILES["audio_data"]
    ext  = os.path.splitext(file.name)[1].lower()
    if ext not in (".webm",".wav",".ogg"):
        return HttpResponseBadRequest("Invalid file type")

    # save incoming
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    in_name = f"{ts}_in{ext}"
    os.makedirs(VOICE_RECORDINGS_DIR, exist_ok=True)
    in_path = os.path.join(VOICE_RECORDINGS_DIR, in_name)
    with open(in_path, "wb") as fp:
        for chunk in file.chunks():
            fp.write(chunk)

    # 1) transcription
    transcript = transcribe_audio(in_path)
    #try:
    #    transcript = transcribe_audio(in_path)
    #except Exception:
    #    transcript = "Transcription failed."

    # 2) reset thread?
    text = transcript.strip()
    if text.lower() in ("/quit","/restart"):
        new_id = str(uuid.uuid4())
        patient.current_thread_id = new_id
        patient.save(update_fields=["current_thread_id"])
        return JsonResponse({
            "transcript": transcript,
            "response":   "Conversation reset.",
            "input_audio":  "",
            "response_audio": ""
        })

    # 3) LangGraph
    thread_id = patient.current_thread_id
    resp_text = generate_response_langgraph(patient, transcript, thread_id)

    # 4) ElevenLabs TTS
    out_name = f"{ts}_out.mp3"
    out_path = os.path.join(VOICE_RECORDINGS_DIR, out_name)
    # you already have synthesize_speech_elevenlabs utility
    voice_id = resolve_tts_voice_id(getattr(patient, "agent", None))
    synthesize_speech_elevenlabs(resp_text, out_name, voice_id=voice_id)

    # 5) persist both sides like the text path does: save_message also upserts
    # the Conversation and links it to the patient/agent, so voice turns show up
    # in the patient's message history (not just as orphan Message rows).
    msg = save_message(
        _chat_user_label(request.user), transcript, resp_text, thread_id,
        patient=patient,
        input_audio_file=in_name,
        response_audio_file=out_name,
    )

    return JsonResponse({
        "transcript":      transcript,
        "response":        resp_text,
        "input_audio":     reverse("serve_audio_file", args=[msg.id, "input"]),
        "response_audio":  reverse("serve_audio_file", args=[msg.id, "output"]),
    })


