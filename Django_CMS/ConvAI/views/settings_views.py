from ._base import *  # noqa: F401,F403
from .. import message_export
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

__all__ = ['_build_config_context', 'approve_self_registration', 'config', 'config_save', 'download_client_sdk', 'run_conversation_classification', 'send_test_email_view', 'update_twilio_phonecalls']


def download_client_sdk(request):
    """Serve the Python API client package as a zip.

    Intentionally unauthenticated so it can be fetched with ``wget`` (the
    contents are just open client code — no secrets). Zips the ``client_sdk``
    project tree so the archive root has ``pyproject.toml`` and the package,
    making ``pip install ./conversationalcare_api.zip`` work directly.
    """
    import io
    import zipfile
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "client_sdk"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_dir() or path.suffix == ".pyc" or "__pycache__" in path.parts:
                continue
            zf.write(path, path.relative_to(root).as_posix())

    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="conversationalcare_api.zip"'
    return resp


@login_required
@require_POST
def update_twilio_phonecalls(request):
        get_recordings_from_twilio()
        messages.success(request, _("Saved"))
        return redirect('config')


def _qr_svg(data: str) -> str:
    """Render ``data`` as a compact, dependency-light QR SVG (run-length rects).

    Scalable and CSP-safe (no external JS): the browser can also rasterise it to
    PNG on the client for download / clipboard. Returns "" on any failure.
    """
    try:
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            border=2,
            box_size=1,
        )
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
    except Exception:
        return ""

    n = len(matrix)
    rects = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if row[x]:
                run = x
                while run < n and row[run]:
                    run += 1
                rects.append(f'<rect x="{x}" y="{y}" width="{run - x}" height="1"/>')
                x = run
            else:
                x += 1
    body = "".join(rects)
    # Explicit width/height (attributes) give the SVG an intrinsic size so it can
    # be rasterised to a canvas; CSS in the template controls the displayed size.
    return (
        f'<svg id="srQrSvg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
        f'width="{n}" height="{n}" shape-rendering="crispEdges" '
        f'style="width:100%;height:auto;display:block">'
        f'<rect width="{n}" height="{n}" fill="#ffffff"/>'
        f'<g fill="#0f172a">{body}</g></svg>'
    )


def _selfreg_qr_context(request):
    """Build the WhatsApp "join" QR context for the Self-registration tab.

    The QR encodes a ``wa.me`` deep link that opens WhatsApp to the platform
    number with a pre-filled message in the platform language.
    """
    from urllib.parse import quote

    raw_phone = (get_setting("PLATFORM_PHONE") or "").strip()
    digits = re.sub(r"\D", "", raw_phone)
    message = str(_("Hi! I want to register"))

    ctx = {
        "enabled": get_bool("SELF_REGISTRATION_ENABLED"),
        "agent_name": (get_setting("SELF_REG_AGENT_NAME") or "").strip(),
        "phone": raw_phone,
        "message": message,
        "url": "",
        "qr_svg": "",
    }
    if digits:
        ctx["url"] = f"https://wa.me/{digits}?text={quote(message)}"
        ctx["qr_svg"] = _qr_svg(ctx["url"])
    return ctx


def _email_status_context(request):
    """Whether email is ready to send, for the banner on the Email tab.

    Reported from the same check the backend runs before every send, so the
    page cannot claim email works while sends are failing for a missing value.
    """
    from ..mailer import provider, reminder_channel, status

    ok, reason = status()
    return {
        "ok": ok,
        "reason": reason,
        "provider": provider(),
        "reminder_channel": reminder_channel(),
        # Pre-filled into the test-email box: the address the admin can check.
        "test_to": request.user.email or "",
    }


def _sensei_status_context():
    """Whether Sensei is on and fully configured, for the banner on its tab."""
    from .. import sensei

    ok, reason = sensei.configured()
    return {
        "enabled": sensei.enabled(),
        "ok": ok,
        "reason": reason,
        "api_url": sensei.api_url(),
    }


