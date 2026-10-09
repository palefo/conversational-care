"""The public join flow: /join/ -> claim -> consent -> done.

These are the **only unauthenticated pages in the platform**, which is what most
of the care here is about.

* The whole flow 404s unless the feature is switched on, so a default
  installation has no public surface at all.
* CSRF stays on. The implementation this replaces marked its endpoints
  ``@csrf_exempt``; nothing here needs that.
* A wrong code gets one message that does not distinguish "no such code" from
  "already claimed" from "that study has closed", and every attempt is counted
  against the caller's address. Otherwise the form is an oracle for guessing
  codes and confirming who is in a study.
* The name is never taken from the claim form. The clinician entered it; letting
  a form overwrite it would let somebody with a leaked code rewrite whose
  enrolment it is.

Progress is held in the session, keyed by enrolment id. Nothing sensitive is put
in a URL.
"""
from ._base import *  # noqa: F401,F403

from datetime import date as _date

from .. import enrolment as enrolment_service

__all__ = ['enrolment_landing', 'enrolment_claim', 'enrolment_consent',
           'enrolment_done', 'enrolment_resume']

SESSION_KEY = "enrolment_id"

DEFAULT_LANDING_TEXT = _(
    "If you have been given an access code, enter it here to join. If you have "
    "already joined, you can pick up where you left off."
)


def _enrolment_on_or_404():
    if not enrolment_service.enabled():
        raise Http404("Study enrolment is not enabled.")


def _client_ip(request) -> str:
    """Best-effort caller address for the attempt limiter.

    Behind a proxy the left-most X-Forwarded-For entry is the client. It is
    spoofable, which is worth knowing: the limiter raises the cost of guessing,
    it does not make it impossible. Documented as a limitation rather than
    presented as airtight.
    """
    fwd = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return fwd or request.META.get("REMOTE_ADDR") or ""


def _session_enrolment(request):
    """The enrolment this browser is partway through, or None."""
    pk = request.session.get(SESSION_KEY)
    if not pk:
        return None
    return (Enrolment.objects.select_related("study")
            .filter(pk=pk).exclude(status=Enrolment.Status.WITHDRAWN).first())


def enrolment_landing(request):
    _enrolment_on_or_404()
    return render(request, "enrolment/landing.html", {
        "landing_text": (get_setting("ENROLMENT_LANDING_TEXT") or "").strip()
                        or DEFAULT_LANDING_TEXT,
    })


def enrolment_claim(request):
    """Enter an access code, plus whatever the study needs alongside it."""
    _enrolment_on_or_404()

    # Rendered before the POST branch so a GET after a lockout says so too.
    locked = enrolment_service.too_many_attempts(_client_ip(request))

    ctx = {
        "require_dob": enrolment_service.require_dob(),
        # Asked whenever any open study needs one, not only the study behind the
        # code they are about to type — see enrolment.service.phone_required.
        "require_phone": enrolment_service.phone_required(),
        "locked": locked,
    }

    if request.method != "POST":
        return render(request, "enrolment/claim.html", ctx)

    code = request.POST.get("access_code", "")
    dob_raw = (request.POST.get("date_of_birth") or "").strip()
    phone = (request.POST.get("phone_number") or "").strip()

    dob = None
    if dob_raw:
        try:
            dob = _date.fromisoformat(dob_raw)
        except ValueError:
            messages.error(request, _("Please enter your date of birth as YYYY-MM-DD."))
            return render(request, "enrolment/claim.html", ctx)
        if dob > timezone.localdate():
            messages.error(request, _("That date of birth is in the future."))
            return render(request, "enrolment/claim.html", ctx)

    try:
        enrolment = enrolment_service.claim(
            code=code, date_of_birth=dob, phone_number=phone or None,
            ip=_client_ip(request),
        )
    except enrolment_service.ClaimError as exc:
        messages.error(request, str(exc))
        ctx["locked"] = enrolment_service.too_many_attempts(_client_ip(request))
        return render(request, "enrolment/claim.html", ctx)

    # Cycle the session key on a successful claim: this is the point the session
    # starts representing a specific person.
    request.session.cycle_key()
    request.session[SESSION_KEY] = enrolment.pk
    return redirect("enrolment_consent")


