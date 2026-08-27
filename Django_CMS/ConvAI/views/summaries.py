from ._base import *  # noqa: F401,F403
from ..summarization import (
    summarize_meeting as _summarize_meeting,
    transcribe_and_summarize_recording as _transcribe_and_summarize,
)

__all__ = ['summarize_meeting_view', 'transcribe_recording_view', 'edit_overview']


def _patient_for_recording(rec):
    """The client a recording belongs to.

    The rule moved onto the model when recordings gained a client of their own:
    named where the call wrote one down, matched by number only where it did
    not. See CallRecording.resolve_patient.
    """
    return rec.resolve_patient()


def _back(request, fallback):
    """Return to the panel that fired this, or fall back to the old target.

    Both of these actions are now taken from inside the detail panel, where
    being thrown to the client page loses the list, the filters and the panel
    itself. Only our own paths are followed.
    """
    nxt = (request.POST.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return fallback


@login_required
@require_POST
def summarize_meeting_view(request, meeting_id):
    """Summarize a meeting's protocol answers with the configured prompt."""
    meeting = get_object_or_404(Meeting.objects.select_related("patient__navigator"), pk=meeting_id)
    if not (is_admin(request.user) or meeting.patient.navigator_id == request.user.id):
        return HttpResponseForbidden(_("You cannot summarize this meeting."))

    try:
        _summarize_meeting(meeting)
        messages.success(request, _("Protocol summary generated."))
    except Exception as exc:  # LLM/config errors shouldn't 500 the page
        messages.error(request, _("Could not generate the summary: %(err)s") % {"err": exc})

    return _back(request, redirect("patient_detail", pk=meeting.patient_id))


@login_required
@require_POST
def transcribe_recording_view(request, sid):
    """Transcribe a call recording with Whisper and summarize the transcript."""
    rec = get_object_or_404(CallRecording, recording_sid=sid)
    patient = _patient_for_recording(rec)
    # Checked against every client the recording could be, not just the one
    # named above. They differ only for legacy rows on a shared number, where
    # narrowing to one would take a recording away from a navigator who could
    # reach it yesterday over a row that has no better answer.
    if not is_admin(request.user):
        if not rec.owner_patients().filter(navigator=request.user).exists():
            return HttpResponseForbidden(_("You cannot transcribe this recording."))

    try:
        _transcribe_and_summarize(rec)
        messages.success(request, _("Recording transcribed and summarized."))
    except Exception as exc:  # Whisper/LLM/config errors shouldn't 500 the page
        messages.error(request, _("Could not transcribe the recording: %(err)s") % {"err": exc})

    fallback = (redirect("patient_detail", pk=patient.pk) if patient
                else redirect(request.META.get("HTTP_REFERER") or "patients"))
    return _back(request, fallback)


# ─────────────────────────── editing an overview ──────────────────────────
# Which field each kind keeps its overview in, and how its pk is looked up.
# A recording is addressed by recording_sid everywhere else in the app, so it
# is addressed that way here too.
OVERVIEW = {
    "meeting": (Meeting, "protocol_summary", "pk"),
    "recording": (CallRecording, "transcript_summary", "recording_sid"),
    "conversation": (Conversation, "summary", "pk"),
    # Only classifier-raised alerts reach here; the panel withholds the edit
    # URL from the ones a person wrote, so there is nothing to post to.
    "alert": (Alert, "description", "pk"),
}


def _overview_patient(kind, obj):
    """The client whose permissions govern this overview."""
    if kind == "recording":
        return _patient_for_recording(obj)
    return getattr(obj, "patient", None)


@login_required
@require_POST
def edit_overview(request, kind, pk):
    """Replace a generated overview with what a person actually wants it to say.

    The generated text is not sacred — a navigator who was on the call knows
    better than the model what the call was about, and correcting it in place
    beats writing "actually, ..." in a note underneath. What matters is that
    the record then says a person wrote it, which is what SummaryEdit is for.
    """
    spec = OVERVIEW.get(kind)
    if spec is None:
        return JsonResponse({"ok": False, "error": _("Unknown kind.")}, status=400)
    model, field, lookup = spec

    obj = get_object_or_404(model, **{lookup: pk})
    patient = _overview_patient(kind, obj)
    if not (is_admin(request.user)
            or (patient and patient.navigator_id == request.user.id)):
        return HttpResponseForbidden(_("You cannot edit this summary."))

    if request.content_type and "json" in request.content_type:
        try:
            body = (json.loads(request.body or b"{}").get("body") or "").strip()
        except (ValueError, AttributeError):
            body = ""
    else:
        body = (request.POST.get("body") or "").strip()

    if not body:
        return JsonResponse({"ok": False, "error": _("Write something first.")}, status=400)

    setattr(obj, field, body)
    obj.save(update_fields=[field])

    # update_or_create rather than a new row each time: this records who the
    # text belongs to now, not every hand that has passed over it.
    edit, _created = SummaryEdit.objects.update_or_create(
        **{kind: obj}, defaults={"author": request.user},
    )
    return JsonResponse({
        "ok": True,
        "body": body,
        "author": request.user.get_full_name() or request.user.username,
        "when": timezone.localtime(edit.edited_at).strftime("%d %b · %H:%M"),
    })
