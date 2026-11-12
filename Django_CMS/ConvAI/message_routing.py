"""One Twilio number, several installations: the routing hub.

Twilio delivers every inbound message for a phone number to **one** webhook.
Two Conversational Care installations that share a number therefore need one
of them — the **hub**, the one Twilio is pointed at — to work out which
installation a message is for and hand it over. Nothing else is shared:
outbound already works from each installation with its own copy of the Twilio
credentials, so only inbound needs routing. See shared_numbers.md.

Three parts, all here:

* :func:`claims` — the **claim rule**, the one answer an installation gives
  about a number and a message: *is this mine?* The hub asks itself with it,
  and asks every linked installation the same question over
  ``GET /api/v1/routing/claim/`` (which calls it on their side).
* :func:`decide` — ask everybody at once and pick the first claimant by
  priority. A peer that times out or errors has not claimed, and the reason
  is written to its ``last_error`` so the Settings card can show it.
* :func:`forward` — re-post the raw Twilio form to the winner's webhook,
  **re-signed** for that URL with the shared auth token, so the peer's own
  ``whatsapp_webhook`` accepts it exactly as if Twilio had called it. The
  ``X-CC-Routed: 1`` header tells the peer not to route it again.

:func:`route` is the entry point ``whatsapp_webhook`` calls once the Twilio
signature has checked out. It answers ``None`` — carry on locally — unless a
peer won, in which case it answers the response to give Twilio.

The rest is **alarms**: a misconfigured pair of installations fails quietly
(a message answered by the wrong one, or by nobody), so everything that can be
detected is shown on the Settings page — see :func:`peer_status`,
:func:`hub_warnings` and :func:`registration_warnings`, and the "What can go
wrong" section of shared_numbers.md.

**Why a hub and not a router service.** One more deployment to run, secure and
keep up is the wrong price for two installations; the hub is the platform
install, which already receives the webhook and already has a REST API. The
claim rule runs where the data is, so no installation has to tell another
which numbers it holds.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

__all__ = ["claims", "claim_payload", "decide", "forward", "route", "test_connection",
           "peer_status", "hub_warnings", "registration_warnings", "registration_collisions",
           "mask", "ROUTED_HEADER", "CLAIM_TIMEOUT", "FORWARD_TIMEOUT"]

# A peer has this long to answer a claim. Twilio gives the webhook about
# fifteen seconds in all, and the message still has to be forwarded after.
CLAIM_TIMEOUT = 2.0
# The forward itself: the peer's webhook may answer the message inline.
FORWARD_TIMEOUT = 10.0
# Set on every forwarded request; a webhook that sees it never routes again.
ROUTED_HEADER = "X-CC-Routed"
# A number that wrote to the registration agent this recently is still
# registering here, whatever it says next.
PROSPECT_WINDOW = timedelta(hours=24)
# Every inbound message asks every enabled peer, so on a number in use a peer
# that has not answered for this long is not being reached.
STALE_AFTER = timedelta(days=3)
# An installation asked by a hub within this window counts as sharing a number.
LINKED_FROM_HUB_WINDOW = timedelta(days=30)
# How often the claim endpoint writes down that it was asked.
CLAIMED_AT_RESOLUTION = timedelta(hours=1)
# What a connection test sends: a number nobody holds, and a body that is not
# anybody's registration message.
TEST_PHONE = "+15005550006"
TEST_BODY = "connection test"

EMPTY_TWIML = "<Response></Response>"


def mask(phone) -> str:
    """A number as the routing logs print it: enough to tell two apart, not
    enough to identify somebody — ``+44…0001``."""
    from .message_attribution import normalise

    number = normalise(phone)
    if len(number) <= 6:
        return "…"
    return f"{number[:3]}…{number[-4:]}"


def _fold(text) -> str:
    """Case- and whitespace-insensitive form of a message."""
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


# ----------------------------------------------------------------------------
# The claim rule
# ----------------------------------------------------------------------------

def claims(phone, body) -> tuple[bool, str]:
    """Is a message from ``phone`` saying ``body`` this installation's?

    Yes when:

    a. the number is on file — a client's or caregiver's number
       (:func:`message_attribution.resolve_inbound`) or a navigator's linked
       phone (:mod:`staff_whatsapp`);
    b. a self-registration is in progress from it — a pending
       ``SelfRegistration``, or a prospect's message in the last 24 hours;
    c. the number is unknown, self-registration is on, and the message starts
       with **this installation's** registration message (case and whitespace
       aside) — which is what makes one shared number serve two registration
       QR codes.

    Returns ``(claims, reason)``; the reason is one short phrase for the log
    and the claim API.
    """
    from . import registration, staff_whatsapp
    from .message_attribution import normalise, resolve_inbound
    from .models import Message, SelfRegistration
    from .site_config import get_bool

    phone = normalise(phone)
    if not phone:
        return False, "no number"

    inbound = resolve_inbound(phone)
    if inbound.patient is not None:
        return True, "caregiver" if inbound.role == Message.SenderRole.CAREGIVER else "client"
    if staff_whatsapp.staff_link_for(phone) is not None:
        return True, "navigator"

    if SelfRegistration.objects.filter(
            phone_number=phone, state=SelfRegistration.State.REGISTERED).exists():
        return True, "registration pending"
    since = timezone.now() - PROSPECT_WINDOW
    if Message.objects.filter(sender_role=Message.SenderRole.PROSPECT, user=phone,
                              timestamp__gte=since).exists():
        return True, "registering"

    if get_bool("SELF_REGISTRATION_ENABLED"):
        expected = _fold(registration.registration_message())
        if expected and _fold(body).startswith(expected):
            return True, "registration message"

    return False, "unknown number"


def _async_replies() -> bool:
    return bool(getattr(settings, "ASYNC_WHATSAPP_REPLY", False))


def claim_payload(phone, body) -> dict:
    """What the claim endpoint answers: the verdict, plus what the hub needs
    to warn about this installation — whether it replies asynchronously, and
    the registration message it is waiting for (blank while self-registration
    is off, since then it is waiting for none)."""
    from . import registration
    from .site_config import get_bool

    ok, reason = claims(phone, body)
    self_reg = get_bool("SELF_REGISTRATION_ENABLED")
    _note_claimed()
    return {
        "claims": ok,
        "reason": reason,
        "async_replies": _async_replies(),
        "self_registration": self_reg,
        "registration_message": registration.registration_message() if self_reg else "",
    }


def _note_claimed() -> None:
    """Write down that a hub asked, at most once per CLAIMED_AT_RESOLUTION —
    a conditional UPDATE, so most requests write nothing."""
    from django.db.models import Q

    from .models import SiteConfiguration

    now = timezone.now()
    SiteConfiguration.load()  # the singleton row exists
    (SiteConfiguration.objects.filter(pk=1)
     .filter(Q(routing_claimed_at__isnull=True)
             | Q(routing_claimed_at__lt=now - CLAIMED_AT_RESOLUTION))
     .update(routing_claimed_at=now))


# ----------------------------------------------------------------------------
# Asking everybody, and choosing
# ----------------------------------------------------------------------------

@dataclass
class Answer:
    """One installation's answer. ``peer`` is None for the hub itself."""

    name: str
    priority: int
    peer: object = None
    claims: bool = False
    reason: str = ""
    error: str = ""
    # What a peer said about itself (None/blank when it did not answer).
    peer_async: bool | None = None
    registration_message: str = ""

    @property
    def is_hub(self) -> bool:
        return self.peer is None


