"""Notes on a call, a meeting, an alert or a conversation.

One endpoint set for all four, because a note is the same thing wherever it is
written and the panel shows it identically. Permission is decided by the client
the parent belongs to, which is the rule everywhere else in the app.

Each answers JSON so the panel can save without losing the list, the filters and
the scroll position behind it.
"""

import json

from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from ..models import Alert, CallRecording, Conversation, Meeting, Note
from ._base import is_admin

__all__ = ["add_note", "edit_note", "delete_note"]

# The four things a note can hang off, by the token the panel sends.
PARENTS = {
    "meeting": Meeting,
    "recording": CallRecording,
    "alert": Alert,
    "conversation": Conversation,
}


def _may_touch(request, note_or_parent):
    """A note is the client's, so whoever may see the client may write on it."""
    if is_admin(request.user):
        return True
    patient = getattr(note_or_parent, "patient", None)
    if callable(patient):
        patient = patient()
    return bool(patient and patient.navigator_id == request.user.id)


def _body(request):
    """Accept a form post or a JSON body; the panel sends JSON."""
    if request.content_type and "json" in request.content_type:
        try:
            return (json.loads(request.body or b"{}").get("body") or "").strip()
        except (ValueError, AttributeError):
            return ""
    return (request.POST.get("body") or "").strip()


@require_POST
def add_note(request, kind, pk):
    model = PARENTS.get(kind)
    if model is None:
        return JsonResponse({"ok": False, "error": _("Unknown kind.")}, status=400)

    lookup = {"recording_sid": pk} if model is CallRecording else {"pk": pk}
    parent = get_object_or_404(model, **lookup)
    if not _may_touch(request, parent):
        return HttpResponseForbidden(_("You cannot write notes for this client."))

    body = _body(request)
    if not body:
        return JsonResponse({"ok": False, "error": _("Write something first.")}, status=400)

    note = Note(body=body, author=request.user)
    setattr(note, kind, parent)
    note.save()
    return JsonResponse({
        "ok": True,
        "id": note.pk,
        "body": note.body,
        "author": request.user.get_full_name() or request.user.username,
        "when": note.created_at.strftime("%d %b · %H:%M"),
    })


@require_POST
def edit_note(request, pk):
    note = get_object_or_404(Note, pk=pk)
    if not _may_touch(request, note):
        return HttpResponseForbidden(_("You cannot edit this note."))

    body = _body(request)
    if not body:
        return JsonResponse({"ok": False, "error": _("A note cannot be empty.")}, status=400)

    note.body = body
    note.save(update_fields=["body", "updated_at"])
    return JsonResponse({
        "ok": True,
        "id": note.pk,
        "body": note.body,
        "when": note.updated_at.strftime("%d %b · %H:%M"),
    })


@require_POST
def delete_note(request, pk):
    """Deleted for real.

    The panel holds the request back for a few seconds and offers Undo, so a
    misclick never reaches here; once it does, the person meant it. Keeping a
    tombstone would mean a note someone deleted still sitting in the database
    with their name on it.
    """
    note = get_object_or_404(Note, pk=pk)
    if not _may_touch(request, note):
        return HttpResponseForbidden(_("You cannot delete this note."))
    note.delete()
    return JsonResponse({"ok": True})
