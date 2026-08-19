"""Azure OpenAI GPT Realtime (speech-to-speech) session broker.

Prompt-based agents with ``realtime_enabled`` talk over the Realtime API via
WebRTC, straight from the browser to Azure. The Azure API key never reaches the
client: the browser asks this module (through a view) for a short-lived
*ephemeral* session key, minted only when the user actually starts a call — so
the Realtime API is never touched by merely opening the page.

Config is resolved through ``site_config`` (Settings → Agents → Azure OpenAI
Realtime, falling back to .env):

    AZURE_REALTIME_ENDPOINT     resource endpoint; blank reuses AZURE_OPENAI_ENDPOINT
    AZURE_REALTIME_API_KEY      blank reuses AZURE_OPENAI_API_KEY
    AZURE_REALTIME_DEPLOYMENT   the gpt-realtime deployment name (required)
    AZURE_REALTIME_VOICE        output voice (default 'marin')

Prefers the Azure OpenAI v1 GA surface (mirrors the public OpenAI Realtime API):
    POST {endpoint}/openai/v1/realtime/client_secrets   → ephemeral key
    POST {endpoint}/openai/v1/realtime/calls?model=...  → WebRTC SDP exchange

Not every resource exposes that surface (it 404s e.g. on older/most-region
resources). Those still ship the *preview* surface, which we fall back to:
    POST {endpoint}/openai/realtimeapi/sessions?api-version=2025-04-01-preview
    WebRTC SDP goes to the regional preview gateway instead, which is why the
    fallback additionally needs AZURE_REALTIME_WEBRTC_REGION:
    https://{region}.realtimeapi-preview.ai.azure.com/v1/realtimertc?model=...
"""
from __future__ import annotations

import requests

from .site_config import get_setting

# Ephemeral keys only need to outlive the WebRTC handshake; once the call is
# established the session stays up without it.
_EPHEMERAL_TTL_SECONDS = 600
_HTTP_TIMEOUT = 15
_LEGACY_API_VERSION = "2025-04-01-preview"


class RealtimeNotConfigured(Exception):
    """Raised when the Azure Realtime settings are incomplete."""


class RealtimeUpstreamError(Exception):
    """Raised when Azure rejects the session request; message is admin-safe."""


def _azure_error_detail(resp) -> str:
    """Short human-readable error out of an Azure error response body."""
    try:
        detail = (resp.json().get("error") or {}).get("message") or ""
    except Exception:
        detail = (resp.text or "")
    return " ".join(detail.split())[:300]


def _endpoint() -> str:
    ep = (get_setting("AZURE_REALTIME_ENDPOINT")
          or get_setting("AZURE_OPENAI_ENDPOINT") or "").strip()
    return ep.rstrip("/")


def _api_key() -> str:
    return (get_setting("AZURE_REALTIME_API_KEY")
            or get_setting("AZURE_OPENAI_API_KEY") or "").strip()


def realtime_configured() -> bool:
    """True when a realtime call can be started (endpoint, key, deployment)."""
    return bool(_endpoint() and _api_key()
                and (get_setting("AZURE_REALTIME_DEPLOYMENT") or "").strip())


def mint_realtime_session(agent) -> dict:
    """Create an ephemeral Realtime session for ``agent``.

    Returns ``{"client_secret", "webrtc_url", "expires_at"}`` for the browser
    to run the WebRTC SDP exchange itself. The agent's ``system_prompt`` is
    pinned server-side as the session instructions, so the client cannot swap
    the persona.
    """
    endpoint = _endpoint()
    api_key = _api_key()
    deployment = (get_setting("AZURE_REALTIME_DEPLOYMENT") or "").strip()
    if not (endpoint and api_key and deployment):
        raise RealtimeNotConfigured(
            "Azure OpenAI Realtime is not configured (endpoint, API key and "
            "deployment are required — see Settings → Agents)."
        )
    voice = (get_setting("AZURE_REALTIME_VOICE") or "").strip() or "marin"

    resp = requests.post(
        f"{endpoint}/openai/v1/realtime/client_secrets",
        headers={"api-key": api_key},
        json={
            "expires_after": {"anchor": "created_at",
                              "seconds": _EPHEMERAL_TTL_SECONDS},
            "session": {
                "type": "realtime",
                "model": deployment,
                "instructions": agent.system_prompt or "",
                "audio": {"output": {"voice": voice}},
            },
        },
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code == 404:
        # Resource doesn't expose the GA v1 surface — use the preview one.
        return _mint_legacy_session(endpoint, api_key, deployment, voice, agent)
    if resp.status_code >= 400:
        raise RealtimeUpstreamError(
            f"Azure Realtime session request failed "
            f"(HTTP {resp.status_code}): {_azure_error_detail(resp)}")
    data = resp.json()
    return {
        "client_secret": data["value"],
        "expires_at": data.get("expires_at"),
        "webrtc_url": f"{endpoint}/openai/v1/realtime/calls?model={deployment}",
    }


def _mint_legacy_session(endpoint, api_key, deployment, voice, agent) -> dict:
    """Preview-surface fallback: /openai/realtimeapi/sessions + regional WebRTC."""
    region = (get_setting("AZURE_REALTIME_WEBRTC_REGION") or "").strip().lower()
    if not region:
        raise RealtimeNotConfigured(
            "This Azure resource only exposes the preview Realtime API, which "
            "needs the resource's region for the WebRTC endpoint (e.g. "
            "'swedencentral' or 'eastus2'). Set the Realtime WebRTC region "
            "under Settings → Agents."
        )
    resp = requests.post(
        f"{endpoint}/openai/realtimeapi/sessions"
        f"?api-version={_LEGACY_API_VERSION}",
        headers={"api-key": api_key},
        json={
            "model": deployment,
            "voice": voice,
            "instructions": agent.system_prompt or "",
        },
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RealtimeUpstreamError(
            f"Azure Realtime session request failed "
            f"(HTTP {resp.status_code}): {_azure_error_detail(resp)}")
    data = resp.json()
    secret = data.get("client_secret") or {}
    return {
        "client_secret": secret.get("value"),
        "expires_at": secret.get("expires_at"),
        "webrtc_url": (f"https://{region}.realtimeapi-preview.ai.azure.com"
                       f"/v1/realtimertc?model={deployment}"),
    }
