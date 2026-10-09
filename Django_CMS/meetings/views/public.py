"""The client's side: following a link, the lobby, being let in, leaving.

Nobody here is logged in; a link is what identifies them. The rules:

* **A GET on the link changes nothing.** Mail scanners (Outlook Safe Links,
  Mimecast) open every link in an email; if opening one bound a session, the
  scanner would be "the client". The link page only offers a button, and the
  POST behind it does the binding.
* **The link leaves the address bar.** After the POST, the session holds the
  invite and the browser is redirected to ``/m/lobby``, so the token is not in
  history, not in a Referer header, not on a screenshot.
* **Every failure looks the same.** Revoked, expired, cancelled, wrong token,
  feature off: one generic page, so a link cannot be used to learn anything.
* **No LiveKit token until admitted.** The lobby only polls this server.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from ConvAI.site_config import brand_name

from .. import config, links, rooms
from ..headers import meeting_page, private_link
from ..models import LobbyEntry, MeetingInvite

logger = logging.getLogger(__name__)

SESSION_KEY = "mtg_invite"
NONCE_KEY = "mtg_nonce"
LANG_KEY = "mtg_lang"
ATTEMPT_LIMIT = 30          # failed link checks per IP per window
ATTEMPT_WINDOW_S = 600


def _client_ip(request) -> str:
    return (request.META.get("REMOTE_ADDR") or "").strip()


def _throttled(request) -> bool:
    return cache.get(f"mtg-link-fail:{_client_ip(request)}", 0) >= ATTEMPT_LIMIT


def _note_failure(request):
    key = f"mtg-link-fail:{_client_ip(request)}"
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, ATTEMPT_WINDOW_S)


def _language(request, invite=None) -> str:
    chosen = request.GET.get("lang") or request.session.get(LANG_KEY) or ""
    codes = {code for code, _name in settings.LANGUAGES}
    if chosen in codes:
        return chosen
    if invite is not None and invite.language in codes:
        return invite.language
    return translation.get_language() or settings.LANGUAGE_CODE


def _base_ctx(request, invite=None):
    lang = _language(request, invite)
    return {
        "brand": brand_name(),
        "lang": lang,
        "languages": settings.LANGUAGES,
    }, lang


def _unavailable(request, status=404):
    ctx, lang = _base_ctx(request)
    with translation.override(lang):
        return render(request, "meetings/public_unavailable.html", ctx, status=status)


def _bound_invite(request):
    """The invite this browser is joining with, if it is still good."""
    pk = request.session.get(SESSION_KEY)
    if not pk:
        return None
    invite = (MeetingInvite.objects
              .select_related("meeting", "meeting__patient", "meeting__patient__navigator")
              .filter(pk=pk).first())
    if invite is None or invite.nonce != request.session.get(NONCE_KEY):
        # A new link was issued since: the old binding goes with the old link.
        return None
    return invite


def _host_name(meeting) -> str:
    navigator = meeting.patient.navigator
    if not navigator:
        return _("your care team")
    return (navigator.first_name or navigator.get_full_name() or navigator.get_username()).strip()


def _when(meeting):
    return timezone.localtime(meeting.scheduled_time)


# ── The link ───────────────────────────────────────────────────────────────

@private_link
def link(request, public_id, token):
    if _throttled(request):
        return _unavailable(request, status=429)
    invite = links.resolve(public_id, token)
    if invite is None:
        _note_failure(request)
        return _unavailable(request)
    verdict = links.verdict(invite)
    if verdict == links.Verdict.UNAVAILABLE:
        return _unavailable(request)

    ctx, lang = _base_ctx(request, invite)
    ctx.update({"invite": invite, "host": _host_name(invite.meeting),
                "when": _when(invite.meeting), "early": verdict == links.Verdict.EARLY,
                "ics_url": f"{links.path_for(invite)}/calendar.ics"})

    if request.method == "POST":
        if verdict == links.Verdict.EARLY:
            with translation.override(lang):
                return render(request, "meetings/public_link.html", ctx)
        request.session.cycle_key()
        request.session[SESSION_KEY] = invite.pk
        request.session[NONCE_KEY] = invite.nonce
        if lang:
            request.session[LANG_KEY] = lang
        MeetingInvite.objects.filter(pk=invite.pk).update(
            last_opened_at=timezone.now(), open_count=invite.open_count + 1)
        return redirect("meetings:lobby")

    with translation.override(lang):
        return render(request, "meetings/public_link.html", ctx)


@private_link
@require_GET
def link_ics(request, public_id, token):
    invite = links.resolve(public_id, token)
    if invite is None or links.verdict(invite) == links.Verdict.UNAVAILABLE:
        return _unavailable(request)
    from ..ics import invite_ics
    resp = HttpResponse(invite_ics(invite, request), content_type="text/calendar; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="meeting.ics"'
    return resp


# ── The lobby ──────────────────────────────────────────────────────────────

@private_link
@meeting_page
@require_GET
def lobby(request):
    invite = _bound_invite(request)
    if invite is None or links.verdict(invite) == links.Verdict.UNAVAILABLE:
        return _unavailable(request)
    if request.GET.get("lang"):
        request.session[LANG_KEY] = _language(request, invite)
    ctx, lang = _base_ctx(request, invite)
    session = rooms.live_session(invite.meeting)
    s = config.cfg()
    recording = (session.recording_enabled if session else s.record_by_default) and invite.record_allowed
    from django.urls import reverse
    from .. import i18n
    with translation.override(lang):
        strings = i18n.client()
        strings["waitingTitle"] = strings["waitingTitle"] % {"host": _host_name(invite.meeting)}
        strings["noHostTitle"] = strings["noHostTitle"] % {"host": _host_name(invite.meeting)}
    ctx.update({
        "i18n": strings,
        "invite": invite,
        "host": _host_name(invite.meeting),
        "when": _when(invite.meeting),
        "recording": recording,
        "boot": {
            "name": invite.display_name,
            "host": _host_name(invite.meeting),
            "identity": invite.identity,
            "recording": recording,
            "urls": {
                "knock": reverse("meetings:knock"),
                "status": reverse("meetings:lobby_status"),
                "leave": reverse("meetings:leave"),
                "ended": reverse("meetings:ended"),
            },
        },
    })
    with translation.override(lang):
        return render(request, "meetings/public_lobby.html", ctx)


@require_POST
def knock(request):
    invite = _bound_invite(request)
    if invite is None or links.verdict(invite) != links.Verdict.OK:
        return JsonResponse({"state": "unavailable"}, status=410)
    rooms.knock(invite, user_agent=request.META.get("HTTP_USER_AGENT", ""))
    return _status_payload(invite)


@require_GET
def lobby_status(request):
    invite = _bound_invite(request)
    if invite is None or links.verdict(invite) == links.Verdict.UNAVAILABLE:
        return JsonResponse({"state": "unavailable"}, status=410)
    return _status_payload(invite)


def _status_payload(invite):
    session = rooms.live_session(invite.meeting)
    entry = LobbyEntry.objects.filter(meeting=invite.meeting, invite=invite).first()
    if entry:
        LobbyEntry.objects.filter(pk=entry.pk).update(seen_at=timezone.now())
    if session is None:
        if entry and entry.state == LobbyEntry.State.ADMITTED and entry.session_id:
            return JsonResponse({"state": "ended"})
        return JsonResponse({"state": "no_host"})
    if entry is None or entry.state == LobbyEntry.State.LEFT or entry.session_id not in (None, session.pk):
        return JsonResponse({"state": "ready"})
    if entry.state == LobbyEntry.State.DENIED:
        return JsonResponse({"state": "denied"})
    if entry.state == LobbyEntry.State.ADMITTED:
        return JsonResponse({
            "state": "admitted",
            "ws_url": config.livekit_url(),
            "token": rooms.invitee_token(session, invite),
            "recording": session.recording_enabled and invite.record_allowed,
        })
    return JsonResponse({"state": "waiting"})


@require_POST
def leave(request):
    invite = _bound_invite(request)
    if invite is not None:
        LobbyEntry.objects.filter(meeting=invite.meeting, invite=invite).exclude(
            state=LobbyEntry.State.DENIED).update(state=LobbyEntry.State.LEFT)
    return JsonResponse({"ok": True})


@private_link
@require_GET
def ended(request):
    invite = _bound_invite(request)
    ctx, lang = _base_ctx(request, invite)
    ctx.update({"invite": invite,
                "host": _host_name(invite.meeting) if invite else "",
                "reason": request.GET.get("reason", "")})
    with translation.override(lang):
        return render(request, "meetings/public_ended.html", ctx)
