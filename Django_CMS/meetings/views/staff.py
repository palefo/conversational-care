"""The navigator's side: the room window, starting and ending, the lobby,
invites, and the agents."""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from ConvAI.models import Meeting, Patient, Protocol
from ConvAI.roles import is_admin, navigator_required

from .. import config, invites, links, rooms
from ..headers import meeting_page
from ..models import AgentRun, InviteDelivery, MeetingInvite, MeetingSession

logger = logging.getLogger(__name__)


def _meeting(request, pk) -> Meeting:
    meeting = get_object_or_404(
        Meeting.objects.select_related("patient", "patient__caregiver", "patient__navigator"),
        pk=pk)
    if not (is_admin(request.user) or meeting.patient.navigator_id == request.user.id):
        raise Http404()
    return meeting


def _require_on():
    if not config.enabled():
        raise Http404()


def _err(message, status=400):
    return JsonResponse({"ok": False, "error": str(message)}, status=status)


def _wants_json(request) -> bool:
    return (request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "json" in (request.headers.get("accept") or ""))


def _back(request, meeting):
    nxt = (request.POST.get("next") or "").strip()
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(f"{reverse('communications')}?item={meeting.panel_token}")


def _protocol_choices(meeting):
    """Protocols the interviewer can work through: this meeting's first."""
    booked = list(meeting.scheduled_protocols.filter(questions__isnull=False).distinct()
                  .order_by("number"))
    booked_ids = {p.pk for p in booked}
    programme = [p for p in meeting.patient.protocols.filter(questions__isnull=False)
                 .distinct().order_by("number") if p.pk not in booked_ids]
    out = []
    for p, scheduled in [(p, True) for p in booked] + [(p, False) for p in programme]:
        out.append({"number": p.number, "title": p.title, "scheduled": scheduled,
                    "questions": p.questions.count()})
    return out


# ── The room window ────────────────────────────────────────────────────────

