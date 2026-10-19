"""The web app chat: a client's conversation as an installable web page.

For clients with no WhatsApp — or tablets with no SIM card. Staff show a QR code
from the client page; the device that scans it is let into that one client's
chat and nothing else. See web_app_chat.md.

How a device gets in, and why it is shaped this way:

* **The QR carries a link, not a login.** ``/c/join/<public_id>/<token>``,
  the token an HMAC of the public id and the link's ``nonce`` — the same scheme
  as an online-meeting invite (meetings/links.py). Nothing secret is stored, so
  the QR can be shown again; rotating the nonce kills every printed copy.
* **The link is exchanged for a device key.** Joining is a POST from a page that
  names the client, so a link previewer cannot join by fetching it, and the
  person holding the tablet can see whose chat it is before they go in. The
  device then holds a random key in a cookie scoped to ``/c/``; only its
  SHA-256 is stored.
* **A device is live while it is not revoked and was joined through the link's
  current nonce.** That is what lets "Reset link" sign every tablet out at once
  without a sweep over the devices.
* **The cookie is not a Django login.** It opens the /c/ routes for one client
  and nothing else in the platform; a tablet that also has a staff session open
  gains nothing from it, and loses nothing.

Off by default (``WEB_CHAT_ENABLED``). While off, every /c/ route 404s, the
client page shows nothing, and outbound messages go to the phone as before —
even for a client whose channel says "web". Nothing is deleted; devices come
back when it is switched on again.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .site_config import get_bool

COOKIE_NAME = "cc_webchat"
COOKIE_PATH = "/c/"
# A year: a tablet joined for a programme should not quietly lose its chat
# half-way through. Ending it is a deliberate act — Unlink, Sign out, Reset.
COOKIE_MAX_AGE = 365 * 24 * 3600

# last_seen_at is a convenience for the device list, not an audit trail; writing
# it on every poll would be a write every few seconds per open tablet.
_SEEN_EVERY = timedelta(minutes=2)

_SALT = "conversational-care.webchat.link.v1"


def enabled() -> bool:
    """Whether this installation offers the web app chat. Off by default."""
    return get_bool("WEB_CHAT_ENABLED", False)


def delivers_to_web(patient) -> bool:
    """Should messages the platform starts go to this client's web app chat?

    Only when the feature is on *and* a person chose it on the client page. Off,
    a client marked "web" is reached on the phone exactly as before.
    """
    return bool(patient is not None and enabled()
                and getattr(patient, "chat_channel", "") == "web")


# --- The link the QR code carries -------------------------------------------

def _key() -> bytes:
    return salted_hmac(_SALT, "link-key", secret=settings.SECRET_KEY,
                       algorithm="sha256").digest()


def token_for(link) -> str:
    mac = hmac.new(_key(), f"{link.public_id}:{link.nonce}".encode(),
                   hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def link_for(patient):
    """This client's link, made the first time anybody asks for it."""
    from .models import WebChatLink

    link, _ = WebChatLink.objects.get_or_create(patient=patient)
    return link


def join_path(link) -> str:
    from django.urls import reverse

    return reverse("webchat_join", args=[link.public_id, token_for(link)])


def resolve_link(public_id: str, token: str):
    """The link a scanned URL names, or None when it is not a current one."""
    from .models import WebChatLink

    if not public_id or not token or len(public_id) > 24 or len(token) > 64:
        return None
    link = (WebChatLink.objects.select_related("patient")
            .filter(public_id=public_id).first())
    if link is None or not hmac.compare_digest(token_for(link), token):
        return None
    return link


def reset_link(link, *, by=None) -> None:
    """Issue a new QR code. The old one, and every device it let in, stop."""
    from .models import WebChatDevice

    now = timezone.now()
    WebChatDevice.objects.filter(
        patient=link.patient, revoked_at__isnull=True,
    ).update(revoked_at=now, revoked_reason=WebChatDevice.RevokedBy.RESET,
             revoked_by=by)
    link.nonce = secrets.token_urlsafe(18)
    link.rotated_at = now
    link.rotated_by = by
    link.save(update_fields=["nonce", "rotated_at", "rotated_by"])


# --- Devices ----------------------------------------------------------------

def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def device_label(user_agent: str) -> str:
    """'Android · Chrome' — enough to tell two tablets apart, nothing more."""
    ua = user_agent or ""
    if "Android" in ua:
        os_name = "Android"
    elif "iPad" in ua:
        os_name = "iPad"
    elif "iPhone" in ua:
        os_name = "iPhone"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "Mac"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = ""
    if "SamsungBrowser" in ua:
        browser = "Samsung Internet"
    elif "Edg/" in ua:
        browser = "Edge"
    elif "Firefox/" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = ""
    return " · ".join(p for p in (os_name, browser) if p)[:80]


def join(link, request):
    """Let this browser into the link's chat. Returns (device, key for the cookie)."""
    from .models import WebChatDevice

    key = secrets.token_urlsafe(32)
    device = WebChatDevice.objects.create(
        patient=link.patient,
        key_hash=_hash(key),
        link_nonce=link.nonce,
        label=device_label(request.META.get("HTTP_USER_AGENT", "")),
    )
    return device, key


