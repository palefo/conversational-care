"""Outbound email: provider selection, sending, and the messages the platform
sends itself.

Two providers, picked at runtime by ``EMAIL_PROVIDER``:

``azure``
    Azure Communication Services Email, via the ``azure-communication-email``
    package. Credentials are either a whole connection string, or an endpoint
    and an access key that get assembled into one — the portal hands out both
    shapes, so both are accepted.

``smtp``
    Any SMTP server, delegated to Django's own SMTP backend with the host, port
    and credentials filled in from configuration rather than from settings.py.

Every value resolves through :mod:`ConvAI.site_config`, so an admin can change
provider or credentials in Settings → Email and the next send picks them up
with no restart, while ``.env`` stays the fallback.

Django's own mail goes down the same path: ``EMAIL_BACKEND`` points at
:class:`PlatformEmailBackend` below, so the password-reset flow — which calls
``django.core.mail`` deep inside ``django.contrib.auth`` — is carried by
whichever provider is configured without knowing anything about it.
"""
import logging
from email.utils import formataddr, parseaddr

from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .site_config import brand_name, get_setting

logger = logging.getLogger(__name__)

AZURE = "azure"
SMTP = "smtp"

# SMTP conversations happen inside a web request. Without a cap, one unreachable
# host holds a worker until the platform's own timeout kills the whole request.
SMTP_TIMEOUT = 20


class EmailConfigurationError(RuntimeError):
    """Email was asked for before it was configured well enough to send."""


def _cfg(key, default=""):
    return (get_setting(key, default) or "").strip()


def provider():
    """Which provider outbound mail goes through: ``azure``, ``smtp`` or ""."""
    return _cfg("EMAIL_PROVIDER").lower()


def azure_connection_string():
    """The ACS connection string, whole or assembled from endpoint + key.

    Returns "" when neither shape is configured.
    """
    conn = _cfg("AZURE_EMAIL_CONNECTION_STRING")
    if conn:
        return conn
    endpoint = _cfg("AZURE_EMAIL_ENDPOINT")
    access_key = _cfg("AZURE_EMAIL_ACCESS_KEY")
    if not (endpoint and access_key):
        return ""
    if not endpoint.endswith("/"):
        endpoint += "/"
    return f"endpoint={endpoint};accesskey={access_key}"


def from_address():
    """``(address, display name)`` the platform sends as."""
    return _cfg("EMAIL_FROM"), (_cfg("EMAIL_FROM_NAME") or brand_name() or "")


def from_header():
    """The From header: ``Name <address>``, or just the address."""
    address, name = from_address()
    if not address:
        return ""
    return formataddr((name, address)) if name else address


def reply_to_addresses():
    """Reply-To addresses, from a comma- or semicolon-separated setting."""
    raw = _cfg("EMAIL_REPLY_TO").replace(";", ",")
    return [a.strip() for a in raw.split(",") if a.strip()]


def status():
    """``(ok, reason)`` — whether a send would work with the current settings.

    The reason is written for an admin looking at the Settings page, so it names
    what is missing rather than reporting that something went wrong.
    """
    name = provider()
    if name not in (AZURE, SMTP):
        return False, _("No email provider is selected. Choose Azure or SMTP under Settings → Email.")
    if not from_address()[0]:
        return False, _("No sender address is configured (EMAIL_FROM).")

    if name == AZURE:
        if not azure_connection_string():
            return False, _(
                "Azure email needs either a connection string, or an endpoint and an access key."
            )
        try:
            import azure.communication.email  # noqa: F401
        except ImportError:
            return False, _(
                "The azure-communication-email package is not installed on this server."
            )
    elif not _cfg("SMTP_HOST"):
        return False, _("SMTP needs a host name (SMTP_HOST).")

    return True, ""


def is_configured():
    """True when mail can be sent. Convenience for templates and guards."""
    return status()[0]


# ── Message → provider payload ──────────────────────────────────────────────

def _bodies(message):
    """``(plain text, html)`` for one Django ``EmailMessage``.

    Django keeps the primary body in ``body`` and any richer version in
    ``alternatives``; which of the two is the HTML depends on
    ``content_subtype``. Providers want them as two named fields, so they are
    separated here rather than at each call site.
    """
    text = html = ""
    if getattr(message, "content_subtype", "plain") == "html":
        html = message.body or ""
    else:
        text = message.body or ""
    for alternative in getattr(message, "alternatives", None) or []:
        # Django 5.2 makes these named tuples; 5.1 uses plain ones. Both unpack.
        content, mimetype = alternative[0], alternative[1]
        if mimetype == "text/html":
            html = content
    return text, html


