"""The web app chat: the pages a client's tablet sees, and the staff controls.

Every route here 404s unless WEB_CHAT_ENABLED is on (ConvAI.web_chat). The
device pages take no login: what lets a browser in is the device cookie it was
given when it scanned the client's QR code, and it opens this one client's chat
and nothing else. See web_app_chat.md.
"""
from ._base import *  # noqa: F401,F403

import logging

from django.templatetags.static import static
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.cache import never_cache

from .. import web_chat
from ..models import WebChatDevice
from ..site_config import get_bool

__all__ = [
    'webchat_join', 'webchat_home', 'webchat_send', 'webchat_messages',
    'webchat_voice', 'webchat_audio', 'webchat_signout', 'webchat_manifest',
    'webchat_service_worker',
    'webchat_set_channel', 'webchat_reset_link', 'webchat_unlink_device',
]

# Accepted voice-note containers: Chrome records webm, Safari mp4.
_AUDIO_EXTS = (".webm", ".ogg", ".wav", ".mp4", ".m4a")
logger = logging.getLogger(__name__)
_MAX_TEXT = 4000


def _on_or_404():
    if not web_chat.enabled():
        raise Http404("The web app chat is not enabled.")


def _device_or_403(request):
    device = web_chat.current_device(request)
    if device is None:
        return None, JsonResponse({"error": "not_linked"}, status=403)
    return device, None


def _first_name(patient) -> str:
    # The join page and the chat greet by first name only: a forwarded link or
    # a tablet left on a table shows as little about the person as it can while
    # still being recognisable to them.
    return (patient.name or "").strip().split(" ")[0]


def _chat_title(patient) -> str:
    agent = getattr(patient, "agent", None)
    return getattr(agent, "name", None) or str(_("Assistant"))


# --- Device pages ------------------------------------------------------------

@never_cache
@ensure_csrf_cookie
def webchat_join(request, public_id, token):
    """The page a scanned QR code opens: whose chat this is, and a button to open it.

    Joining is the POST, not the GET, so a link preview or a prefetch cannot
    create a device, and the person holding the tablet sees the name before
    they go in.
    """
    _on_or_404()
    link = web_chat.resolve_link(public_id, token)
    if link is None:
        return render(request, "webchat/unlinked.html",
                      {"reason": "invalid"}, status=404)

    current = web_chat.current_device(request)
    if current is not None and current.patient_id == link.patient_id:
        return redirect("webchat_home")

    if request.method == "POST":
        # A tablet handed from one person to the next: whoever it belonged to
        # before is signed out of it, rather than left live with a key nobody
        # holds any more.
        if current is not None:
            web_chat.revoke(current, reason=WebChatDevice.RevokedBy.DEVICE)
        _device, key = web_chat.join(link, request)
        response = redirect("webchat_home")
        web_chat.set_cookie(response, key, request)
        return response

    return render(request, "webchat/join.html", {
        "first_name": _first_name(link.patient),
        "switching": current is not None,
    })


@never_cache
@ensure_csrf_cookie
def webchat_home(request):
    """The chat itself, or how to get into it when this device is not linked."""
    _on_or_404()
    device = web_chat.current_device(request)
    if device is None:
        response = render(request, "webchat/unlinked.html", {"reason": "not_linked"})
        # A cookie that no longer opens anything (unlinked, reset) is dropped,
        # so the next scan starts clean.
        if web_chat.COOKIE_NAME in request.COOKIES:
            web_chat.clear_cookie(response)
        return response

    patient = device.patient
    return render(request, "webchat/chat.html", {
        "chat_title": _chat_title(patient),
        "first_name": _first_name(patient),
        "rows": web_chat.message_rows(patient),
        "voice_enabled": get_bool("WHATSAPP_AUDIO_ENABLED"),
        "chatbot_enabled": patient.chatbot_enabled,
    })


def _row_for(msg, patient):
    """The one message just written, in the shape the poll returns."""
    if msg is None:
        return []
    return web_chat.message_rows(patient, after_id=msg.pk - 1, limit=1)