def enrolment_consent(request):
    """Show the study's consent wording and record the answer."""
    _enrolment_on_or_404()

    enrolment = _session_enrolment(request)
    if enrolment is None:
        return redirect("enrolment_landing")
    if enrolment.claimed_at is None:
        # Consent comes after the claim, never instead of it: the claim form is
        # where the date of birth and phone number a study needs are collected.
        return redirect("enrolment_claim")
    if enrolment.consent_is_current:
        # Already agreed to this version. Showing the form again would only
        # invite a second, identical record.
        return redirect("enrolment_done")

    study = enrolment.study
    items = study.consent_items or []

    def _for_template(answers):
        """Items with their tick state, so the template needs no dict lookups."""
        return [{**i, "checked": bool(answers.get(i["key"]))} for i in items]

    if request.method == "POST":
        answers = {i["key"]: bool(request.POST.get(f"item_{i['key']}")) for i in items}
        try:
            enrolment_service.record_consent(
                enrolment=enrolment,
                answers=answers,
                ip=_client_ip(request) or None,
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except enrolment_service.ClaimError as exc:
            messages.error(request, str(exc))
            return render(request, "enrolment/consent.html", {
                "enrolment": enrolment, "study": study,
                "items": _for_template(answers),
            })
        return redirect("enrolment_done")

    return render(request, "enrolment/consent.html", {
        "enrolment": enrolment,
        "study": study,
        "items": _for_template({}),
    })


def enrolment_done(request):
    """Confirmation, and the hand-off to whatever comes next."""
    _enrolment_on_or_404()

    enrolment = _session_enrolment(request)
    if enrolment is None:
        return redirect("enrolment_landing")

    latest = enrolment.latest_consent
    if latest is None:
        # Claimed but never consented: send them back rather than congratulate
        # them on something they did not do.
        return redirect("enrolment_consent")

    return render(request, "enrolment/done.html", {
        "enrolment": enrolment,
        "study": enrolment.study,
        "consent": latest,
        "awaiting_approval": enrolment.patient_id is None,
    })


def enrolment_resume(request):
    """Pick up an interrupted flow with the access code alone.

    No password anywhere in this flow, so the code is the only credential —
    which is exactly why the attempt limiter covers this path too.
    """
    _enrolment_on_or_404()

    if request.method != "POST":
        return render(request, "enrolment/resume.html", {
            "locked": enrolment_service.too_many_attempts(_client_ip(request)),
        })

    ip = _client_ip(request)
    if enrolment_service.too_many_attempts(ip):
        messages.error(request, _("Too many incorrect attempts. Please wait an hour."))
        return render(request, "enrolment/resume.html", {"locked": True})

    from ..enrolment.codes import normalise_code
    code = normalise_code(request.POST.get("access_code", ""))
    enrolment = (Enrolment.objects.select_related("study")
                 .filter(access_code=code)
                 .exclude(status=Enrolment.Status.WITHDRAWN)
                 .first()) if code else None

    if enrolment is None:
        enrolment_service.note_failed_attempt(ip)
        messages.error(request, enrolment_service.GENERIC_CODE_ERROR)
        return render(request, "enrolment/resume.html", {
            "locked": enrolment_service.too_many_attempts(ip),
        })

    enrolment_service.clear_attempts(ip)

    # Where they are in the flow decides where they land. An unclaimed code has
    # nothing to resume: it goes to the claim form *without* a session, so the
    # claim's questions cannot be skipped by resuming instead.
    if enrolment.claimed_at is None:
        return redirect("enrolment_claim")

    request.session.cycle_key()
    request.session[SESSION_KEY] = enrolment.pk
    if enrolment.latest_consent is None or not enrolment.consent_is_current:
        return redirect("enrolment_consent")
    return redirect("enrolment_done")
