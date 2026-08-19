from ._base import *  # noqa: F401,F403
from ..summarization import (
    summarize_meeting as _summarize_meeting,
    transcribe_and_summarize_recording as _transcribe_and_summarize,
)

__all__ = ['summarize_meeting_view', 'transcribe_recording_view']


def _patient_for_recording(rec):
    """Find the patient a recording belongs to (matched by callee/caller number)."""
    nums = {str(rec.to_number or ""), str(rec.from_number or "")}
    nums.discard("")
    if not nums:
        return None
    return (
        Patient.objects
        .filter(Q(phone_number__in=nums) | Q(caregiver__phone_number__in=nums))
        .select_related("navigator")
        .first()
    )


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
    if not is_admin(request.user):
        if not (patient and patient.navigator_id == request.user.id):
            return HttpResponseForbidden(_("You cannot transcribe this recording."))

    try:
        _transcribe_and_summarize(rec)
        messages.success(request, _("Recording transcribed and summarized."))
    except Exception as exc:  # Whisper/LLM/config errors shouldn't 500 the page
        messages.error(request, _("Could not transcribe the recording: %(err)s") % {"err": exc})

    fallback = (redirect("patient_detail", pk=patient.pk) if patient
                else redirect(request.META.get("HTTP_REFERER") or "patients"))
    return _back(request, fallback)