@require_POST
def webchat_send(request):
    """A typed message from the tablet: answered like a WhatsApp message is.

    The reply comes back in the response, and the page also polls — so a reply
    still arrives if this request was cut off, and messages the platform starts
    arrive at all.
    """
    _on_or_404()
    device, denied = _device_or_403(request)
    if denied:
        return denied
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")
    text = (payload.get("message") or "").strip()[:_MAX_TEXT]
    if not text:
        return HttpResponseBadRequest("Empty message")

    patient = device.patient
    started = timezone.now()
    from ..utils import process_message_for_patient_with_row
    reply, msg = process_message_for_patient_with_row(
        patient, text, user_label=f"web:{device.pk}",
        sender_role=Message.SenderRole.CLIENT, channel=Message.Channel.WEB,
    )
    # Whether this turn ended the conversation — the agent said so, or /quit.
    ended = msg is None or Conversation.objects.filter(
        patient=patient, ended_at__gte=started).exists()
    return JsonResponse({
        "rows": _row_for(msg, patient),
        # /quit writes no message; its answer is shown once and not kept.
        "notice": reply if msg is None else "",
        "reset": ended,
    })


@require_GET
def webchat_messages(request):
    """Everything on this client's chat after ``after`` — the page's poll."""
    _on_or_404()
    device, denied = _device_or_403(request)
    if denied:
        return denied
    try:
        after = max(0, int(request.GET.get("after") or 0))
    except ValueError:
        after = 0
    return JsonResponse({"rows": web_chat.message_rows(device.patient, after_id=after)})


@require_POST
def webchat_voice(request):
    """A voice note from the tablet: transcribed, answered, and spoken back.

    Only while voice notes are on for WhatsApp, so the two channels behave the
    same. Unlike the WhatsApp voice path, the transcript goes through the full
    inbound pipeline — the agent switch, automations, the review — exactly as a
    typed message would.
    """
    _on_or_404()
    if not get_bool("WHATSAPP_AUDIO_ENABLED"):
        raise Http404("Voice notes are off.")
    device, denied = _device_or_403(request)
    if denied:
        return denied
    upload = request.FILES.get("audio_data")
    if upload is None:
        return HttpResponseBadRequest("No audio file provided")
    ext = os.path.splitext(upload.name)[1].lower()
    if ext not in _AUDIO_EXTS:
        return HttpResponseBadRequest("Invalid file type")
    if upload.size > 15 * 1024 * 1024:
        return HttpResponseBadRequest("Audio too large")

    patient = device.patient
    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
    in_name = f"{stamp}_{uuid.uuid4().hex[:8]}_web_in{ext}"
    os.makedirs(VOICE_RECORDINGS_DIR, exist_ok=True)
    in_path = os.path.join(VOICE_RECORDINGS_DIR, in_name)
    with open(in_path, "wb") as fp:
        for chunk in upload.chunks():
            fp.write(chunk)

    try:
        transcript = (transcribe_audio(in_path) or "").strip()
    except Exception:
        logger.exception("Web app voice note could not be transcribed")
        transcript = ""
    if not transcript:
        return JsonResponse({"error": "transcription_failed"}, status=422)

    from ..utils import process_message_for_patient_with_row
    started = timezone.now()
    reply, msg = process_message_for_patient_with_row(
        patient, transcript, user_label=f"web:{device.pk}",
        sender_role=Message.SenderRole.CLIENT, channel=Message.Channel.WEB,
        input_audio_file=in_name,
    )
    if msg is not None and reply:
        out_name = f"{stamp}_{uuid.uuid4().hex[:8]}_web_out.mp3"
        try:
            synthesize_speech(str(reply), out_name, agent=getattr(patient, "agent", None))
            msg.response_audio_file = out_name
            msg.save(update_fields=["response_audio_file"])
        except Exception:
            # The text reply stands on its own; a voice that could not be made
            # is a missing extra, not a failed turn.
            logger.exception("Web app voice reply could not be synthesised")

    ended = msg is None or Conversation.objects.filter(
        patient=patient, ended_at__gte=started).exists()
    return JsonResponse({
        "rows": _row_for(msg, patient),
        "notice": reply if msg is None else "",
        "reset": ended,
    })


def webchat_audio(request, message_id, which):
    """A voice note on this device's own client's chat, and nobody else's."""
    _on_or_404()
    device = web_chat.current_device(request)
    if device is None:
        return HttpResponseForbidden(_("Not authorised."))
    msg = get_object_or_404(Message, pk=message_id, patient_id=device.patient_id)
    field = {"input": msg.input_audio_file, "output": msg.response_audio_file}.get(which)
    if not field:
        raise Http404("No audio")
    full_path = os.path.join(VOICE_RECORDINGS_DIR, os.path.basename(str(field)))
    if not os.path.exists(full_path):
        raise Http404("Audio file not found on disk.")
    ctype = "audio/mpeg" if full_path.endswith(".mp3") else (
        "audio/mp4" if full_path.endswith((".mp4", ".m4a")) else "audio/webm")
    return FileResponse(open(full_path, "rb"), content_type=ctype)