def _build_config_context(request, forms_override=None, active_tab="general"):
    """Assemble the settings-page context, letting one bound (invalid) form be
    injected so validation errors render inline."""
    forms_override = forms_override or {}
    cfg = SiteConfiguration.load()

    protocols = []
    locked_numbers = set(
        Answer.objects.values_list("question__protocol__number", flat=True).distinct()
    )
    for p in Protocol.objects.annotate(num_questions=Count("questions")):
        protocols.append({
            "number": p.number,
            "title": p.title,
            "num_questions": p.num_questions,
            "locked": p.number in locked_numbers,
        })

    def _form(name):
        return forms_override.get(name) or _CONFIG_FORMS[name](instance=cfg)

    return {
        "active_page": "admin",
        "active_tab": active_tab,
        # (id, label, material-icon) for the settings sidebar.
        "tabs": [
            ("general", _("General"), "tune"),
            ("integrations", _("Integrations"), "key"),
            ("messaging", _("Messaging"), "forum"),
            ("email", _("Email"), "mail"),
            ("branding", _("Branding"), "palette"),
            ("agents", _("Agents"), "smart_toy"),
            ("sensei", _("Sensei"), "sensors"),
            ("prompts", _("Prompts"), "auto_awesome"),
            ("protocols", _("Protocols"), "checklist"),
            ("registrations", _("Registrations"), "how_to_reg"),
            ("selfreg", _("Self registration"), "qr_code_2"),
            ("api", _("API client"), "terminal"),
            ("export", _("Export"), "download"),
            ("maintenance", _("Maintenance"), "build"),
        ],
        # Absolute URLs for the API-client instructions (wget / base_url).
        "client_download_url": request.build_absolute_uri(reverse("download_client_sdk")),
        "api_base_url": request.build_absolute_uri("/").rstrip("/"),
        # Whether the REST API is exposed (boot setting; the client needs it on).
        "api_enabled": getattr(settings, "ENABLE_API", True),
        "self_regs": SelfRegistration.objects.order_by("-created_at"),
        "selfreg": _selfreg_qr_context(request),
        "agents": Agent.objects.all().order_by("name"),
        "protocols": protocols,
        "general_form": _form("general"),
        "integrations_form": _form("integrations"),
        "messaging_form": _form("messaging"),
        "email_form": _form("email"),
        "branding_form": _form("branding"),
        # Whether mail could go out right now, and if not, what is missing.
        "email_status": _email_status_context(request),
        "agents_form": _form("agents"),
        "prompts_form": _form("prompts"),
        "sensei_form": _form("sensei"),
        # Whether a Sensei turn could succeed right now, reported from the same
        # check the adapter runs — so the tab cannot say "ready" while every
        # turn is failing on a missing value.
        "sensei_status": _sensei_status_context(),
        "export_form": _form("export"),
        "export_enabled": message_export.enabled(),
        "export_columns": message_export.COLUMN_NOTES,
        # Boot-only values shown read-only (require .env change + restart).
        "boot_info": {
            "language": settings.LANGUAGE_CODE,
            "timezone": str(settings.TIME_ZONE),
            "async_whatsapp_reply": getattr(settings, "ASYNC_WHATSAPP_REPLY", False),
            "debug": settings.DEBUG,
            "webhook_url": request.build_absolute_uri(
                "/" + getattr(settings, "TWILIO_WEBHOOK_PATH", "webhooks/whatsapp")
            ),
        },
    }


@login_required
@admin_required
def config(request):
    active_tab = request.GET.get("tab", "general")
    return render(request, "settings/config.html", _build_config_context(request, active_tab=active_tab))


@login_required
@admin_required
def config_save(request):
    if request.method != "POST":
        return redirect("config")
    section = request.POST.get("section", "general")
    form_cls = _CONFIG_FORMS.get(section)
    if not form_cls:
        return redirect("config")

    form = form_cls(request.POST, request.FILES, instance=SiteConfiguration.load())
    if form.is_valid():
        form.save()
        messages.success(request, _("Settings saved."))
        return redirect(f"{reverse('config')}?tab={section}#{section}")

    messages.error(request, _("Please correct the errors below."))
    ctx = _build_config_context(request, forms_override={section: form}, active_tab=section)
    return render(request, "settings/config.html", ctx)