def _recipients(addresses):
    """Django address strings → the ``{"address", "displayName"}`` dicts ACS wants."""
    out = []
    for raw in addresses or []:
        name, address = parseaddr(raw)
        if not address:
            continue
        entry = {"address": address}
        if name:
            entry["displayName"] = name
        out.append(entry)
    return out


def _azure_payload(message):
    """One Django ``EmailMessage`` as an Azure Communication Services message."""
    text, html = _bodies(message)
    content = {"subject": message.subject or ""}
    if text:
        content["plainText"] = text
    if html:
        content["html"] = html

    recipients = {}
    for key, addresses in (
        ("to", message.to), ("cc", message.cc), ("bcc", message.bcc),
    ):
        entries = _recipients(addresses)
        if entries:
            recipients[key] = entries

    # ACS takes a bare sender address — the display name comes from the sender
    # username configured on the domain in the Azure portal, not from here.
    _, sender = parseaddr(message.from_email or "")
    payload = {
        "senderAddress": sender,
        "recipients": recipients,
        "content": content,
    }
    reply_to = _recipients(getattr(message, "reply_to", None))
    if reply_to:
        payload["replyTo"] = reply_to
    return payload


# ── The backend ─────────────────────────────────────────────────────────────

class PlatformEmailBackend(BaseEmailBackend):
    """Django email backend that routes to whichever provider is configured.

    Being a backend rather than a helper function is what lets Django's own
    password-reset mail use the same providers as the platform's: everything
    that calls ``django.core.mail`` arrives here.
    """

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        ok, reason = status()
        if not ok:
            if self.fail_silently:
                logger.warning("Email not sent: %s", reason)
                return 0
            raise EmailConfigurationError(str(reason))

        for message in email_messages:
            self._apply_defaults(message)

        if provider() == AZURE:
            return self._send_azure(email_messages)
        return self._send_smtp(email_messages)

    def _apply_defaults(self, message):
        """Stamp the configured sender and Reply-To onto a message.

        Django fills ``from_email`` from ``DEFAULT_FROM_EMAIL``, which is read
        once at boot; the configured address may have changed since. Anything a
        caller set deliberately is left alone.
        """
        from django.conf import settings

        if not message.from_email or message.from_email == settings.DEFAULT_FROM_EMAIL:
            message.from_email = from_header() or message.from_email
        if not getattr(message, "reply_to", None):
            message.reply_to = reply_to_addresses()

    def _send_smtp(self, messages):
        security = _cfg("SMTP_SECURITY").lower() or "tls"
        port = _cfg("SMTP_PORT")
        connection = get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=_cfg("SMTP_HOST"),
            port=int(port) if port.isdigit() else (465 if security == "ssl" else 587),
            username=_cfg("SMTP_USER") or None,
            password=_cfg("SMTP_PASSWORD") or None,
            use_tls=security == "tls",
            use_ssl=security == "ssl",
            timeout=SMTP_TIMEOUT,
            fail_silently=self.fail_silently,
        )
        return connection.send_messages(messages) or 0

    def _send_azure(self, messages):
        # Imported here rather than at module scope: the package is only needed
        # by deployments that chose Azure, and the module is imported by
        # settings-time code paths that must not depend on it.
        from azure.communication.email import EmailClient

        client = EmailClient.from_connection_string(azure_connection_string())
        sent = 0
        for message in messages:
            try:
                # begin_send returns a poller; ACS accepts the message first and
                # reports delivery status as the operation completes.
                result = client.begin_send(_azure_payload(message)).result()
            except Exception as exc:
                logger.warning("Azure email send failed: %s", exc)
                if not self.fail_silently:
                    raise
                continue

            state = str((result or {}).get("status", "")).lower()
            if state == "failed":
                detail = (result or {}).get("error") or result
                logger.warning("Azure rejected the message: %s", detail)
                if not self.fail_silently:
                    raise RuntimeError(f"Azure rejected the message: {detail}")
                continue
            sent += 1
        return sent


# ── Sending ─────────────────────────────────────────────────────────────────

