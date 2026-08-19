"""MC-004/MC-010 fix: add Content-Security-Policy, Permissions-Policy and COEP
response headers (Django 5.1 has no built-in CSP setting; django-csp not installed)."""

CSP = (
    "default-src 'self'; "
    "script-src 'self' https://code.jquery.com https://cdn.jsdelivr.net 'unsafe-inline'; "
    # cdnjs serves Font Awesome (used by the SimpleMDE markdown-editor toolbar);
    # its CSS is loaded from style-src and its icon fonts from font-src.
    "style-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
    "img-src 'self' data:; "
    # Real-time voice agents run the WebRTC SDP handshake against the Azure
    # OpenAI resource straight from the browser (authorised by a short-lived
    # ephemeral key minted server-side). Without an explicit connect-src the
    # default-src 'self' fallback blocks that fetch.
    # realtimeapi-preview.ai.azure.com is the regional WebRTC gateway used by
    # resources that only expose the preview Realtime API.
    "connect-src 'self' https://*.openai.azure.com https://*.cognitiveservices.azure.com https://*.services.ai.azure.com https://*.realtimeapi-preview.ai.azure.com; "
    # Voice chat plays recorded audio (blob:) and TTS responses (data:). Without
    # an explicit media-src these fall back to default-src 'self' and are blocked.
    "media-src 'self' data: blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'"
)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        resp = self.get_response(request)
        resp.setdefault("Content-Security-Policy", CSP)
        # microphone=(self): the voice chat (test-user chat, agent test) records
        # audio from the app's own pages. An empty allowlist microphone=() would
        # block getUserMedia even on HTTPS with the user's permission granted.
        resp.setdefault("Permissions-Policy", "geolocation=(), microphone=(self), camera=()")
        resp.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
        resp.setdefault("X-Content-Type-Options", "nosniff")
        return resp