@require_POST
def webchat_signout(request):
    """'Sign out of this tablet': the device stops opening the chat. History stays."""
    _on_or_404()
    device = web_chat.current_device(request)
    if device is not None:
        web_chat.revoke(device, reason=WebChatDevice.RevokedBy.DEVICE)
    response = redirect("webchat_home")
    web_chat.clear_cookie(response)
    return response


@require_GET
def webchat_manifest(request):
    """What 'Add to Home screen' installs: the chat, full screen, under /c/."""
    _on_or_404()
    from ..templatetags.custom_filters import brand_name
    name = str(brand_name() or "Chat")
    icon = lambda size: {  # noqa: E731
        "src": static(f"app/images/webchat-icon-{size}.png"),
        "sizes": f"{size}x{size}", "type": "image/png", "purpose": "any maskable",
    }
    manifest = {
        "name": name,
        # The label under the home-screen icon, which launchers cut at about
        # twelve characters: a long brand name becomes a plain "Chat" rather
        # than half a word.
        "short_name": name if len(name) <= 12 else str(_("Chat")),
        "start_url": reverse("webchat_home"),
        "scope": web_chat.COOKIE_PATH,
        "display": "standalone",
        "orientation": "any",
        "background_color": "#e7ecef",
        "theme_color": "#006877",
        "icons": [icon(192), icon(512)],
    }
    response = JsonResponse(manifest)
    response["Content-Type"] = "application/manifest+json"
    return response


_SERVICE_WORKER = """// The web app chat's service worker. It caches nothing on purpose: a chat
// shown from a cache is a chat that looks current and is not. It exists so the
// browser offers 'Install', and to say plainly when there is no connection.
const OFFLINE = `<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#e7ecef;color:#0b3238;text-align:center;padding:1rem">
<div><p style="font-size:1.2rem;font-weight:600">%(title)s</p><p>%(body)s</p></div>`;
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => {
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).catch(() =>
    new Response(OFFLINE, { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
});
"""


@require_GET
def webchat_service_worker(request):
    _on_or_404()
    body = _SERVICE_WORKER % {
        "title": _("No connection"),
        "body": _("Check the Wi-Fi, then open the chat again."),
    }
    response = HttpResponse(body, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    return response


# --- Staff controls (the client page) ---------------------------------------

def _own_client_or_403(request, pk):
    patient = get_object_or_404(Patient.objects.select_related("navigator"), pk=pk)
    if not (is_admin(request.user) or patient.navigator_id == request.user.id):
        return patient, HttpResponseForbidden(_("You cannot change this client."))
    return patient, None


def _back(request, patient):
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(f"{reverse('patient_detail', args=[patient.pk])}#webapp")


@require_POST
def webchat_set_channel(request, pk):
    """Where messages the platform starts go for this client: phone or web app."""
    _on_or_404()
    patient, denied = _own_client_or_403(request, pk)
    if denied:
        return denied
    value = request.POST.get("chat_channel", "")
    if value not in dict(Patient.CHAT_CHANNELS):
        return HttpResponseBadRequest("Unknown channel")
    patient.chat_channel = value
    patient.save(update_fields=["chat_channel"])
    if value == "web":
        messages.success(request, _("Messages the platform starts for %s now go to the web app.")
                         % patient.name)
    else:
        messages.success(request, _("Messages the platform starts for %s go to their phone again.")
                         % patient.name)
    return _back(request, patient)


@require_POST
def webchat_reset_link(request, pk):
    """New QR code: the old one, and every device that joined through it, stop."""
    _on_or_404()
    patient, denied = _own_client_or_403(request, pk)
    if denied:
        return denied
    web_chat.reset_link(web_chat.link_for(patient), by=request.user)
    messages.success(request, _("New QR code issued. Every device that was linked "
                                "has been signed out."))
    return _back(request, patient)


@require_POST
def webchat_unlink_device(request, pk, device_id):
    """Sign one device out of this client's chat — a tablet handed back."""
    _on_or_404()
    patient, denied = _own_client_or_403(request, pk)
    if denied:
        return denied
    device = get_object_or_404(WebChatDevice, pk=device_id, patient=patient,
                               revoked_at__isnull=True)
    web_chat.revoke(device, reason=WebChatDevice.RevokedBy.STAFF, by=request.user)
    messages.success(request, _("Device unlinked. Its chat history stays on %s's file.")
                     % patient.name)
    return _back(request, patient)
