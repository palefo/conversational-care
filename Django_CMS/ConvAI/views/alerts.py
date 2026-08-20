from ._base import *  # noqa: F401,F403

__all__ = ['act_alert', 'alert_detail', 'send_alert_sms', 'alerts_since']


def _after_action(request, alert, default="dashboard"):
    """Where to land after acting on an alert.

    Actions taken from the detail panel post a `next` pointing back at the page
    the panel was open on, so clearing a queue returns you to the queue with
    your filters intact. Only same-site paths are honoured, so `next` cannot be
    used to bounce someone off the platform.
    """
    nxt = (request.POST.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    if default == "dashboard":
        return redirect("dashboard")
    return redirect("alert_detail", pk=alert.pk)


@login_required
def alert_detail(request, pk: int):
    """
    Shows the alert details, handles alert action buttons, saves internal notes,
    and links to the related patient conversation.

    Important: this version does NOT require Patient.chatbot_enabled.
    Chatbot enable/disable state is stored in alert.data["chatbot_status"] only.
    """
    alert = get_object_or_404(
        Alert.objects.select_related(
            "patient",
            "patient__caregiver",
            "patient__navigator",
            "user",
        ),
        pk=pk,
    )

    can_view = (
        is_admin(request.user)
        or alert.user_id == request.user.id
        or (
            alert.patient
            and alert.patient.navigator_id == request.user.id
        )
    )

    if not can_view:
        return HttpResponseForbidden(_("You do not have permission to view this alert."))

    if request.method == "POST":
        form_type = (request.POST.get("form_type") or "").strip()
        data = alert.data if isinstance(alert.data, dict) else {}

        if form_type in {"action_update", "status_update"}:
            action_taken = (request.POST.get("action_taken") or "").strip()
            archive_bucket = (request.POST.get("archive_bucket") or "").strip()

            try:
                new_status = int(request.POST.get("status", alert.status))
            except (TypeError, ValueError):
                messages.error(request, _("Invalid alert status."))
                return redirect("alert_detail", pk=alert.pk)

            valid_statuses = {
                Alert.AlertStatus.CREATED,
                Alert.AlertStatus.IN_PROGRESS,
                Alert.AlertStatus.RESOLVED,
            }

            if new_status not in valid_statuses:
                messages.error(request, _("Invalid alert status."))
                return redirect("alert_detail", pk=alert.pk)

            # Requested workflow:
            # Reviewed -> In Progress until resolved.
            # Disable chatbot -> In Progress until enabled again.
            # Enable chatbot -> In Progress and feedback can be saved.
            # False Alarm -> Resolved, false alarms folder in archive.
            # Resolved -> Resolved, resolved folder in archive.
            # Reopened -> In Progress, back to active alerts.
            if action_taken in {"reviewed", "set_in_progress"}:
                new_status = Alert.AlertStatus.IN_PROGRESS
                archive_bucket = ""
                data["review_state"] = "review_in_progress"

            elif action_taken == "chatbot_disabled":
                new_status = Alert.AlertStatus.IN_PROGRESS
                archive_bucket = ""
                data["chatbot_status"] = "disabled"
                data["chatbot_disabled_at"] = timezone.now().isoformat()
                data["chatbot_disabled_by"] = request.user.get_username()

            elif action_taken == "chatbot_enabled":
                new_status = Alert.AlertStatus.IN_PROGRESS
                archive_bucket = ""
                data["chatbot_status"] = "enabled"
                data["chatbot_enabled_at"] = timezone.now().isoformat()
                data["chatbot_enabled_by"] = request.user.get_username()

            elif action_taken == "false_alarm":
                new_status = Alert.AlertStatus.RESOLVED
                archive_bucket = "false_alarms"

            elif action_taken in {"resolved", "manual_resolved"}:
                new_status = Alert.AlertStatus.RESOLVED
                archive_bucket = archive_bucket or "resolved"

            elif action_taken == "reopened":
                new_status = Alert.AlertStatus.IN_PROGRESS
                archive_bucket = ""
                data["archive_bucket"] = ""
                data["reopened_at"] = timezone.now().isoformat()
                data["reopened_by"] = request.user.get_username()

            elif action_taken == "New":
                new_status = Alert.AlertStatus.CREATED
                archive_bucket = ""

            if new_status == Alert.AlertStatus.RESOLVED and not archive_bucket:
                archive_bucket = "resolved"

            # What changed for the person. One line, written by whoever closed
            # it, and the only record of the good half of this work — the rest
            # of the platform only ever shows what is still owed. Stored on the
            # alert's own JSON, so no migration and no new table.
            outcome = (request.POST.get("outcome") or "").strip()[:280]
            if outcome and new_status == Alert.AlertStatus.RESOLVED:
                data["outcome"] = outcome
                data["outcome_at"] = timezone.now().isoformat()
                data["outcome_by"] = request.user.get_username()

            # Store resolution/false alarm/chatbot re-enable feedback when supplied.
            feedback_fields = {
                "feedback_reason": (request.POST.get("feedback_reason") or "").strip(),
                "feedback_accuracy": (request.POST.get("feedback_accuracy") or "").strip(),
                "feedback_response": (request.POST.get("feedback_response") or "").strip(),
                "feedback_issue": (request.POST.get("feedback_issue") or "").strip(),
                "feedback_comments": (request.POST.get("feedback_comments") or "").strip(),
            }

            if any(feedback_fields.values()):
                data["resolution_feedback"] = {
                    **feedback_fields,
                    "submitted_at": timezone.now().isoformat(),
                    "submitted_by": request.user.get_username(),
                    "action_context": action_taken,
                }

            # Archive bucket determines where resolved items appear in the archive.
            if archive_bucket:
                data["archive_bucket"] = archive_bucket
            elif action_taken in {"reopened", "reviewed", "set_in_progress", "chatbot_disabled", "chatbot_enabled", "New"}:
                data["archive_bucket"] = ""

            data["last_action_taken"] = action_taken
            data["last_action_status"] = int(new_status)
            data["last_action_at"] = timezone.now().isoformat()
            data["last_action_by"] = request.user.get_username()

            action_log = data.get("action_log", [])
            if not isinstance(action_log, list):
                action_log = []

            action_log.append({
                "action": action_taken,
                "status": int(new_status),
                "archive_bucket": archive_bucket,
                "at": timezone.now().isoformat(),
                "by": request.user.get_username(),
            })
            data["action_log"] = action_log[-25:]

            alert.status = new_status
            alert.data = data
            alert.acted_at = timezone.now()
            alert.save(update_fields=["status", "data", "acted_at", "updated_at"])

            if action_taken == "reopened":
                messages.success(request, _("Alert reopened and moved back to Active Alerts."))
                return _after_action(request, alert)

            if new_status == Alert.AlertStatus.RESOLVED:
                if archive_bucket == "false_alarms":
                    messages.success(request, _("Alert marked as false alarm and moved to the resolved archive."))
                else:
                    messages.success(request, _("Alert resolved and moved to the resolved archive."))
                return _after_action(request, alert)

            if action_taken == "chatbot_disabled":
                messages.success(request, _("Chatbot marked as disabled for this alert. Alert moved to In Progress."))
            elif action_taken == "chatbot_enabled":
                messages.success(request, _("Chatbot marked as enabled again. Alert remains In Progress until resolved."))
            elif new_status == Alert.AlertStatus.IN_PROGRESS:
                messages.success(request, _("Alert moved to In Progress."))
            elif new_status == Alert.AlertStatus.CREATED:
                messages.success(request, _("Alert moved back to New."))
            else:
                messages.success(request, _("Alert updated."))

            return _after_action(request, alert, default="alert_detail")

        if form_type == "note_update":
            # A note is a Note row, the same record the detail panel writes and
            # reads. It used to be a single string overwritten inside the
            # alert's JSON, with a parallel `note_log` list that nothing ever
            # displayed — so a note written here was invisible in the panel, and
            # the previous one was gone.
            body = (request.POST.get("internal_note") or "").strip()
            if body:
                Note.objects.create(alert=alert, body=body, author=request.user)
                messages.success(request, _("Note saved."))
            else:
                messages.warning(request, _("Write something first."))
            return redirect("alert_detail", pk=alert.pk)

    conversation_review_url = None
    conversation_messages = Message.objects.none()
    conversation_id = ""

    if isinstance(alert.data, dict):
        conversation_id = (
            alert.data.get("conversation_id")
            or alert.data.get("thread_id")
            or alert.data.get("conv_id")
            or ""
        )

    # Best case: alert contains the exact conversation/thread id.
    if conversation_id and alert.patient:
        first_msg = (
            Message.objects
            .filter(conversation_id=str(conversation_id))
            .order_by("timestamp")
            .first()
        )

        if first_msg:
            day = timezone.localtime(first_msg.timestamp).date().strftime("%Y-%m-%d")
            base = reverse("patient_conversation_detail", kwargs={
                "pk": alert.patient.pk,
                "day": day,
            })
            conversation_review_url = f"{base}?{urlencode({'visited': str(conversation_id)})}"

            conversation_messages = (
                Message.objects
                .filter(conversation_id=str(conversation_id))
                .order_by("timestamp")
            )

    # Fallback: if no conversation_id is saved, find the nearest patient messages
    # (matched by phone OR patient-linked Conversation, like the patient views).
    elif alert.patient:
        patient_q = patient_message_q(alert.patient)
        first_msg = (
            Message.objects
            .filter(patient_q)
            .order_by("-timestamp")
            .first()
        )

        if first_msg:
            conversation_id = first_msg.conversation_id or ""
            day = timezone.localtime(first_msg.timestamp).date().strftime("%Y-%m-%d")
            base = reverse("patient_conversation_detail", kwargs={
                "pk": alert.patient.pk,
                "day": day,
            })

            if conversation_id:
                conversation_review_url = f"{base}?{urlencode({'visited': str(conversation_id)})}"
            else:
                conversation_review_url = base

            conversation_messages = (
                Message.objects
                .filter(patient_q, timestamp__date=timezone.localtime(first_msg.timestamp).date())
                .order_by("timestamp")
            )

    # Final fallback: always build a conversation link from the patient and the
    # alert's own date when one wasn't resolved above. This mirrors the alerts
    # list and does not depend on a saved conversation_id.
    if conversation_review_url is None and alert.patient:
        day = timezone.localtime(alert.created_at).date().strftime("%Y-%m-%d")
        conversation_review_url = reverse("patient_conversation_detail", kwargs={
            "pk": alert.patient.pk,
            "day": day,
        })

    return render(request, "alerts/alert_detail.html", {
        "alert": alert,
        "conversation_review_url": conversation_review_url,
        "conversation_messages": conversation_messages,
        "conversation_id": conversation_id,
        # The same rows the detail panel's Notes tab shows, so the two surfaces
        # cannot disagree about what was written on an alert.
        "notes_list": (Note.objects.filter(alert=alert)
                       .select_related("author").order_by("-created_at")),
        "active_page": "alerts",
    })


@login_required
def act_alert(request, pk: int):
    """
    Calls the alert webhook (if present). Transitions status to IN_PROGRESS on click.
    If the webhook responds 2xx, records success; otherwise shows error.
    """
    alert = get_object_or_404(Alert, pk=pk)
    if not (is_admin(request.user) or alert.user_id == request.user.id):
        return HttpResponseForbidden(_("You do not have permission to act on this alert."))

    # Move to IN_PROGRESS when user acts
    if alert.status == Alert.AlertStatus.CREATED:
        alert.status = Alert.AlertStatus.IN_PROGRESS
        alert.save(update_fields=["status", "updated_at"])

    # Alert.webhook_url is commented out in models.py, so reading it directly
    # raised AttributeError and returned a 500 rather than the intended message.
    # Whenever the field comes back this keeps working unchanged.
    webhook_url = getattr(alert, "webhook_url", "")
    if not webhook_url:
        messages.error(request, _("No webhook is configured for this alert."))
        return redirect("alert_detail", pk=alert.pk)

    payload = {
        "id": alert.id,
        "title": alert.title,
        "description": alert.description,
        "data": alert.data,
        "alert_type": alert.get_alert_type_display(),
        "priority": alert.get_priority_display(),
        "status": alert.get_status_display(),
        "user": alert.user.username,
        "created_at": alert.created_at.isoformat(),
    }

    try:
        ### start response #############
        resp = requests.post(webhook_url, json=payload, timeout=15)


        ################################
        ok = 200 <= resp.status_code < 300
        alert.last_action_ok = ok
        alert.last_action_status = resp.status_code
        # Truncate to keep DB safe
        body_txt = ""
        try:
            body_txt = resp.text or ""
        except Exception:
            body_txt = ""
        alert.last_action_body = body_txt[:4000]
        alert.acted_at = timezone.now()
        # (Optional) auto-resolve on success:
        # if ok: alert.status = Alert.AlertStatus.RESOLVED
        alert.save(update_fields=["last_action_ok","last_action_status","last_action_body","acted_at","updated_at"])
        if ok:
            messages.success(request, _("Webhook executed successfully."))
        else:
            messages.error(request, f"Webhook respondió con estado {resp.status_code}.")
    except requests.RequestException as e:
        alert.last_action_ok = False
        alert.last_action_status = None
        alert.last_action_body = f"Request failed: {e}"[:4000]
        alert.acted_at = timezone.now()
        alert.save(update_fields=["last_action_ok","last_action_status","last_action_body","acted_at","updated_at"])
        messages.error(request, f"No se pudo contactar el webhook: {e}")

    return redirect("alert_detail", pk=alert.pk)


@admin_required
@login_required
@require_POST
def send_alert_sms(request, alert_id: int):
    """
    Sends the 'start conversation (alert)' SMS via Twilio Content Template,
    logs it under the SAME conversation_id used by LangGraph, and appends a
    SYSTEM note (with alert_id) into that conversation history.
    """
    alert = get_object_or_404(Alert, pk=alert_id)

    # Resolve recipient phone from alert.user
    to_number = getattr(alert.user, "phone_number", None)
    if not to_number:
        messages.error(request, _("The alert's user has no phone number."))
        return redirect("alert_detail", pk=alert.id)

    # Try to map phone -> patient (for thread + agent)
    patient = (
        Patient.objects
        .filter(Q(phone_number=to_number) | Q(caregiver__phone_number=to_number))
        .select_related("agent")
        .first()
    )
    thread_id = None
    if patient and patient.agent:
        thread_id = _get_or_create_thread(patient)

    # Template config (from env)
    template_sid = get_setting("SMS_TEMPLATE_START_INFECTION_SID")
    body_to_log = get_setting("SMS_TEMPLATE_START_INFECTION_TEXT")

    # Send via Twilio content template
    ok = send_sms_with_template(to_e164=str(to_number), content_sid=template_sid)
    if not ok:
        messages.error(request, _("Cannot send SMS via Twilio."))
        return redirect("alert_detail", pk=alert.id)

    # --- Log outbound under the SAME conversation_id (if we have a patient/thread) ---
    if thread_id:
        # store as a bot/assistant message in that conversation
        save_message(
            phone=str(to_number),
            user_message="",                 # no inbound from user in this step
            response_message=body_to_log,    # exact text we sent
            thread_id=thread_id,
            patient=patient,
        )

        # Also append a SYSTEM note containing alert_id into that conversation history
        system_note = (
            "__ALERT_CONTEXT__\n"
            f"alert_id={alert.id}\n"
            f"thread_id={thread_id}\n"
            f"set_at={timezone.now().isoformat()}\n"
            "instruction=When appropriate, call the API to mark this alert as finished using the given alert_id.\n"
        )
        append_system_note_to_langgraph(patient, system_note, thread_id=thread_id)
    else:
        # Fallback logging if we couldn't resolve a patient/thread
        Message.objects.create(
            conversation_id=f"alert-{alert.id}",
            user=str(to_number),
            user_message="",
            response_message=body_to_log,
        )

    # Progress the alert if still CREATED
    if alert.status == Alert.AlertStatus.CREATED:
        alert.status = Alert.AlertStatus.IN_PROGRESS
        alert.save(update_fields=["status", "updated_at"])

    messages.success(request, _("SMS sent successfully."))
    return redirect("alert_detail", pk=alert.id)


# How many new alerts a single poll will show as toasts. Beyond this the badge
# carries the rest — five toasts stacking up is not a notification any more.
_SINCE_MAX = 3


@login_required
@require_GET
def alerts_since(request):
    """Alerts raised since `t`, for the notification poller in base.html.

    `t` is epoch milliseconds from a previous reply, so the browser never has
    to agree with the server about clocks or time zones — it just hands back
    the number it was given. Without `t` the reply carries no alerts at all,
    only the current time: opening a page should start the clock, not replay
    everything that happened while you were away. That is what the queue on
    the dashboard is for.

    Scope is the same rule the dashboard uses: your own alerts and the alerts
    of clients you navigate, unless you are an admin.
    """
    qs = Alert.objects.exclude(status=Alert.AlertStatus.RESOLVED)
    if not is_admin(request.user):
        qs = qs.filter(Q(user=request.user) | Q(patient__navigator=request.user))

    now_ms = int(timezone.now().timestamp() * 1000)
    raw = (request.GET.get("t") or "").strip()
    if not raw:
        return JsonResponse({"now": now_ms, "alerts": [], "more": 0})

    try:
        since = dt.datetime.fromtimestamp(int(raw) / 1000, tz=dt.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return JsonResponse({"now": now_ms, "alerts": [], "more": 0})

    fresh = list(
        qs.filter(created_at__gt=since)
        .select_related("patient")
        .order_by("-created_at")[: _SINCE_MAX + 1]
    )
    more = max(0, len(fresh) - _SINCE_MAX)
    fresh = fresh[:_SINCE_MAX]

    out = []
    for a in fresh:
        who = f"{a.patient.name} {a.patient.lastname}".strip() if a.patient else ""
        high = a.priority == Alert.Priority.HIGH
        label = _("High priority") if high else _("New chat to review")
        out.append({
            "id": a.pk,
            "high": high,
            "title": f"{label} — {who}" if who else (a.title or str(label)),
            "body": a.title or a.description or "",
            # Just the panel token. The browser hangs it off whatever page it
            # is on, keeping the filters and sort already in the URL, so acting
            # on an alert never costs you the list you were working.
            "token": f"alert-{a.pk}",
        })

    return JsonResponse({"now": now_ms, "alerts": out, "more": more})