def _hub_priority() -> int:
    from .models import SiteConfiguration
    try:
        return int(SiteConfiguration.load().routing_priority)
    except Exception:
        return 100


def _ask_peer(peer, phone: str, body: str) -> Answer:
    """One claim request to one peer. Runs in a worker thread, so it touches
    no database: the caller records the outcome.

    Each way of failing gets its own wording, because each has a different
    fix: a wrong token, a wrong URL or an old release, a peer that is down.
    """
    import requests

    answer = Answer(name=peer.name, priority=peer.priority, peer=peer)
    try:
        resp = requests.get(
            peer.claim_url,
            params={"phone": phone, "body": body},
            headers={"Authorization": f"Token {peer.api_token}", "Accept": "application/json"},
            timeout=CLAIM_TIMEOUT,
        )
    except requests.Timeout:
        answer.error = _("did not answer within %(s)s seconds (timeout)") % {"s": f"{CLAIM_TIMEOUT:g}"}
        return answer
    except requests.ConnectionError as exc:
        answer.error = (_("could not be reached (ConnectionError): %(detail)s")
                        % {"detail": _connection_reason(exc)})
        return answer
    except Exception as exc:
        answer.error = f"{type(exc).__name__}: {exc}"[:500]
        return answer

    if resp.status_code in (401, 403):
        answer.error = _("rejected the API token (HTTP %(code)s): paste a valid token from that "
                         "installation's Settings → API client") % {"code": resp.status_code}
        return answer
    if resp.status_code == 404:
        answer.error = _("has no claim endpoint (HTTP 404): it runs an older release, its REST API "
                         "is switched off (ENABLE_API), or the webhook URL points at the wrong host")
        return answer
    if resp.status_code != 200:
        answer.error = _("claim endpoint answered HTTP %(code)s") % {"code": resp.status_code}
        return answer
    try:
        data = resp.json()
        answer.claims = bool(data["claims"])
    except Exception:
        answer.error = _("answered, but not with the claim endpoint's JSON: check the webhook URL")
        return answer
    answer.reason = str(data.get("reason") or "")
    if "async_replies" in data:
        answer.peer_async = bool(data["async_replies"])
    if data.get("self_registration"):
        answer.registration_message = str(data.get("registration_message") or "")[:200]
    return answer


