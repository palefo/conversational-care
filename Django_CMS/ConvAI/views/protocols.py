from ._base import *  # noqa: F401,F403
from django.utils.http import url_has_allowed_host_and_scheme

__all__ = ['_may_edit', 'protocol_create', 'protocol_delete', 'protocol_editor', 'protocol_editor_save', 'protocol_view', 'start_protocol_automation', 'dismiss_sms_offer']


def _may_edit(user, meeting):
    return is_admin(user) or meeting.patient.navigator == user


@login_required
def protocol_view(request, meeting_id, protocol_num):
    meeting  = get_object_or_404(
        Meeting.objects.select_related("patient__navigator"),
        pk=meeting_id
    )
    protocol = get_object_or_404(Protocol, number=protocol_num)

    # R3-02 fix: enforce ownership for READ and write (GET previously leaked answers).
    if not _may_edit(request.user, meeting):
        raise Http404()

    if request.method == "POST":
        if not _may_edit(request.user, meeting):
            raise Http404()
        form = ProtocolAnswerForm(meeting=meeting, protocol=protocol,
                                  data=request.POST)
        if form.is_valid():
            form.save()
            url = reverse(
                "protocol_view",
                kwargs={"meeting_id": meeting.id, "protocol_num": protocol.number}
            )
            return redirect(f"{url}?saved=ok")
    else:
        form = ProtocolAnswerForm(meeting=meeting, protocol=protocol)

    # answers for display-mode cards
    save_status = request.GET.get("saved")
    answers = {
        a.question_id: a.response
        for a in Answer.objects.filter(
            meeting=meeting, question__protocol=protocol
        )
    }

    q_fields = []
    for q in protocol.questions.all():
        field_name = f"q_{q.id}"
        if field_name in form.fields:
            q_fields.append((q, form[field_name]))

    return render(request, "protocols/protocol_form.html", {
        "meeting": meeting,
        "protocol": protocol,
        "form": form,
        "answers": answers,
        "can_edit": _may_edit(request.user, meeting),
        "q_fields": q_fields,
        "enable_automations": get_bool("ENABLE_AUTOMATIONS"),
        "save_status": save_status,
    })


@login_required
def protocol_editor(request, protocol_num):
    """
    Standalone Airtable / Google-Forms-style editor for a protocol's
    structure (title, description and its questions).

    A navigator can read a protocol; only staff can change one. It used to 404
    for everybody else, which made the questions unreachable from the client
    page — you could see that a protocol had been done three times and never
    what it asked. Saving stays staff-only, and a protocol with answers against
    it is read-only for everyone, so the data is no less protected.
    """
    protocol = get_object_or_404(Protocol, number=protocol_num)
    has_answers = Answer.objects.filter(question__protocol=protocol).exists()
    may_edit = is_admin(request.user)

    questions = [
        {"id": q.id, "prompt_md": q.prompt_md}
        for q in protocol.questions.all()
    ]

    return render(request, "protocols/protocol_editor.html", {
        "active_page": "admin",
        "protocol": protocol,
        "questions_json": questions,
        "question_count": len(questions),
        "has_answers": has_answers,
        "locked": has_answers or not may_edit,
        "may_edit": may_edit,
    })


def _repeatable_from(payload, protocol):
    """Read the repeatable flag out of an editor payload.

    Absent means unchanged rather than False, so a client that does not know
    about the field cannot silently turn a longitudinal protocol back into a
    one-off.
    """
    value = payload.get("repeatable")
    if value is None:
        return protocol.repeatable
    return bool(value)