@navigator_required
@meeting_page
@require_GET
def staff_room(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    if not config.enabled() and not rooms.live_session(meeting):
        raise Http404()
    patient = meeting.patient
    invite = invites.primary_invite(meeting)
    s = config.cfg()
    from .. import i18n
    ctx = {
        "i18n": i18n.staff(),
        "meeting": meeting,
        "patient": patient,
        "invite": invite,
        "join_url": links.url_for(invite, request) if invite else "",
        "boot": {
            "meeting": meeting.pk,
            "patient": f"{patient.name} {patient.lastname}".strip(),
            "invitee": (invite.display_name if invite else
                        (patient.caregiver.name if patient.caregiver else patient.name)),
            "scheduled": timezone.localtime(meeting.scheduled_time).isoformat(),
            "ws_url": config.livekit_url(),
            "configured": config.livekit_configured(),
            "me": {"identity": rooms.staff_identity(request.user),
                   "name": rooms.staff_name(request.user)},
            "protocols": _protocol_choices(meeting),
            "assistant": bool(s.assistant_agent_id) and bool(config.service_key()),
            "agents": bool(config.service_key()),
            "record_default": s.record_by_default,
            "language": request.LANGUAGE_CODE if hasattr(request, "LANGUAGE_CODE") else "",
            "urls": {
                "start": reverse("meetings:start", args=[meeting.pk]),
                "end": reverse("meetings:end", args=[meeting.pk]),
                "state": reverse("meetings:state", args=[meeting.pk]),
                "admit": reverse("meetings:lobby_decide", args=[meeting.pk, "__pid__", "admit"]),
                "deny": reverse("meetings:lobby_decide", args=[meeting.pk, "__pid__", "deny"]),
                "interview": reverse("meetings:interview_start", args=[meeting.pk]),
                "interview_stop": reverse("meetings:run_stop", args=[meeting.pk, 0]).replace("/0/", "/__id__/"),
                "questions": reverse("meetings:questions", args=[meeting.pk]),
                "assistant": reverse("meetings:assistant_start", args=[meeting.pk]),
                "auto_admit": reverse("meetings:auto_admit", args=[meeting.pk]),
                "invite": reverse("meetings:invite", args=[meeting.pk]),
                "send": reverse("meetings:invite_send", args=[meeting.pk]),
                "outcome": reverse("complete_meeting", args=[meeting.pk]),
                "panel": f"{reverse('communications')}?item={meeting.panel_token}",
                "save_answers": reverse("protocol_view", args=[meeting.pk, 0]).replace("/0/", "/__n__/"),
            },
            "outcomes": [
                [Meeting.Status.COMPLETED, _("Completed")],
                [Meeting.Status.NOT_ANSWERED, _("Didn't join")],
                [Meeting.Status.INTERRUPTED, _("Interrupted")],
            ],
        },
    }
    return render(request, "meetings/staff_room.html", ctx)


@navigator_required
@require_POST
def start(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    try:
        session = rooms.start(meeting, request.user)
    except rooms.MeetingError as exc:
        return _err(exc, 409)
    return JsonResponse({
        "ok": True,
        "ws_url": config.livekit_url(),
        "token": rooms.staff_token(session, request.user),
        "state": rooms.state_for(session, for_staff=True),
    })


@navigator_required
@require_POST
def end(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    session = rooms.live_session(meeting)
    if session:
        rooms.end(session, reason="ended_by_staff", by=request.user)
    return JsonResponse({"ok": True})


@navigator_required
@require_GET
def state(request, meeting_id):
    meeting = _meeting(request, meeting_id)
    session = rooms.live_session(meeting)
    data = rooms.state_for(session, for_staff=True)
    if session and request.GET.get("refresh") == "1":
        # A fresh token for a reconnect after the first one expired.
        data["token"] = rooms.staff_token(session, request.user)
        data["ws_url"] = config.livekit_url()
    return JsonResponse(data)


@navigator_required
@require_POST
def lobby_decide(request, meeting_id, public_id, decision):
    meeting = _meeting(request, meeting_id)
    session = rooms.live_session(meeting)
    if not session:
        return _err(_("The meeting is not open."), 409)
    invite = get_object_or_404(MeetingInvite, meeting=meeting, public_id=public_id)
    entry = rooms.decide(session, invite, admit=(decision == "admit"), by=request.user)
    if entry is None:
        return _err(_("Nobody is waiting with that link."), 404)
    return JsonResponse({"ok": True, "state": entry.state})


@navigator_required
@require_POST
def auto_admit(request, meeting_id):
    """Let invitees in without asking, for the rest of this meeting."""
    meeting = _meeting(request, meeting_id)
    session = rooms.live_session(meeting)
    if not session:
        return _err(_("The meeting is not open."), 409)
    on = request.POST.get("on") == "1"
    MeetingSession.objects.filter(pk=session.pk).update(auto_admit=on)
    if on:
        # Anyone already waiting is let in now rather than on their next knock.
        from ..models import LobbyEntry
        for entry in LobbyEntry.objects.filter(meeting=meeting, state=LobbyEntry.State.WAITING):
            rooms.decide(session, entry.invite, admit=True, by=request.user)
    return JsonResponse({"ok": True, "auto_admit": on})


# ── Invites ────────────────────────────────────────────────────────────────

@navigator_required
@require_POST
def invite(request, meeting_id):
    """Make the meeting's link, or issue a new one (``rotate=1``)."""
    _require_on()
    meeting = _meeting(request, meeting_id)
    inv = invites.primary_invite(meeting)
    if inv and request.POST.get("rotate") == "1":
        inv.rotate()
        msg = _("A new link was made. The old one no longer works.")
    elif inv is None:
        inv = invites.primary_invite(meeting, create=True, by=request.user)
        msg = _("Link created.")
    else:
        msg = ""
    url = links.url_for(inv, request)
    if _wants_json(request):
        return JsonResponse({"ok": True, "url": url, "message": msg})
    if msg:
        messages.success(request, msg)
    return _back(request, meeting)


@navigator_required
@require_POST
def invite_send(request, meeting_id):
    _require_on()
    meeting = _meeting(request, meeting_id)
    channel = request.POST.get("channel", "email")
    if channel not in InviteDelivery.Channel.values:
        return _err(_("Unknown channel."))
    inv = invites.primary_invite(meeting, create=True, by=request.user)
    ok, err = invites.send(inv, channel, by=request.user, request=request)
    label = InviteDelivery.Channel(channel).label
    if _wants_json(request):
        return JsonResponse({"ok": ok, "error": err,
                             "message": (_("Link sent by %(c)s.") % {"c": label.lower()}) if ok else err})
    if ok:
        messages.success(request, _("Link sent by %(c)s.") % {"c": label.lower()})
    else:
        messages.error(request, err or _("The link could not be sent."))
    return _back(request, meeting)


@navigator_required
@require_GET
def invite_ics(request, meeting_id):
    _require_on()
    meeting = _meeting(request, meeting_id)
    inv = invites.primary_invite(meeting, create=True, by=request.user)
    from ..ics import invite_ics as build
    resp = HttpResponse(build(inv, request), content_type="text/calendar; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="meeting-{meeting.pk}.ics"'
    return resp


@navigator_required
@require_POST
def start_now(request, patient_id):
    """An unscheduled online meeting, opened on the spot from the client page."""
    _require_on()
    patient = get_object_or_404(Patient.objects.select_related("caregiver", "navigator"),
                                pk=patient_id)
    if not (is_admin(request.user) or patient.navigator_id == request.user.id):
        raise Http404()
    meeting = Meeting.objects.create(
        patient=patient, scheduled_time=timezone.now(), modality=Meeting.Modality.ONLINE,
        status=Meeting.Status.PENDING, unscheduled=True,
    )
    invites.primary_invite(meeting, create=True, by=request.user)
    return JsonResponse({"ok": True, "meeting": meeting.pk, "item": meeting.panel_token,
                         "room_url": reverse("meetings:staff_room", args=[meeting.pk])})


# ── Agents ─────────────────────────────────────────────────────────────────

@navigator_required
@require_POST
def interview_start(request, meeting_id):
    _require_on()
    meeting = _meeting(request, meeting_id)
    session = rooms.live_session(meeting)
    if not session:
        return _err(_("Start the meeting first."), 409)
    try:
        number = int(request.POST.get("protocol", ""))
    except ValueError:
        return _err(_("Choose what to ask about."))
    protocol = Protocol.objects.filter(number=number).first()
    if protocol is None or not protocol.questions.exists():
        return _err(_("That protocol has no questions."))
    respondent = (request.POST.get("respondent") or "").strip()
    if not respondent:
        return _err(_("Choose who to interview."))
    try:
        run = rooms.start_interview(
            session, protocol=protocol, respondent_identity=respondent,
            respondent_name=(request.POST.get("respondent_name") or "").strip(),
            language=(request.POST.get("language") or "").strip(), by=request.user)
    except rooms.MeetingError as exc:
        return _err(exc, 409)
    return JsonResponse({"ok": True, "run": run.pk})


@navigator_required
@require_POST
def assistant_start(request, meeting_id):
    _require_on()
    meeting = _meeting(request, meeting_id)
    session = rooms.live_session(meeting)
    if not session:
        return _err(_("Start the meeting first."), 409)
    try:
        run = rooms.start_assistant(session, by=request.user)
    except rooms.MeetingError as exc:
        return _err(exc, 409)
    return JsonResponse({"ok": True, "run": run.pk})


@navigator_required
@require_POST
def run_stop(request, meeting_id, run_id):
    """Take an agent out — the server-side way, used if the in-room control fails."""
    meeting = _meeting(request, meeting_id)
    run = get_object_or_404(AgentRun.objects.select_related("session"),
                            pk=run_id, session__meeting=meeting)
    rooms.stop_run(run)
    return JsonResponse({"ok": True})


@navigator_required
@require_GET
def questions(request, meeting_id):
    """The interview checklist: one protocol's questions and this meeting's answers."""
    from ..protocol_data import questions_for

    meeting = _meeting(request, meeting_id)
    try:
        number = int(request.GET.get("protocol", ""))
    except ValueError:
        return _err(_("Which protocol?"))
    data = questions_for(meeting, number)
    if data is None:
        return _err(_("No such protocol."), 404)
    return JsonResponse(data)