def _connection_reason(exc) -> str:
    """The operating system's reason from urllib3's long chain of wrappers —
    "[Errno 111] Connection refused" rather than three lines of pool state."""
    text = str(exc)
    m = re.search(r"\[Errno -?\d+\][^'\")\]]*", text)
    if m:
        return m.group(0).strip()
    return text[:200]


def _record(peer, error: str = "", answer: Answer | None = None) -> None:
    """Note the outcome of a call to ``peer`` on its row, without touching the
    rest of it (an admin may be editing it at the same time)."""
    from .models import LinkedInstallation

    fields = {"last_error": error}
    if not error:
        fields["last_contact"] = timezone.now()
    if answer is not None and not answer.error:
        fields["peer_async"] = answer.peer_async
        fields["peer_registration_message"] = answer.registration_message
    LinkedInstallation.objects.filter(pk=peer.pk).update(**fields)


def _poll(phone: str, body: str, peers) -> list[Answer]:
    """Everybody's answer: every peer in parallel, then the hub locally."""
    peers = list(peers)
    answers = []
    if peers:
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            futures = [pool.submit(_ask_peer, p, phone, body) for p in peers]
            answers = [f.result() for f in futures]
        for a in answers:
            _record(a.peer, a.error, a)
            if a.error:
                logger.warning("Claim request to %s for %s failed: %s", a.name, mask(phone), a.error)

    ok, reason = claims(phone, body)
    answers.append(Answer(name="this installation", priority=_hub_priority(),
                          claims=ok, reason=reason))
    return answers


def _choose(answers) -> Answer | None:
    """The lowest priority number among those that claimed; on a tie a peer
    beats the hub, since the hub's default is meant to be the fallback."""
    claimants = [a for a in answers if a.claims]
    if not claimants:
        return None
    claimants.sort(key=lambda a: (a.priority, 1 if a.is_hub else 0, a.name))
    return claimants[0]


def decide(phone: str, body: str, peers) -> Answer | None:
    """Ask the hub and every peer, and answer the winner — or None when nobody
    claims the message. Peers are asked within :data:`CLAIM_TIMEOUT`."""
    return _choose(_poll(phone, body, peers))


# ----------------------------------------------------------------------------
# Forwarding
# ----------------------------------------------------------------------------

