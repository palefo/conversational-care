from ._base import *  # noqa: F401,F403
import logging

logger = logging.getLogger(__name__)

__all__ = ['send_care_plan_whatsapp', 'serve_audio_file', 'serve_protected_file', 'twilio_audio_download', 'twilio_careplan_download']


@csrf_exempt
def twilio_audio_download(request, message_id: int, kind: str):
    """
    Twilio fetches the audio reply here via a signed, single-use, short-lived URL.
    """
    token = request.GET.get("t") or ""
    ok, payload = verify_and_consume_download_token(token)
    if not ok:
        return HttpResponseForbidden("Invalid or expired token")

    if str(payload.get("message_id")) != str(message_id) or payload.get("kind") != kind:
        return HttpResponseForbidden("Token mismatch")

    msg = get_object_or_404(Message, pk=message_id)
    filename = msg.response_audio_file if kind == "output" else msg.input_audio_file
    if not filename:
        return HttpResponseBadRequest("No file")

    file_path = os.path.join(VOICE_RECORDINGS_DIR, str(filename))
    if not os.path.exists(file_path):
        return HttpResponseBadRequest("File not found")

    ct = "audio/mpeg" if str(filename).lower().endswith(".mp3") else "audio/ogg"
    return FileResponse(open(file_path, "rb"), content_type=ct)


@login_required
def serve_protected_file(request, id):
    # AS-08/F4 / WB-05 fix: enforce ownership — non-staff may only fetch a recording
    # whose from/to number belongs to a patient (or their caregiver) they navigate.
    rec = CallRecording.objects.filter(recording_sid=id).first()
    if not is_admin(request.user):
        nums = {str(getattr(rec, "from_number", "") or ""), str(getattr(rec, "to_number", "") or "")} if rec else set()
        owns = bool(rec) and Patient.objects.filter(
            Q(navigator=request.user) &
            (Q(phone_number__in=nums) | Q(caregiver__phone_number__in=nums))
        ).exists()
        if not owns:
            return HttpResponseForbidden(_("You are not authorised to access this file."))
    audio_path = get_path_audio(id)
    if audio_path is None:
         return HttpResponseBadRequest("Not found")
    full_path = os.path.join(audio_path)
    response = FileResponse(open(full_path, 'rb'))
    return response


@login_required
def serve_audio_file(request, message_id, which):
    """
    Streams back either the input or response audio for a Message,
    using the FileField’s own path.
    """
    msg = get_object_or_404(Message, pk=message_id)
    # AS-08/F4 fix: enforce per-object ownership. Staff may fetch any; a
    # PatientTester only their own linked patient's audio; a navigator only
    # audio belonging to a patient they navigate (matched by sender phone or
    # by patient-linked Conversation — tester chat stores a username).
    if not is_admin(request.user):
        tp = getattr(request.user, "test_patient", None)
        # The in-app test chat labels Message.user with the tester's username, so
        # allow that; also allow the patient/caregiver phone (WhatsApp/SMS audio).
        allowed = {request.user.get_username()}
        if tp:
            if tp.phone_number:
                allowed.add(str(tp.phone_number))
            if tp.caregiver and tp.caregiver.phone_number:
                allowed.add(str(tp.caregiver.phone_number))
        ok = (msg.user or "").strip() in allowed
        if not ok:
            sender = (msg.user or "").strip()
            ok = Patient.objects.filter(
                Q(navigator=request.user) &
                (Q(phone_number=sender) | Q(caregiver__phone_number=sender))
            ).exists()
        if not ok and msg.conversation_id:
            try:
                conv_uuid = uuid.UUID(str(msg.conversation_id))
            except (ValueError, TypeError):
                conv_uuid = None
            if conv_uuid:
                ok = Conversation.objects.filter(
                    id=conv_uuid, patient__navigator=request.user
                ).exists()
        if not ok:
            return HttpResponseForbidden(_("You are not authorised to access this audio."))
    if which == "input":
        file_field = msg.input_audio_file
    elif which == "output":
        file_field = msg.response_audio_file
    else:
        raise Http404("Invalid audio type")

    if not file_field:
        return HttpResponseBadRequest(f"No {which} audio available for this message.")

    # Resolve within the private voice directory (single source of truth).
    full_path = os.path.join(VOICE_RECORDINGS_DIR, os.path.basename(str(file_field)))

    if not os.path.exists(full_path):
        raise Http404("Audio file not found on disk.")

    return FileResponse(open(full_path, "rb"), content_type="audio/mpeg")