@login_required
@require_POST
def protocol_editor_save(request, protocol_num):
    """
    Persist the full editor state for a protocol.

    Expects JSON:
        {
          "title": "...",
          "description": "...",
          "questions": [{"id": 12|null, "prompt_md": "..."}, ...]
        }

    Questions are reconciled positionally (DOM order == question order).
    Because the protocol is locked once any answer exists, here we can safely
    wipe-and-recreate the questions (no Answer rows reference them).
    """
    if not is_admin(request.user):
        raise Http404()

    protocol = get_object_or_404(Protocol, number=protocol_num)

    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    if Answer.objects.filter(question__protocol=protocol).exists():
        # Locked, with one exception: a payload carrying nothing but the
        # repeatable flag.
        #
        # Whether a protocol is answered once or on every call is something you
        # find out *after* using it once — the IQCODE looked like any other
        # protocol until the second round came due. Refusing it here would mean
        # the flag could only ever be set on a protocol nobody had used, which
        # is exactly the protocol nobody yet knows the answer for.
        #
        # Safe to allow: it changes how answers are read, never what they say.
        # Anything else in the payload and this is an ordinary edit, refused as
        # before — the exemption is for the flag, not for the lock.
        if set(payload) == {"repeatable"}:
            protocol.repeatable = bool(payload["repeatable"])
            protocol.save(update_fields=["repeatable"])
            return JsonResponse({"ok": True, "repeatable": protocol.repeatable})
        return JsonResponse(
            {"ok": False, "error": "locked",
             "message": "El protocolo tiene respuestas y no puede editarse."},
            status=409,
        )

    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    questions = payload.get("questions") or []

    if not title:
        return JsonResponse(
            {"ok": False, "error": "title_required",
             "message": "El título del protocolo es obligatorio."},
            status=400,
        )

    from django.db import transaction
    with transaction.atomic():
        protocol.title = title[:120]
        protocol.description = description
        protocol.repeatable = _repeatable_from(payload, protocol)
        protocol.save(update_fields=["title", "description", "repeatable"])

        # Safe because no answers reference these questions.
        protocol.questions.all().delete()

        order = 1
        for item in questions:
            prompt = (item.get("prompt_md") or "").strip()
            if not prompt:
                continue
            Question.objects.create(
                protocol=protocol, order=order, prompt_md=prompt
            )
            order += 1

    return JsonResponse({"ok": True, "redirect": reverse("config")})


@login_required
@admin_required
@require_POST
def protocol_create(request):
    """Create a new, empty protocol and open it in the editor.

    The protocol stays fully editable until answers are recorded against it,
    at which point it locks to protect collected data.
    """
    next_number = (
        Protocol.objects.order_by("-number").values_list("number", flat=True).first() or 0
    ) + 1
    protocol = Protocol.objects.create(number=next_number, title=_("Untitled protocol"))
    messages.success(
        request,
        _("New protocol created. It stays editable until answers are recorded against it."),
    )
    return redirect("protocol_editor", protocol_num=protocol.number)


@login_required
@admin_required
@require_POST
def protocol_delete(request, protocol_num):
    """Delete an editable protocol. Refuses if answers reference it (locked)."""
    protocol = get_object_or_404(Protocol, number=protocol_num)
    if Answer.objects.filter(question__protocol=protocol).exists():
        messages.error(request, _("This protocol has recorded answers and cannot be deleted."))
    else:
        protocol.delete()
        messages.success(request, _("Protocol deleted."))
    return redirect(f"{reverse('config')}?tab=protocols#protocols")


