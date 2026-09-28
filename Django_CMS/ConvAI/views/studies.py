"""Study create / edit / delete, reached from Settings -> Participants.

Follows the protocol editor: the settings tab lists the studies, and each one
opens a full page of its own, because a consent form is too much to edit inside a
tab and too important to edit in a modal.

Deleting is guarded the way a protocol's editability is guarded. A study with
enrolments is evidence about real people, so it cannot be deleted — it is closed
instead, which stops new enrolments and leaves everything else working.
"""
from ._base import *  # noqa: F401,F403

from .. import enrolment as enrolment_service

__all__ = ['study_create', 'study_editor', 'study_editor_save', 'study_delete']


def _enrolment_on_or_404():
    """Every page here 404s while the feature is off, as the exports do."""
    if not enrolment_service.enabled():
        raise Http404("Study enrolment is not enabled.")


@login_required
@admin_required
@require_POST
def study_create(request):
    _enrolment_on_or_404()
    n = Study.objects.count() + 1
    study = Study(slug=f"study-{n}", display_name=f"Study {n}")
    # Uniqueness is not guaranteed by the count alone (a deleted study leaves a
    # gap), so walk forward until the slug is free.
    while Study.objects.filter(slug=study.slug).exists():
        n += 1
        study.slug = f"study-{n}"
        study.display_name = f"Study {n}"
    study.save()  # seeds the default consent items
    messages.success(request, _("Study created. Set its name and consent wording below."))
    return redirect("study_editor", pk=study.pk)


@login_required
@admin_required
def study_editor(request, pk):
    _enrolment_on_or_404()
    study = get_object_or_404(Study, pk=pk)
    return render(request, "settings/study_editor.html", {
        "active_page": "admin",
        "study": study,
        "form": StudyForm(instance=study),
        "consent_locked": study.consent_locked,
        "stale_consent_count": study.stale_consent_count,
        "enrolment_count": study.enrolments.count(),
        "consent_count": ConsentRecord.objects.filter(enrolment__study=study).count(),
    })


@login_required
@admin_required
@require_POST
def study_editor_save(request, pk):
    _enrolment_on_or_404()
    study = get_object_or_404(Study, pk=pk)
    form = StudyForm(request.POST, instance=study)
    if form.is_valid():
        form.save()
        messages.success(request, _("Study saved."))
        return redirect("study_editor", pk=study.pk)

    messages.error(request, _("Please correct the errors below."))
    # is_valid() has already copied the posted values onto form.instance, which
    # is ``study``. The header and the lock describe what is saved, so they are
    # read from a fresh copy; the form keeps what was typed.
    saved = Study.objects.get(pk=study.pk)
    return render(request, "settings/study_editor.html", {
        "active_page": "admin",
        "study": saved,
        "form": form,
        "consent_locked": saved.consent_locked,
        "stale_consent_count": saved.stale_consent_count,
        "enrolment_count": saved.enrolments.count(),
        "consent_count": ConsentRecord.objects.filter(enrolment__study=saved).count(),
    })


@login_required
@admin_required
@require_POST
def study_delete(request, pk):
    _enrolment_on_or_404()
    study = get_object_or_404(Study, pk=pk)
    if study.enrolments.exists():
        # PROTECT on the FK would raise anyway; this is the readable version of
        # that error, with the thing they actually want to do next.
        messages.error(request, _(
            "This study has participants enrolled, so it cannot be deleted. "
            "Close it instead to stop new enrolments."))
        return redirect("study_editor", pk=study.pk)
    name = study.display_name
    study.delete()
    messages.success(request, _('Study "%(name)s" deleted.') % {"name": name})
    return redirect(f"{reverse('config')}?tab=participants#participants")
