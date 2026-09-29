"""The staff side of study enrolment: issuing codes and following one participant.

There is no participant list of its own. A participant becomes a client the
moment they consent, so the Clients page is where the cohort lives; what it adds
is the part of the lifecycle that has no client record yet — people issued a code
who have not arrived — plus the form for issuing one (``enrolment_context``).
Each enrolment also has a detail page, for its consent history, status and
withdrawal.

Study scoping: a member of staff with ``ConvAIUser.study`` set sees only that
study. Blank sees everything, which is the right default for an installation
running one study or none. Admins ignore it.
"""
from ._base import *  # noqa: F401,F403

from .. import enrolment as enrolment_service

__all__ = ['enrolment_context', 'participant_enrol', 'participant_generate_code',
           'participant_detail', 'participant_status', 'participant_withdraw',
           'participant_approve']


def _enrolment_on_or_404():
    if not enrolment_service.enabled():
        raise Http404("Study enrolment is not enabled.")


def _visible_studies(user):
    """Studies this person may work with."""
    qs = Study.objects.all().order_by("display_name")
    if not is_admin(user) and getattr(user, "study_id", None):
        qs = qs.filter(pk=user.study_id)
    return qs


def _visible_enrolments(user):
    qs = Enrolment.objects.select_related("study", "patient").all()
    if not is_admin(user) and getattr(user, "study_id", None):
        qs = qs.filter(study_id=user.study_id)
    return qs


def enrolment_context(request):
    """The enrolment block shown on the Clients page.

    Not its own page. A participant becomes a client the moment they consent, so
    a separate list would have shown most of the cohort twice. What Clients
    genuinely cannot show is the part of the lifecycle with no client record yet —
    somebody issued a code who has not arrived — so that is what this adds, plus
    the form for issuing one.

    Returns an empty, falsy-``enabled`` dict while the feature is off, so the
    Clients page renders exactly as it did before this feature existed.
    """
    if not enrolment_service.enabled():
        return {"enabled": False}

    studies = _visible_studies(request.user)
    waiting = (_visible_enrolments(request.user)
               .filter(patient__isnull=True)
               .exclude(status=Enrolment.Status.WITHDRAWN)
               .order_by("-created_at"))

    counts = {
        row["status"]: row["n"]
        for row in _visible_enrolments(request.user).values("status").annotate(n=Count("id"))
    }

    return {
        "enabled": True,
        "open_studies": [s for s in studies if s.is_open],
        "waiting": waiting[:100],
        "counts": counts,
        "statuses": Enrolment.Status.choices,
        "withdrawn_count": counts.get(Enrolment.Status.WITHDRAWN, 0),
        "auto_approve": enrolment_service.auto_approve(),
        "join_url": request.build_absolute_uri(reverse("enrolment_landing")),
        "is_admin": is_admin(request.user),
    }


@login_required
@navigator_required
@require_POST
def participant_enrol(request):
    """Pre-enrol somebody and issue their code."""
    _enrolment_on_or_404()

    study = _visible_studies(request.user).filter(pk=request.POST.get("study") or 0).first()
    if study is None:
        messages.error(request, _("Choose a study to enrol this participant into."))
        return redirect("patients")

    try:
        enrolment = enrolment_service.create_enrolment(
            study=study,
            name=request.POST.get("name", ""),
            lastname=request.POST.get("lastname", ""),
            phone_number=(request.POST.get("phone_number") or "").strip() or None,
            access_code=request.POST.get("access_code", ""),
            created_by=request.user,
        )
    except (ValueError, RuntimeError) as exc:
        messages.error(request, str(exc))
        return redirect("patients")

    # The code is the deliverable of this action, so it goes in the message —
    # the clinician is about to read it out or write it on a card.
    messages.success(request, _(
        'Enrolled %(name)s %(lastname)s. Their access code is %(code)s'
    ) % {"name": enrolment.name, "lastname": enrolment.lastname,
         "code": enrolment.access_code})
    return redirect(f"{reverse('patients')}?highlight={enrolment.pk}")


@login_required
@navigator_required
def participant_generate_code(request):
    """AJAX: a fresh unused code for the Generate button."""
    _enrolment_on_or_404()
    from ..enrolment.codes import generate_access_code

    try:
        code = generate_access_code(
            enrolment_service.code_words(),
            exclude=lambda c: Enrolment.objects.filter(access_code=c).exists(),
        )
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=500)
    return JsonResponse({"code": code})


def _enrolment_or_404(request, pk):
    enrolment = get_object_or_404(_visible_enrolments(request.user), pk=pk)
    return enrolment


@login_required
@navigator_required
def participant_detail(request, pk):
    _enrolment_on_or_404()
    enrolment = _enrolment_or_404(request, pk)
    return render(request, "participants/participant_detail.html", {
        "active_page": "patients",
        "enrolment": enrolment,
        "consents": enrolment.consents.all(),
        "manual_statuses": [(s.value, s.label) for s in Enrolment.MANUAL_STATUSES],
        "consent_is_current": enrolment.consent_is_current,
        "auto_approve": enrolment_service.auto_approve(),
    })


@login_required
@navigator_required
@require_POST
def participant_status(request, pk):
    _enrolment_on_or_404()
    enrolment = _enrolment_or_404(request, pk)
    new = (request.POST.get("status") or "").strip()

    # Only someone taking part can be marked finished (or back again), and only
    # between those two states: see Enrolment.MANUAL_STATUSES for why the rest
    # are not a member of staff's to choose.
    if (new not in Enrolment.MANUAL_STATUSES
            or enrolment.patient_id is None
            or enrolment.status == Enrolment.Status.WITHDRAWN):
        messages.error(request, _(
            "Only a participant who is taking part can be marked active or completed."))
        return redirect("participant_detail", pk=pk)

    enrolment.status = new
    enrolment.save(update_fields=["status", "updated_at"])
    messages.success(request, _("Status updated."))
    return redirect("participant_detail", pk=pk)


@login_required
@navigator_required
@require_POST
def participant_withdraw(request, pk):
    _enrolment_on_or_404()
    enrolment = _enrolment_or_404(request, pk)
    enrolment_service.withdraw(
        enrolment, reason=request.POST.get("reason", ""), by=request.user
    )
    messages.success(request, _(
        "Participant withdrawn. Their agent has been switched off so the platform "
        "will not message them again. Nothing has been deleted."))
    return redirect("participant_detail", pk=pk)


@login_required
@admin_required
@require_POST
def participant_approve(request, pk):
    """Admit a consented participant by hand, where auto-approval is off."""
    _enrolment_on_or_404()
    enrolment = _enrolment_or_404(request, pk)
    try:
        enrolment_service.approve(enrolment)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("participant_detail", pk=pk)
    messages.success(request, _("Participant admitted; their client record now exists."))
    return redirect("participant_detail", pk=pk)