@require_POST
@login_required
def start_protocol_automation(request, meeting_id, protocol_num):
    meeting = get_object_or_404(
        Meeting.objects.select_related("patient__caregiver", "patient__navigator"),
        pk=meeting_id
    )
    protocol = get_object_or_404(Protocol, number=protocol_num)

    if not get_bool("ENABLE_AUTOMATIONS"):
        return HttpResponseForbidden(_("Automations are disabled."))
    if not _may_edit(request.user, meeting):
        return HttpResponseForbidden(_("You do not have permission to do this."))

    # Which way to send. WhatsApp unless the navigator has answered the offer
    # made after a WhatsApp failure — see the failure branch below.
    channel = "sms" if request.POST.get("channel") == "sms" else "whatsapp"

    patient   = meeting.patient
    caregiver = patient.caregiver
    if not caregiver or not caregiver.phone_number:
        messages.error(request, _("The client has no caregiver with a valid phone number."))
        return redirect("protocol_view", meeting_id=meeting.id, protocol_num=protocol.number)

    # Resolve the built-in native Protocol QA agent.
    agent = Agent.objects.filter(
        kind=Agent.Kind.NATIVE, native_key="protocol_qa"
    ).first()
    if not agent:
        messages.error(request, _('The built-in "protocol_qa" agent is missing.'))
        return redirect("protocol_view", meeting_id=meeting.id, protocol_num=protocol.number)

    # Assign the agent, remember the context + agent to revert to, and open a
    # fresh thread. Native agents get context via extra_configurable (below),
    # re-injected on every inbound turn by process_message_for_patient.
    new_thread_id = start_automation(patient, agent, meeting, protocol.number)

    # Generate the first message (synthetic "Hello") with the automation context.
    reply_text = generate_response_langgraph(
        patient,
        "Hello",
        new_thread_id,
        extra_configurable=automation_context(patient),
    )

    # Send, and persist ONLY the assistant reply.
    to_e164 = str(caregiver.phone_number)

    if channel == "sms":
        # The navigator has already been told WhatsApp could not reach them and
        # has chosen this, so there is no second question to ask here.
        sent_ok = send_sms_text(to_e164, reply_text)
        reason = "" if sent_ok else str(_("The SMS could not be sent."))
    else:
        # WhatsApp accepts a message it is about to fail, so the bool this used
        # to read said "sent" for a message nobody received — see
        # send_whatsapp_text_result.
        result = send_whatsapp_text_result(to_e164, reply_text)
        sent_ok, reason = result["ok"], result["reason"]

    if not sent_ok:
        # Nothing reached the caregiver, so nothing should behave as if it had.
        # Left as it was, the client stayed switched to the protocol agent for
        # the next three hours and the panel showed the protocol as out with
        # them — over a question they were never asked. Their next message,
        # about anything at all, would have been read as an answer to it.
        end_automation(patient)

        # WhatsApp being shut is not the same kind of failure as the number
        # being wrong: SMS would get there. Rather than quietly switching
        # channel on the navigator's behalf — health questions over SMS is
        # their call, not the platform's — the panel asks. The offer is held in
        # the session because it belongs to the person who pressed the button,
        # not to the meeting: a second navigator opening the same panel should
        # see the protocol un-asked, not somebody else's half-finished decision.
        # Said out loud either way. The dialogue below is the offer, but it only
        # exists in the panel, and this same button is on the protocol page,
        # which has no panel to render it into — so the failure would be
        # completely silent there.
        messages.warning(
            request,
            _("%(who)s was not asked anything: the message could not be sent. "
              "%(why)s") % {"who": caregiver.name, "why": reason},
        )

        if channel != "sms":
            request.session["ask_sms_offer"] = {
                "meeting": meeting.pk,
                "protocol": protocol.number,
                "who": caregiver.name,
                "reason": reason,
            }
            request.session.modified = True
    else:
        # Only what actually went out is written to the conversation.
        save_message(
            phone=to_e164,
            user_message="",
            response_message=reply_text,
            thread_id=new_thread_id,
            patient=patient,
        )
        request.session.pop("ask_sms_offer", None)
        if channel == "sms":
            messages.success(request, _("Sent to %s by SMS.") % caregiver.name)
        else:
            messages.success(request, _("Sent to %s by text.") % caregiver.name)

    # Back where it was pressed. Sending is now something you do from the panel
    # while working the call, so landing on a page of its own afterwards loses
    # the list, the filters and the panel underneath.
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return redirect(nxt)
    return redirect("protocol_view", meeting_id=meeting.id, protocol_num=protocol.number)


@require_POST
@login_required
def dismiss_sms_offer(request):
    """Put away the "send it by SMS instead?" offer without sending anything.

    The offer is the only trace a failed WhatsApp send leaves — the automation
    was already reverted — so declining it is just forgetting it. No meeting is
    named because the session holds one offer at a time.
    """
    request.session.pop("ask_sms_offer", None)
    request.session.modified = True

    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return redirect(nxt)
    return redirect("dashboard")
