from ._base import *  # noqa: F401,F403

__all__ = ['conversation_feedback', 'message_feedback']


@login_required
@require_POST
def message_feedback(request, message_id):
    """
    Toggle feedback on a single Message.
    Expects JSON body: { "action": "like"|"dislike"|"warning"|"dangerous", "value": true|false }
    """
    msg = get_object_or_404(Message, pk=message_id)

    # find the patient whose phone or caregiver phone matches msg.user
    patient = Patient.objects.filter(
        Q(phone_number=str(msg.user)) |
        Q(caregiver__phone_number=str(msg.user))
    ).select_related('navigator').first()

    # permission check
    if not (is_admin(request.user) or (patient and patient.navigator == request.user)):
        raise Http404()

    try:
        payload = json.loads(request.body)
        action = payload["action"]
        value = bool(payload.get("value", True))
    except (ValueError, KeyError):
        return JsonResponse({"error": "Invalid payload"}, status=400)

    if action == "like":
        msg.liked = value
        if value:
            msg.disliked = False
    elif action == "dislike":
        msg.disliked = value
        if value:
            msg.liked = False
    elif action == "warning":
        msg.warning = value
    elif action == "dangerous":
        msg.dangerous = value
    else:
        return JsonResponse({"error": "Unknown action"}, status=400)

    msg.save(update_fields=["liked", "disliked", "warning", "dangerous"])
    return JsonResponse({
        "liked":    msg.liked,
        "disliked": msg.disliked,
        "warning":  msg.warning,
        "dangerous": msg.dangerous,
    })


@csrf_exempt
@login_required
def conversation_feedback(request, conversation_id):
    """
    Accepts JSON for:
      - rating: int 1..5
      - feedback: str
      - label/value: toggle a single human flag (binary)
      - flags: {label: bool, ...} toggle multiple human flags

    Permissions: staff or CTN assigned to Conversation.patient.
    Persists ALL detector labels in human_flags (true/false), not just the ones sent.
    """
    if request.method != "POST":
        return HttpResponseBadRequest("Must POST")

    # Parse UUID
    try:
        conv_pk = UUID(str(conversation_id))
    except (ValueError, TypeError):
        return HttpResponseBadRequest("Invalid conversation id")

    # Load conversation with patient/agent for permissions & detectors
    conv = get_object_or_404(
        Conversation.objects.select_related("patient__navigator", "agent"),
        id=conv_pk
    )

    # Permission: staff or assigned CTN only
    if not (is_admin(request.user) or (conv.patient and conv.patient.navigator_id == request.user.id)):
        return HttpResponseForbidden(_("You do not have permission to update this conversation."))

    # Parse payload
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    rating   = payload.get("rating", None)
    feedback = payload.get("feedback", None)
    flags    = payload.get("flags", None)   # dict of {label: bool}
    label    = payload.get("label", None)   # single label
    value    = payload.get("value", None)   # single value

    # Update rating
    if rating is not None:
        try:
            r = int(rating)
            if 1 <= r <= 5:
                conv.rating = r
            else:
                return HttpResponseBadRequest("Rating must be 1..5")
        except ValueError:
            return HttpResponseBadRequest("Rating must be integer")

    # Update free-text feedback
    if feedback is not None:
        conv.feedback = (feedback or "").strip()

    # Build the canonical label set:
    #   • From agent.detectors (primary source)
    #   • Union with existing auto_flags/human_flags keys
    #   • Union with any keys provided in payload
    agent_detectors = {}
    if conv.agent and isinstance(getattr(conv.agent, "detectors", None), dict):
        agent_detectors = conv.agent.detectors or {}

    existing_auto  = conv.auto_flags or {}
    existing_human = conv.human_flags or {}

    payload_flags = flags if isinstance(flags, dict) else {}
    single_flag   = {str(label): bool(value)} if (label is not None and value is not None) else {}

    canonical_keys = (
        set(agent_detectors.keys())
        | set(existing_auto.keys())
        | set(existing_human.keys())
        | set(payload_flags.keys())
        | set(single_flag.keys())
    )

    # Start from existing human flags, then apply payload values.
    # Any missing keys are explicitly set to False so we persist ALL labels.
    new_human = {k: bool(existing_human.get(k, False)) for k in canonical_keys}
    for k, v in payload_flags.items():
        new_human[str(k)] = bool(v)
    for k, v in single_flag.items():
        new_human[str(k)] = bool(v)

    conv.human_flags = new_human

    #conv.last_message_at = timezone.now()
    conv.save(update_fields=["rating", "feedback", "human_flags"])#"last_message_at"])

    return JsonResponse({
        "ok": True,
        "rating": conv.rating,
        "feedback": conv.feedback,
        "human_flags": conv.human_flags,
    })


