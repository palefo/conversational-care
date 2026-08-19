from ._base import *  # noqa: F401,F403
from ..forms import AgentForm, PromptAgentForm, NativeAgentForm
import logging

from ..realtime import (mint_realtime_session, realtime_configured,
                        RealtimeNotConfigured, RealtimeUpstreamError)

logger = logging.getLogger(__name__)

__all__ = ['agent_list', 'agent_form', 'agent_delete',
           'agent_test', 'agent_test_send', 'agent_test_audio',
           'agent_realtime_session', 'realtime_session_json']

# Which app-level form drives editing for each kind. Native agents can be
# *edited* (model/voice) but not *created* here — they ship with the platform.
_KIND_FORMS = {
    Agent.Kind.REMOTE: AgentForm,
    Agent.Kind.PROMPT: PromptAgentForm,
    Agent.Kind.NATIVE: NativeAgentForm,
}
_KIND_LABELS = {
    Agent.Kind.REMOTE: _("remote agent"),
    Agent.Kind.PROMPT: _("prompt-based agent"),
    Agent.Kind.NATIVE: _("native agent"),
}
# Kinds that admins may create from scratch (native agents are seeded, not created).
_CREATABLE_KINDS = {Agent.Kind.REMOTE, Agent.Kind.PROMPT}


@login_required
@admin_required
def agent_list(request):
    """Admin-only listing of agents, grouped by kind."""
    agents = Agent.objects.order_by('name')
    return render(request, 'agents/agent_list.html', {
        'active_page': 'agents',
        'native_agents': [a for a in agents if a.kind == Agent.Kind.NATIVE],
        'prompt_agents': [a for a in agents if a.kind == Agent.Kind.PROMPT],
        'remote_agents': [a for a in agents if a.kind == Agent.Kind.REMOTE],
    })


@login_required
@admin_required
def agent_form(request, pk=None, kind=None):
    """Create (pk=None, with ``kind``) or edit an agent.

    Native agents ship with the platform: they can be *edited* here (to pick the
    model/voice) but not *created* — new native agents come from migrations.
    """
    agent = get_object_or_404(Agent, pk=pk) if pk else None
    kind = agent.kind if agent else kind

    if kind not in _KIND_FORMS:
        messages.error(request, _("That agent kind cannot be edited here."))
        return redirect('agents')
    if agent is None and kind not in _CREATABLE_KINDS:
        messages.error(request, _("That agent kind cannot be created here."))
        return redirect('agents')

    FormClass = _KIND_FORMS[kind]
    if request.method == 'POST':
        form = FormClass(request.POST, instance=agent)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.kind = kind  # ensure kind is set for new records
            obj.save()
            form.save_m2m()
            messages.success(request, _("Agent saved.") if agent else _("Agent created."))
            return redirect('agents')
    else:
        form = FormClass(instance=agent)
    # Classification fields are advanced/optional; the form template tucks them
    # into a collapsible section when the form has them.
    classification_fields = ['classification_role', 'abstract_instruction', 'detectors']
    show_classification = any(f in form.fields for f in classification_fields)
    return render(request, 'agents/agent_form.html', {
        'active_page': 'agents',
        'form': form,
        'agent': agent,
        'kind': kind,
        'kind_label': _KIND_LABELS[kind],
        'classification_fields': classification_fields,
        'show_classification': show_classification,
    })


@login_required
@admin_required
@require_POST
def agent_delete(request, pk):
    agent = get_object_or_404(Agent, pk=pk)
    # Native agents ship with the platform (seeded) — they must not be deleted.
    if agent.kind == Agent.Kind.NATIVE:
        messages.error(request, _("Native agents cannot be deleted."))
        return redirect('agents')
    agent.delete()
    messages.success(request, _("Agent deleted."))
    return redirect('agents')


# ---------------------------------------------------------------------------
# Agent test chat — same UI as the external chatbot (with audio), wired to a
# specific agent. Ephemeral: nothing is persisted as history.
# ---------------------------------------------------------------------------
def realtime_session_json(agent):
    """Mint an ephemeral Realtime session for ``agent`` and wrap it as JSON.

    Shared by the admin test page and the tester chat. Failure modes map to
    HTTP statuses the voice UI understands (503 = not configured, 502 = Azure
    rejected the request).
    """
    try:
        return JsonResponse(mint_realtime_session(agent))
    except RealtimeNotConfigured as exc:
        return JsonResponse({'error': str(exc)}, status=503)
    except RealtimeUpstreamError as exc:
        # Azure's own error message (e.g. DeploymentNotFound) — admin-safe and
        # far more actionable than a generic failure.
        logger.error("Realtime session mint failed for agent %s: %s", agent.pk, exc)
        return JsonResponse({'error': str(exc)}, status=502)
    except Exception:
        logger.exception("Realtime session mint failed for agent %s", agent.pk)
        return JsonResponse(
            {'error': str(_("Could not start a voice session. Check the Azure "
                            "Realtime settings and try again."))},
            status=502)