def forward(peer, params: dict, reason: str = "") -> HttpResponse:
    """Re-post the Twilio form to ``peer``'s webhook and relay what it answers.

    The request is signed for the peer's URL with ``TWILIO_AUTH_TOKEN`` — the
    same token on both installations — so the peer's own signature check
    passes; it reads from ``os.environ`` because that is where
    ``whatsapp_webhook`` validates from. One retry on a connection failure
    (not on a timeout: the peer may already be answering, and Twilio is
    waiting). When both attempts fail the message is **not** processed here —
    it is the peer's — Twilio gets an empty TwiML so it does not retry forever
    against a webhook that did its part, and the failure is counted on the
    peer's row.
    """
    import requests
    from twilio.request_validator import RequestValidator

    from .models import LinkedInstallation

    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    signature = RequestValidator(token).compute_signature(peer.webhook_url, params)
    headers = {"X-Twilio-Signature": signature, ROUTED_HEADER: "1"}
    sender = mask(params.get("From", ""))

    last_error = ""
    for attempt in (1, 2):
        try:
            resp = requests.post(peer.webhook_url, data=params, headers=headers,
                                 timeout=FORWARD_TIMEOUT)
        except requests.ConnectionError as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:400]
            logger.warning("Forward to %s failed (attempt %d): %s", peer.name, attempt, last_error)
            continue
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:400]
            break
        if resp.status_code == 403:
            # The peer refused the signature: a different auth token, or a
            # webhook URL that is not the one it builds for itself.
            logger.error("%s refused the forwarded message from %s (HTTP 403): is TWILIO_AUTH_TOKEN "
                         "the same on both, and is %s exactly its webhook URL?",
                         peer.name, sender, peer.webhook_url)
            _record(peer, _("refused a forwarded message (HTTP 403): its Twilio auth token or "
                            "webhook URL does not match"))
            LinkedInstallation.objects.filter(pk=peer.pk).update(
                forward_failures=F("forward_failures") + 1)
        else:
            _record(peer)
            logger.info("Routed message from %s to %s (%s)", sender, peer.name, reason)
        return HttpResponse(resp.content, status=resp.status_code,
                            content_type=resp.headers.get("Content-Type", "text/xml"))

    _record(peer, _("a forwarded message could not be delivered: %(error)s") % {"error": last_error})
    LinkedInstallation.objects.filter(pk=peer.pk).update(forward_failures=F("forward_failures") + 1)
    logger.error("Message from %s claimed by %s (%s) could not be forwarded: %s",
                 sender, peer.name, reason, last_error)
    return HttpResponse(EMPTY_TWIML, content_type="text/xml")


def route(request) -> HttpResponse | None:
    """Called by ``whatsapp_webhook`` after the signature check.

    None means *handle it here* — there is nobody to route to, the request was
    already routed, the hub won, or nobody claimed it. Otherwise the response to
    give Twilio for a message that is now a peer's.
    """
    from .message_attribution import normalise
    from .models import LinkedInstallation

    if request.headers.get(ROUTED_HEADER):
        return None
    peers = list(LinkedInstallation.objects.filter(enabled=True).order_by("priority", "name"))
    if not peers:
        return None

    params = request.POST.dict()
    phone = normalise(params.get("From", ""))
    body = params.get("Body", "") or ""

    answers = _poll(phone, body, peers)
    winner = _choose(answers)
    if winner is None or winner.is_hub:
        silent = [a for a in answers if a.error]
        if silent:
            # A peer that did not answer may have been the one this message was
            # for. Handled here anyway — but somebody should know.
            logger.warning("Message from %s handled here without an answer from %s; it may have "
                           "been theirs.", mask(phone), ", ".join(a.name for a in silent))
        if winner is None:
            logger.info("Message from %s claimed by nobody; handled here", mask(phone))
        else:
            logger.info("Message from %s handled here (%s)", mask(phone), winner.reason)
        return None
    return forward(winner.peer, params, winner.reason)


# ----------------------------------------------------------------------------
# Alarms (Settings)
# ----------------------------------------------------------------------------

def test_connection(peer) -> tuple[bool, str, list[str]]:
    """Call the peer's claim endpoint with a number nobody holds, and record
    the outcome on its row as a real message would.

    Returns ``(ok, detail, warnings)``: ``detail`` says what went wrong when
    it failed; ``warnings`` are what is wrong with a peer that did answer.
    """
    answer = _ask_peer(peer, TEST_PHONE, TEST_BODY)
    _record(peer, answer.error, answer)
    if answer.error:
        return False, answer.error, []
    peer.refresh_from_db()
    return True, answer.reason or "ok", [text for _level, kind, text in peer_status(peer)["notes"]
                                         if kind in ("sync", "collision")]


def peer_status(peer, now=None) -> dict:
    """How a linked installation looks on the Settings card.

    ``level`` is ``ok``, ``warn`` or ``error`` (red); ``notes`` are
    ``(level, kind, text)``. A disabled peer raises nothing: it is not asked.
    """
    now = now or timezone.now()
    notes = []
    if not peer.enabled:
        return {"level": "off", "notes": notes}
    if peer.last_error:
        notes.append(("error", "failed", _("Last call failed: %(error)s") % {"error": peer.last_error}))
    if peer.last_contact is None:
        notes.append(("error", "never", _("Has never answered. Press Test connection.")))
    elif now - peer.last_contact > STALE_AFTER:
        notes.append(("error", "stale", _("No answer since %(when)s.")
                      % {"when": timezone.localtime(peer.last_contact).strftime("%d/%m/%Y %H:%M")}))
    if peer.peer_async is False:
        notes.append(("warn", "sync", _("Replies synchronously (ASYNC_WHATSAPP_REPLY is off there): each "
                                "forwarded message waits for its whole reply, and Twilio gives up "
                                "after 15 seconds.")))
    if peer.forward_failures:
        notes.append(("warn", "failures", _("%(n)s forwarded message(s) could not be delivered.")
                      % {"n": peer.forward_failures}))
    for a, b in registration_collisions():
        if peer.name in (a, b):
            other = b if a == peer.name else a
            notes.append(("error", "collision", _("Waits for the same registration message as %(other)s: "
                                     "strangers go to whichever ranks first.") % {"other": other}))
    level = ("error" if any(note[0] == "error" for note in notes)
             else "warn" if notes else "ok")
    return {"level": level, "notes": notes}