@login_required
@require_POST
def send_care_plan_whatsapp(request, pk: int):
    """
    Sends current care plan to caregiver via WhatsApp template with a
    single-use Twilio-only download URL. Logs the text in Message.
    """
    if not get_bool("SEND_CARE_PLAN"):
        return HttpResponseForbidden(_("Sending the care plan is disabled."))

    patient = get_object_or_404(Patient.objects.select_related("caregiver", "navigator"), pk=pk)

    # Only staff or assigned CTN
    if not (is_admin(request.user) or patient.navigator_id == request.user.id):
        return HttpResponseForbidden(_("You do not have permission to send this."))

    if not patient.caregiver or not patient.caregiver.phone_number:
        messages.error(request, _("The client has no caregiver with a valid phone number."))
        return redirect("patient_detail", pk=pk)

    if not patient.care_plan:
        messages.error(request, _("This client has no care plan uploaded."))
        return redirect("patient_detail", pk=pk)

    template_sid = get_setting("WHATSAPP_TEMPLATE_CARE_PLAN_SID", "").strip()
    if not template_sid:
        messages.error(request, _("WHATSAPP_TEMPLATE_CARE_PLAN_SID is missing in .env."))
        return redirect("patient_detail", pk=pk)

    # Reuse existing single-use token util by encoding a subject in (message_id, kind)
    token = build_signed_download_token(f"careplan_{patient.pk}", "pdf", ttl_seconds=900)
    media_url = f"careplan_{str(patient.pk)}.pdf/?t={token}"

    ok = send_whatsapp_template_with_media(
        to_e164=str(patient.caregiver.phone_number),
        content_sid=template_sid,
        content_variables={"1": media_url},  # {{1}} in template = file URL
    )
    if not ok:
        messages.error(request, _("Could not send the WhatsApp message. Check credentials/template."))
        return redirect("patient_detail", pk=pk)

    # Log context message (no media body stored; link is Twilio-only)
    Message.objects.create(
        conversation_id=f"careplan-{patient.pk}",
        user=str(patient.caregiver.phone_number),
        user_message="",
        response_message="Se envió el Plan de Cuidado (PDF) vía WhatsApp template."
    )

    messages.success(request, _("Care plan sent via WhatsApp."))
    return redirect("patient_detail", pk=pk)


@csrf_exempt
@require_GET
def twilio_careplan_download(request, pk: int):
    # 1) Verify & consume one-time token
    token = request.GET.get("t", "")
    ok, payload = verify_and_consume_download_token(token)
    if not ok:
        logger.warning("Invalid or expired token for care-plan download (patient %s)", pk)
        return HttpResponseForbidden("Invalid or expired token")

    # 2) Enforce subject bound to token
    if payload.get("message_id") != f"careplan_{pk}" or payload.get("kind") != "pdf":
        logger.warning("Token subject mismatch for care-plan download (patient %s)", pk)
        return HttpResponseForbidden("Token subject mismatch")

    # 3) Serve the file
    patient = get_object_or_404(Patient, pk=pk)
    file_path = getattr(patient.care_plan, "path", None)
    if not file_path or not os.path.exists(file_path):
        logger.warning("Care plan file not found for patient %s", pk)
        raise Http404("Care plan file not found")

    resp = FileResponse(open(file_path, "rb"), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="careplan_{pk}.pdf"'
    resp["Cache-Control"] = "no-store"
    return resp


