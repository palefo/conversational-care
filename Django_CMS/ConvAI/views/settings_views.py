from ._base import *  # noqa: F401,F403

__all__ = ['_build_config_context', 'approve_self_registration', 'config', 'config_save', 'download_client_sdk', 'run_conversation_classification', 'update_twilio_phonecalls']


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
            ("branding", _("Branding"), "palette"),
            ("agents", _("Agents"), "smart_toy"),
            ("prompts", _("Prompts"), "auto_awesome"),
            ("protocols", _("Protocols"), "checklist"),
            ("registrations", _("Registrations"), "how_to_reg"),
            ("selfreg", _("Self registration"), "qr_code_2"),
            ("api", _("API client"), "terminal"),
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
        "branding_form": _form("branding"),
        "agents_form": _form("agents"),
        "prompts_form": _form("prompts"),
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
def run_conversation_classification(request):
    """
    Classify:
      • conversations never analyzed, OR
      • conversations whose last_message_at is newer than analyzed_at.
    Prefer the Agent stored on Conversation; fall back to patient->agent if missing.
    Configuration action — admins only.
    """
    to_analyze = (
        Conversation.objects
        .filter(Q(analyzed=False) | Q(analyzed_at__isnull=True) | Q(last_message_at__gt=F("analyzed_at")))
        .order_by("started_at")
    )

    processed = 0
    for conv in to_analyze.iterator(chunk_size=100):
        conv_id_str = str(conv.id)
        rows = build_message_rows_for_conv(conv_id_str, Message)
        if not rows:
            conv.analyzed = True
            conv.analyzed_at = timezone.now()
            conv.save(update_fields=["analyzed", "analyzed_at"])
            continue

        # 1) Primary: agent from Conversation
        agent = getattr(conv, "agent", None)

        # 2) Fallback: infer from the first message → Patient.agent
        if agent is None:
            first_msg = (
                Message.objects
                .filter(conversation_id=conv_id_str)
                .order_by("timestamp")
                .first()
            )
            if first_msg:
                ms_user = (first_msg.user or "").strip()
                p = (
                    Patient.objects
                    .filter(Q(phone_number=ms_user) | Q(caregiver__phone_number=ms_user))
                    .select_related("agent")
                    .first()
                )
                agent = getattr(p, "agent", None) if p else None

        try:
            result = classify_conversation_with_llm(rows, agent=agent)
            conv.summary      = (result.get("abstract") or "")[:2000]
            conv.topic        = (result.get("classification") or "")[:120]
            conv.is_important = bool(result.get("important"))
            auto_flags        = result.get("detectors") or {}
            conv.auto_flags   = auto_flags if isinstance(auto_flags, dict) else {}
            conv.analyzed     = True
            conv.analyzed_at  = timezone.now()
            conv.visited      = False

            conv.save(update_fields=[
                "summary", "topic", "is_important", "auto_flags", "analyzed", "analyzed_at", "visited"
            ])
            processed += 1
        except Exception:
            # Skip this conversation on model/API errors
            continue



    messages.success(request, _("Classification completed. Conversations processed: %(n)s.") % {"n": processed})
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