def send_email(subject, to, *, html="", text="", reply_to=None, fail_silently=False):
    """Send one message through the configured provider.

    ``to`` may be a single address or a list. Returns True when the provider
    accepted it. Raises :class:`EmailConfigurationError` — or whatever the
    provider raised — unless ``fail_silently``.
    """
    recipients = [to] if isinstance(to, str) else list(to)
    recipients = [a.strip() for a in recipients if a and a.strip()]
    if not recipients:
        if fail_silently:
            return False
        raise ValueError("No recipient address was given.")

    message = EmailMultiAlternatives(
        subject=subject,
        body=text or _html_to_text(html),
        to=recipients,
        reply_to=list(reply_to) if reply_to else None,
    )
    if html:
        message.attach_alternative(html, "text/html")
    return bool(message.send(fail_silently=fail_silently))


def _html_to_text(html):
    """A rough plain-text fallback, so an HTML-only message still has a body.

    Not a renderer — it exists so that mail clients refusing HTML, and spam
    filters counting a missing text part against the sender, both get something
    readable. Templates that care ship their own .txt.
    """
    import html as html_module
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html_module.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_email(template_base, context):
    """Render ``email/<base>.txt`` and ``email/<base>.html`` with shared context.

    Every platform message gets the brand name and the current year without
    each template having to ask for them.
    """
    full = {"brand_name": brand_name(), "year": timezone.localtime().year, **context}
    return (
        render_to_string(f"email/{template_base}.txt", full),
        render_to_string(f"email/{template_base}.html", full),
    )


def send_test_email(to):
    """Send the Settings page's "does email work?" message.

    Returns ``(ok, detail)``: on failure the detail is the provider's own words,
    which is the only thing that tells an admin what to fix.
    """
    ok, reason = status()
    if not ok:
        return False, str(reason)

    text, html = render_email("test_message", {
        "provider": provider(),
        "sent_at": timezone.localtime(),
        "sender": from_address()[0],
    })
    try:
        send_email(
            subject=_("%(brand)s test email") % {"brand": brand_name() or "Platform"},
            to=to,
            text=text,
            html=html,
        )
    except Exception as exc:
        logger.warning("Test email to %s failed: %s", to, exc)
        return False, str(exc)
    return True, ""


# ── Meeting reminders ───────────────────────────────────────────────────────

def reminder_channel():
    """Which channel meeting reminders go out on: ``whatsapp`` or ``email``."""
    channel = (get_setting("REMINDER_CHANNEL", "whatsapp") or "whatsapp").strip().lower()
    return channel if channel in ("whatsapp", "email") else "whatsapp"


def meeting_reminder_recipient(meeting):
    """``(address, display name)`` for a meeting's email reminder.

    The caregiver first, because they are who the call is arranged with; the
    client second. ``("", "")`` when neither has an address on file.
    """
    patient = meeting.patient
    for person in (getattr(patient, "caregiver", None), patient):
        address = (getattr(person, "email", "") or "").strip()
        if address:
            return address, str(person)
    return "", ""


def send_meeting_reminder_email(meeting):
    """Email the caregiver (or client) about an upcoming meeting.

    Raises ``ValueError`` when there is nobody to send to, matching how the
    WhatsApp reminder reports a missing phone number, so the view can tell the
    two failures apart from a provider outage.
    """
    address, display_name = meeting_reminder_recipient(meeting)
    if not address:
        raise ValueError("Neither the caregiver nor the client has an email address.")

    local_time = timezone.localtime(meeting.scheduled_time)
    in_person = meeting.modality == meeting.Modality.IN_PERSON
    text, html = render_email("meeting_reminder", {
        "patient": meeting.patient,
        "recipient_name": display_name,
        "date": local_time.strftime("%d/%m/%Y"),
        "time": local_time.strftime("%H:%M"),
        "in_person": in_person,
        "location": meeting.location if in_person else "",
    })
    subject = _("Reminder: %(kind)s on %(date)s at %(time)s") % {
        "kind": _("meeting") if in_person else _("call"),
        "date": local_time.strftime("%d/%m/%Y"),
        "time": local_time.strftime("%H:%M"),
    }
    send_email(subject=subject, to=[formataddr((display_name, address))],
               text=text, html=html)

    # Same record the WhatsApp reminder leaves, so a reminder shows up in the
    # message history whichever channel carried it.
    from .models import Message
    Message.objects.create(
        conversation_id=f"reminder-{meeting.id}",
        user=address,
        user_message="",
        response_message=text,
    )
    return address