def registration_collisions() -> list[tuple[str, str]]:
    """Pairs of installations on this number that wait for overlapping
    registration messages — one starts with the other, so the claim rule would
    let both claim the same first message. Built from the hub's own message
    and what each enabled peer last reported."""
    from . import registration
    from .models import LinkedInstallation
    from .site_config import get_bool

    entries = []
    if get_bool("SELF_REGISTRATION_ENABLED"):
        entries.append((_("This installation"), _fold(registration.registration_message())))
    for peer in LinkedInstallation.objects.filter(enabled=True).order_by("priority", "name"):
        if peer.peer_registration_message:
            entries.append((peer.name, _fold(peer.peer_registration_message)))
    pairs = []
    for i, (name_a, msg_a) in enumerate(entries):
        for name_b, msg_b in entries[i + 1:]:
            if msg_a and msg_b and (msg_a.startswith(msg_b) or msg_b.startswith(msg_a)):
                pairs.append((name_a, name_b))
    return pairs


def hub_warnings() -> list[str]:
    """Warnings about this installation as a hub, shown at the top of the
    Linked installations card. Empty while no linked installation is enabled."""
    from .models import LinkedInstallation

    if not LinkedInstallation.objects.filter(enabled=True).exists():
        return []
    out = []
    if not _async_replies():
        out.append(_("This installation replies synchronously (ASYNC_WHATSAPP_REPLY is off). Each "
                     "message already waits up to %(s)s seconds for the linked installations to "
                     "answer, then for this installation's whole reply, and Twilio gives up after "
                     "15 seconds. Set ASYNC_WHATSAPP_REPLY=1 and restart.")
                   % {"s": f"{CLAIM_TIMEOUT:g}"})
    for a, b in registration_collisions():
        out.append(_("%(a)s and %(b)s wait for the same registration message, so a stranger "
                     "always goes to whichever ranks first. Give each its own under "
                     "Settings → Registrations.") % {"a": a, "b": b})
    return out


def linked_from_hub_since(now=None):
    """When a hub last asked this installation, if recently enough to count."""
    from .models import SiteConfiguration

    now = now or timezone.now()
    at = SiteConfiguration.objects.filter(pk=1).values_list("routing_claimed_at", flat=True).first()
    if at and now - at <= LINKED_FROM_HUB_WINDOW:
        return at
    return None


def registration_warnings() -> list[str]:
    """Warnings for Settings → Registrations, on either side of a link."""
    from . import registration
    from .models import LinkedInstallation

    out = []
    is_hub = LinkedInstallation.objects.filter(enabled=True).exists()
    claimed_at = linked_from_hub_since()
    if registration.registration_message_is_default():
        if is_hub:
            out.append(_("This installation shares its number with linked installations but still "
                         "uses the default registration message. If another one does too, every "
                         "new registration goes to whichever ranks first. Choose a message of "
                         "this installation's own."))
        elif claimed_at:
            out.append(_("A hub asked this installation about a message on %(when)s, so it shares "
                         "its number with another installation, but it still uses the default "
                         "registration message. Choose one of its own, or new registrations may "
                         "go to the other installation.")
                       % {"when": timezone.localtime(claimed_at).strftime("%d/%m/%Y")})
    if is_hub:
        for a, b in registration_collisions():
            msg = (_("%(a)s and %(b)s wait for the same registration message.")
                   % {"a": a, "b": b})
            if msg not in out:
                out.append(msg)
    return out


def settings_alert_counts() -> dict:
    """How many alarms each settings tab holds, for the dot in its nav entry."""
    from .models import LinkedInstallation

    now = timezone.now()
    red = sum(1 for p in LinkedInstallation.objects.filter(enabled=True)
              if peer_status(p, now)["level"] == "error")
    return {"messaging": len(hub_warnings()) + red, "registrations": len(registration_warnings())}