@login_required
@admin_required
def agent_test(request, pk):
    """Render the external-chat UI pointed at a single agent for testing.

    Prompt-based agents with real-time voice enabled get the live voice-call
    UI instead; the Realtime API is only contacted when the call is started.
    """
    agent = get_object_or_404(Agent, pk=pk)
    if agent.kind == Agent.Kind.PROMPT and agent.realtime_enabled:
        return render(request, 'chat/realtime_voice.html', {
            'chat_title': "%s · %s" % (agent.name, _("test")),
            'session_url': reverse('agent_realtime_session', args=[agent.pk]),
            'realtime_ready': realtime_configured(),
        })
    return render(request, 'chat/external_chat_with_audio.html', {
        'chat_title': "%s · %s" % (agent.name, _("test")),
        'messages': [],  # test chat never loads history
        'send_url':  reverse('agent_test_send', args=[agent.pk]),
        'audio_url': reverse('agent_test_audio', args=[agent.pk]),
        # Fresh thread per page load → isolated session, not shown as history.
        'thread_id': str(uuid.uuid4()),
    })


@login_required
@admin_required
@require_POST
def agent_test_send(request, pk):
    """Text turn for the agent test chat. Does not persist anything."""
    agent = get_object_or_404(Agent, pk=pk)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")
    user_msg = (data.get('message') or '').strip()
    thread_id = (data.get('conversation_id') or '').strip() or str(uuid.uuid4())
    if not user_msg:
        return HttpResponseBadRequest("Empty message")
    # /quit ends the test session. The frontend starts a fresh thread, so there
    # is nothing to reset server-side — just acknowledge without calling the LLM.
    if user_msg.lower() in ('/quit', '/restart'):
        return JsonResponse({'user_message': user_msg,
                             'bot_message': str(_("Conversation ended.")), 'reset': True})
    bot_msg = generate_response_with_agent(agent, request.user, user_msg, thread_id)
    return JsonResponse({'user_message': user_msg, 'bot_message': bot_msg})


@login_required
@admin_required
@require_POST
def agent_test_audio(request, pk):
    """Voice turn for the agent test chat.

    Transcribe → agent reply → TTS, returning the audio inline as a data URL so
    no Message row (and no served audio file) is created. Temp files are removed.
    """
    agent = get_object_or_404(Agent, pk=pk)
    if 'audio_data' not in request.FILES:
        return HttpResponseBadRequest("No audio file provided")

    thread_id = (request.POST.get('conversation_id') or '').strip() or str(uuid.uuid4())
    f = request.FILES['audio_data']
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in ('.webm', '.wav', '.ogg', '.mp4'):
        return HttpResponseBadRequest("Invalid file type")

    os.makedirs(VOICE_RECORDINGS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S%f")
    in_path = os.path.join(VOICE_RECORDINGS_DIR, f"agenttest_{ts}_in{ext}")
    out_name = f"agenttest_{ts}_out.mp3"
    out_path = os.path.join(VOICE_RECORDINGS_DIR, out_name)
    try:
        with open(in_path, 'wb') as fp:
            for chunk in f.chunks():
                fp.write(chunk)

        transcript = (transcribe_audio(in_path) or '').strip()
        resp_text = generate_response_with_agent(agent, request.user, transcript, thread_id)

        # Best-effort TTS; fall back to text-only if it fails.
        response_audio = ''
        try:
            synthesize_speech_elevenlabs(resp_text, out_name, voice_id=resolve_tts_voice_id(agent))
            with open(out_path, 'rb') as fp:
                response_audio = "data:audio/mpeg;base64," + base64.b64encode(fp.read()).decode('ascii')
        except Exception:
            response_audio = ''

        return JsonResponse({
            'transcript': transcript,
            'response': resp_text,
            'response_audio': response_audio,
        })
    finally:
        for p in (in_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass


@login_required
@admin_required
@require_POST
def agent_realtime_session(request, pk):
    """Mint an ephemeral Realtime session key for the agent test voice call.

    Called when the admin presses *Start* — never on page load — so the
    Realtime API is only used for actual conversations.
    """
    agent = get_object_or_404(Agent, pk=pk, kind=Agent.Kind.PROMPT,
                              realtime_enabled=True)
    return realtime_session_json(agent)