@login_required
@admin_required
@require_POST
def send_test_email_view(request):
    """Send one throwaway message to prove the email configuration works.

    Configuration you cannot try is configuration you find out about when a
    password reset silently fails, so the provider's own error is put in front
    of the admin rather than logged.
    """
    from ..mailer import send_test_email

    to = (request.POST.get("to") or "").strip() or (request.user.email or "").strip()
    back = f"{reverse('config')}?tab=email#email"

    if not to:
        messages.error(request, _("Enter an address to send the test to."))
        return redirect(back)

    try:
        validate_email(to)
    except ValidationError:
        messages.error(request, _("“%(address)s” is not a valid email address.") % {"address": to})
        return redirect(back)

    ok, detail = send_test_email(to)
    if ok:
        messages.success(request, _("Test email sent to %(address)s.") % {"address": to})
    else:
        messages.error(request, _("The test email could not be sent: %(detail)s") % {"detail": detail})
    return redirect(back)


@login_required
@admin_required
@require_POST
def run_conversation_classification(request):
    """
    Classify:
      • conversations never analyzed, OR
      • conversations whose last_message_at is newer than analyzed_at.

    Inbound messages are classified as they arrive, so this is now a backstop
    rather than the only way it ever happens: it catches conversations that
    predate the hook, and anything the ingest pool dropped while the model or
    the process was down. It goes through the same
    ``review_conversation`` the hook does, so a re-run cannot reach a
    different verdict than a live message would have.

    Configuration action — admins only.
    """
    from ..conversation_alerts import review_conversation

    to_analyze = (
        Conversation.objects
        .filter(Q(analyzed=False) | Q(analyzed_at__isnull=True) | Q(last_message_at__gt=F("analyzed_at")))
        .order_by("started_at")
        .values_list("id", flat=True)
    )

    processed = 0
    raised = 0
    for conv_id in list(to_analyze):
        outcome = review_conversation(conv_id)
        if outcome["analyzed"]:
            processed += 1
        raised += len(outcome["alerts"])

    if raised:
        messages.success(request, _(
            "Classification completed. Conversations processed: %(n)s. Alerts raised: %(a)s."
        ) % {"n": processed, "a": raised})
    else:
        messages.success(request, _(
            "Classification completed. Conversations processed: %(n)s."
        ) % {"n": processed})
    return redirect("config")


@admin_required
@login_required
@require_POST
def approve_self_registration(request, pk):
    sr = get_object_or_404(SelfRegistration, pk=pk)

    if sr.state == SelfRegistration.State.APPROVED:
        messages.info(request, _("This self-registration is already approved."))
        return redirect('config')

    # Optional Agent selection
    agent_id = request.POST.get("agent_id") or ""
    agent = Agent.objects.filter(pk=agent_id).first() if agent_id else None

    caregiver = Caregiver.objects.create(
        name=sr.name,
        lastname=sr.lastname,
        phone_number=sr.phone_number
    )

    Patient.objects.create(
        name=sr.name,
        lastname=sr.lastname,
        phone_number=sr.phone_number,
        caregiver=caregiver,
        navigator=None,
        agent=agent,  # attach selected agent if any
    )

    sr.state = SelfRegistration.State.APPROVED
    sr.save(update_fields=["state", "updated_at"])

    # Send WhatsApp welcome using the util
    sent = False
    if sr.phone_number:
        sent = send_whatsapp_text(
            to_e164=str(sr.phone_number),
            body=(
                "Welcome to conversational-care.ai! Your registration has been approved."
            )
        )

    if sent:
        messages.success(
            request,
            f"A patient for {sr.name} {sr.lastname} was created, request approved, and a WhatsApp welcome was sent."
        )
    else:
        messages.success(
            request,
            f"A patient for {sr.name} {sr.lastname} was created and the request approved. "
            "No notification was sent due to an error."
        )

    return redirect('config')


