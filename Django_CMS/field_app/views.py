"""Connecting accounts, linking clients, managing installations."""
from __future__ import annotations

import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from ConvAI.models import Patient
from ConvAI.roles import admin_required, is_admin, navigator_required

from .addon import active_installations, connection_for, mark_broken
from .client import Api, FieldAppError, authorize_url, exchange_code
from .models import FieldAppConnection, FieldAppInstallation, PatientFieldAppLink
from .settings_tab import callback_url

STATE_KEY = "field_app_connect"


def _safe_next(request, default):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


# ── Connecting a user's Field App account ───────────────────────────────────

@login_required
@navigator_required
@require_POST
def connect_start(request, pk):
    """Send the user to Field App's consent screen."""
    installation = get_object_or_404(FieldAppInstallation, pk=pk, is_active=True)
    state = secrets.token_urlsafe(24)
    request.session[STATE_KEY] = {
        "state": state, "installation": installation.pk,
        "next": _safe_next(request, reverse("profile")),
    }
    return redirect(authorize_url(installation, callback_url(request), state))


@login_required
@navigator_required
def callback(request):
    """Field App sends the user back here with ?code=…&state=… (or ?error=…)."""
    pending = request.session.pop(STATE_KEY, None) or {}
    state = request.GET.get("state", "")
    if not pending or not state or not secrets.compare_digest(state, pending.get("state", "")):
        messages.error(request, _("That Field App connection could not be verified. Please try again."))
        return redirect("profile")
    nxt = pending.get("next") or reverse("profile")
    if request.GET.get("error"):
        messages.info(request, _("Field App was not connected."))
        return redirect(nxt)

    installation = get_object_or_404(FieldAppInstallation, pk=pending["installation"], is_active=True)
    try:
        grant = exchange_code(installation, request.GET.get("code", ""), callback_url(request))
    except FieldAppError as exc:
        messages.error(request, _("Field App refused the connection (%(err)s).") % {"err": exc.detail})
        return redirect(nxt)

    user = grant.get("user") or {}
    FieldAppConnection.objects.update_or_create(
        installation=installation, user=request.user,
        defaults={"token": grant["access_token"], "remote_user_id": user.get("id"),
                  "remote_username": user.get("username", ""), "broken_at": None},
    )
    messages.success(request, _("Connected to %(app)s as %(user)s.") % {
        "app": installation.name, "user": user.get("username", "?")})
    return redirect(nxt)


@login_required
@require_POST
def disconnect(request, pk):
    conn = get_object_or_404(FieldAppConnection, installation_id=pk, user=request.user)
    Api(conn.installation, conn.token).revoke()
    conn.delete()
    messages.success(request, _("Field App disconnected."))
    return redirect(_safe_next(request, reverse("profile")))


# ── Linking a client to their Field App athlete ─────────────────────────────

def _can_see(user, patient):
    return is_admin(user) or patient.navigator_id == user.id


def client_card_context(request, patient):
    """The Field App card on the client page. None when nothing is installed."""
    installations = list(active_installations())
    if not installations:
        return None
    q = (request.GET.get("fa_q") or "").strip()
    dob = (request.GET.get("fa_dob") or "").strip()
    rows = []
    for inst in installations:
        conn = connection_for(request.user, inst)
        row = {
            "installation": inst,
            "connection": conn,
            "link": PatientFieldAppLink.objects.filter(patient=patient, installation=inst)
                     .select_related("linked_by").first(),
            "results": None, "error": "", "preview": "",
            "default_q": f"{patient.name} {patient.lastname}".strip(),
        }
        usable = conn and not conn.broken_at
        if usable and request.GET.get("fa_inst") == str(inst.pk) and (q or dob):
            try:
                row["results"] = Api(inst, conn.token).search(q, dob)
            except FieldAppError as exc:
                if exc.unauthorized:
                    mark_broken(conn)
                row["error"] = _("Field App could not be searched: %(err)s") % {"err": exc.detail}
        if usable and row["link"] and request.GET.get("fa_preview") == str(inst.pk):
            try:
                row["preview"] = Api(inst, conn.token).context_markdown(row["link"].athlete_id)
            except FieldAppError as exc:
                row["error"] = _("Could not load the record: %(err)s") % {"err": exc.detail}
        rows.append(row)
    return {"rows": rows, "patient": patient, "q": q, "dob": dob}


@login_required
@navigator_required
@require_POST
def link(request, patient_pk, pk):
    patient = get_object_or_404(Patient, pk=patient_pk)
    if not _can_see(request.user, patient):
        return HttpResponseForbidden()
    installation = get_object_or_404(FieldAppInstallation, pk=pk, is_active=True)
    conn = connection_for(request.user, installation)
    back = reverse("patient_detail", args=[patient.pk]) + "#field-app"
    if not conn:
        messages.error(request, _("Connect your Field App account first (Profile → Field App)."))
        return redirect(back)
    # Read the athlete through this user's own token: you can only link someone
    # you can see in Field App, and the name stored is Field App's, not the form's.
    try:
        athlete = Api(installation, conn.token).athlete(int(request.POST.get("athlete_id", 0)))
    except (ValueError, FieldAppError):
        messages.error(request, _("That Field App athlete could not be found."))
        return redirect(back)
    PatientFieldAppLink.objects.update_or_create(
        patient=patient, installation=installation,
        defaults={"athlete_id": athlete["id"],
                  "athlete_name": f"{athlete.get('name', '')} {athlete.get('lastname', '')}".strip(),
                  "linked_by": request.user},
    )
    messages.success(request, _("Linked to Field App."))
    return redirect(back)


@login_required
@navigator_required
@require_POST
def unlink(request, patient_pk, pk):
    patient = get_object_or_404(Patient, pk=patient_pk)
    if not _can_see(request.user, patient):
        return HttpResponseForbidden()
    PatientFieldAppLink.objects.filter(patient=patient, installation_id=pk).delete()
    messages.success(request, _("Unlinked from Field App."))
    return redirect(reverse("patient_detail", args=[patient.pk]) + "#field-app")


# ── Settings → Third-party apps ─────────────────────────────────────────────

SETTINGS_TAB = "?tab=thirdparty#thirdparty"


@login_required
@admin_required
@require_POST
def installation_toggle(request, pk):
    inst = get_object_or_404(FieldAppInstallation, pk=pk)
    inst.is_active = not inst.is_active
    inst.save(update_fields=["is_active"])
    return redirect(reverse("config") + SETTINGS_TAB)


@login_required
@admin_required
@require_POST
def installation_delete(request, pk):
    get_object_or_404(FieldAppInstallation, pk=pk).delete()
    messages.success(request, _("Field App installation removed. Its connections and client links are gone too."))
    return redirect(reverse("config") + SETTINGS_TAB)