def set_cookie(response, key: str, request) -> None:
    response.set_cookie(
        COOKIE_NAME, key, max_age=COOKIE_MAX_AGE, path=COOKIE_PATH,
        secure=request.is_secure(), httponly=True, samesite="Lax",
    )


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, samesite="Lax")


def current_device(request):
    """The live device this request comes from, or None.

    Live means: the feature is on, the device was not revoked, and the client's
    link has not been reset since it joined.
    """
    from .models import WebChatDevice

    if not enabled():
        return None
    key = request.COOKIES.get(COOKIE_NAME, "")
    if not key or len(key) > 128:
        return None
    device = (WebChatDevice.objects
              .select_related("patient", "patient__agent", "patient__web_chat_link")
              .filter(key_hash=_hash(key), revoked_at__isnull=True)
              .first())
    if device is None:
        return None
    link = getattr(device.patient, "web_chat_link", None)
    if link is None or link.nonce != device.link_nonce:
        return None
    now = timezone.now()
    if now - device.last_seen_at >= _SEEN_EVERY:
        WebChatDevice.objects.filter(pk=device.pk).update(last_seen_at=now)
        device.last_seen_at = now
    return device


def live_devices(patient):
    """The devices currently able to open this client's chat, newest first."""
    from .models import WebChatDevice

    link = getattr(patient, "web_chat_link", None)
    if link is None:
        return WebChatDevice.objects.none()
    return WebChatDevice.objects.filter(
        patient=patient, revoked_at__isnull=True, link_nonce=link.nonce)


def revoke(device, *, reason, by=None) -> None:
    device.revoked_at = timezone.now()
    device.revoked_reason = reason
    device.revoked_by = by
    device.save(update_fields=["revoked_at", "revoked_reason", "revoked_by"])


# --- What the chat shows ----------------------------------------------------

def message_rows(patient, *, after_id: int = 0, limit: int = 200) -> list[dict]:
    """This client's conversation as bubbles, oldest first.

    Every message on the client's file that belongs to one of their
    conversations — WhatsApp, SMS and web alike, since the chat is the client's
    and not the device's. Rows outside a conversation (the old reminder rows,
    which went to a caregiver) are left out. ``after_id`` is the poll cursor.
    Each row is a message, which can make two bubbles: what came in and what
    went out.
    """
    from django.urls import reverse

    from .models import Conversation, Message

    conv_ids = [str(c) for c in
                Conversation.objects.filter(patient=patient).values_list("id", flat=True)]
    if not conv_ids:
        return []
    qs = Message.objects.filter(patient=patient, conversation_id__in=conv_ids)
    if after_id:
        qs = qs.filter(pk__gt=after_id).order_by("pk")[:limit]
        msgs = list(qs)
    else:
        msgs = list(qs.order_by("-pk")[:limit])
        msgs.reverse()

    rows = []
    for m in msgs:
        row = {"id": m.pk, "ts": timezone.localtime(m.timestamp).strftime("%H:%M"),
               "day": timezone.localtime(m.timestamp).date().isoformat(),
               "conversation": m.conversation_id, "bubbles": []}
        if m.input_audio_file:
            row["bubbles"].append({"who": "user", "audio": reverse("webchat_audio", args=[m.pk, "input"])})
        elif m.user_message:
            row["bubbles"].append({"who": "user", "text": m.user_message})
        if m.response_message:
            from .utils import PART_JOINER
            # A reply sent as separate messages was stored joined; split it
            # back only for agents that send that way, so an ordinary reply
            # with paragraphs stays one bubble.
            parts = [m.response_message]
            agent = getattr(patient, "agent", None)
            if agent is not None and getattr(agent, "send_separately", False):
                parts = [p for p in m.response_message.split(PART_JOINER) if p.strip()]
            for i, part in enumerate(parts):
                b = {"who": "bot", "text": part}
                if i == len(parts) - 1 and m.response_audio_file:
                    b["audio"] = reverse("webchat_audio", args=[m.pk, "output"])
                row["bubbles"].append(b)
        elif m.response_audio_file:
            row["bubbles"].append({"who": "bot", "audio": reverse("webchat_audio", args=[m.pk, "output"])})
        if row["bubbles"]:
            rows.append(row)
    return rows


# --- Messages the platform starts -------------------------------------------

def deliver(patient, text: str, *, thread_id: str | None = None):
    """Put a message the platform started into the client's web app chat.

    Nothing is sent anywhere: the row is the delivery, and the open page picks
    it up on its next poll. Written to ``thread_id`` when the caller has just
    opened one (a protocol's first question), otherwise to the client's current
    conversation, so it reads in sequence with whatever they say next.
    """
    from .models import Message
    from .utils import _get_or_create_thread, save_message

    thread = thread_id or _get_or_create_thread(patient)
    return save_message(
        f"web:{patient.pk}", "", text, thread, patient=patient,
        sender_role=Message.SenderRole.PLATFORM, channel=Message.Channel.WEB,
    )
