"""Runtime configuration resolver.

Resolution order for a given environment key:
    1. The matching field on the ``SiteConfiguration`` singleton (if non-empty).
    2. ``os.getenv(key)``.
    3. ``settings.<KEY>`` (covers values with a defined default, e.g. BRAND_*).
    4. The supplied ``default``.

Boolean flags use a tri-state CharField where an empty value means "no override,
fall back to the environment".
"""
import os

from django.conf import settings

_TRUTHY = ("1", "true", "yes", "on")

# Environment key -> SiteConfiguration field name.
_STR_KEYS = {
    "SELF_REG_AGENT_NAME": "self_reg_agent_name",
    "TWILIO_ACCOUNT_SID": "twilio_account_sid",
    "TWILIO_AUTH_TOKEN": "twilio_auth_token",
    "PLATFORM_PHONE": "platform_phone",
    "OPENAI_API_KEY": "openai_api_key",
    "ELEVENLABS_API_KEY": "elevenlabs_api_key",
    "ELEVENLABS_VOICE_ID": "elevenlabs_voice_id",
    # Text to speech (see text_to_speech.md).
    "TTS_PROVIDER": "tts_provider",
    "AZURE_SPEECH_KEY": "azure_speech_key",
    "AZURE_SPEECH_REGION": "azure_speech_region",
    "AZURE_SPEECH_VOICE": "azure_speech_voice",
    "TWILIO_SMS_FROM": "twilio_sms_from",
    "SMS_TEMPLATE_START_INFECTION_SID": "sms_template_start_infection_sid",
    "SMS_TEMPLATE_START_INFECTION_TEXT": "sms_template_start_infection_text",
    "WHATSAPP_TEMPLATE_CARE_PLAN_SID": "whatsapp_template_care_plan_sid",
    "BRAND_NAME": "brand_name",
    "BRAND_LOGO": "brand_logo",
    "AGENT_ALLOWED_HOSTS": "agent_allowed_hosts",
    # Summarization prompts.
    "MEETING_SUMMARY_PROMPT": "meeting_summary_prompt",
    "TRANSCRIPT_SUMMARY_PROMPT": "transcript_summary_prompt",
    "TRANSCRIPT_MOMENTS_PROMPT": "transcript_moments_prompt",
    # Agent models / LLM providers.
    "DEFAULT_AGENT_MODEL": "default_agent_model",
    "SUMMARY_MODEL": "summary_model",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "GOOGLE_API_KEY": "google_api_key",
    "MISTRAL_API_KEY": "mistral_api_key",
    "DEEPSEEK_API_KEY": "deepseek_api_key",
    "AZURE_OPENAI_ENDPOINT": "azure_openai_endpoint",
    "AZURE_OPENAI_API_KEY": "azure_openai_api_key",
    "AZURE_OPENAI_API_VERSION": "azure_openai_api_version",
    "AZURE_ANTHROPIC_ENDPOINT": "azure_anthropic_endpoint",
    "AZURE_ANTHROPIC_API_KEY": "azure_anthropic_api_key",
    "AZURE_MISTRAL_ENDPOINT": "azure_mistral_endpoint",
    "AZURE_MISTRAL_API_KEY": "azure_mistral_api_key",
    "AZURE_DEEPSEEK_ENDPOINT": "azure_deepseek_endpoint",
    "AZURE_DEEPSEEK_API_KEY": "azure_deepseek_api_key",
    "AZURE_REALTIME_ENDPOINT": "azure_realtime_endpoint",
    "AZURE_REALTIME_API_KEY": "azure_realtime_api_key",
    "AZURE_REALTIME_DEPLOYMENT": "azure_realtime_deployment",
    "AZURE_REALTIME_VOICE": "azure_realtime_voice",
    "AZURE_REALTIME_WEBRTC_REGION": "azure_realtime_webrtc_region",
    # Embeddings for RAG-based prompt agents.
    "RAG_EMBEDDING_MODEL": "rag_embedding_model",
    "AZURE_EMBEDDING_DEPLOYMENT": "azure_embedding_deployment",
    # Outbound email (see email.md). SMTP_* rather than Django's EMAIL_HOST /
    # EMAIL_PORT / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD on purpose: those names
    # are real Django settings with non-empty defaults ('localhost', 25), so
    # falling through to settings would report SMTP as configured when nobody
    # configured it. The Django names are still read from .env as aliases below.
    "EMAIL_PROVIDER": "email_provider",
    "EMAIL_FROM": "email_from",
    "EMAIL_FROM_NAME": "email_from_name",
    "EMAIL_REPLY_TO": "email_reply_to",
    "AZURE_EMAIL_CONNECTION_STRING": "azure_email_connection_string",
    "AZURE_EMAIL_ENDPOINT": "azure_email_endpoint",
    "AZURE_EMAIL_ACCESS_KEY": "azure_email_access_key",
    "SMTP_HOST": "smtp_host",
    "SMTP_PORT": "smtp_port",
    "SMTP_USER": "smtp_user",
    "SMTP_PASSWORD": "smtp_password",
    "SMTP_SECURITY": "smtp_security",
    # Which channel meeting reminders go out on: 'whatsapp' or 'email'.
    "REMINDER_CHANNEL": "reminder_channel",
    # Sensei agents (see ConvAI.sensei).
    "SENSEI_API_URL": "sensei_api_url",
    "SENSEI_FUNCTION_KEY": "sensei_function_key",
    "SENSEI_USER_ID_SECRET": "sensei_user_id_secret",
    # Study enrolment (see participant_management.md).
    "ENROLMENT_LANDING_TEXT": "enrolment_landing_text",
    # Link Worker on WhatsApp (see link_worker_whatsapp.md).
    "LINK_WORKER_VOICE_PT_BR": "link_worker_voice_pt_br",
    "LINK_WORKER_AZURE_VOICE_PT_BR": "link_worker_azure_voice_pt_br",
}

# Integer settings, resolved like the strings above but coerced. Kept separate
# because a blank override has to fall through to the environment rather than
# become 0 — which for a rate limit would mean "locked out on the first try".
_INT_KEYS = {
    "ENROLMENT_CODE_WORDS": "enrolment_code_words",
    "ENROLMENT_CODE_ATTEMPT_LIMIT": "enrolment_code_attempt_limit",
}

_BOOL_KEYS = {
    "HIDE_MEETING_STEPS": "hide_meeting_steps",
    "ENABLE_AUTOMATIONS": "enable_automations",
    "SELF_REGISTRATION_ENABLED": "self_registration_enabled",
    "SEND_CARE_PLAN": "send_care_plan",
    "WHATSAPP_AUDIO_ENABLED": "whatsapp_audio_enabled",
    "PHONE_CALLS_ENABLED": "phone_calls_enabled",
    "USE_AZURE": "use_azure",
    "SENSEI_ENABLED": "sensei_enabled",
    "MESSAGE_EXPORT_ENABLED": "message_export_enabled",
    "CONVERSATION_DOWNLOAD_ENABLED": "conversation_download_enabled",
    "CONVERSATION_PRIVACY_ENABLED": "conversation_privacy_enabled",
    "STUDY_ENROLMENT_ENABLED": "study_enrolment_enabled",
    "LINK_WORKER_V2_ENABLED": "link_worker_v2_enabled",
    "LINK_WORKER_WHATSAPP_ENABLED": "link_worker_whatsapp_enabled",
    "ENROLMENT_REQUIRE_DOB": "enrolment_require_dob",
    "ENROLMENT_AUTO_APPROVE": "enrolment_auto_approve",
}


def _override(field_name):
    """Return the singleton's field value, or None if the DB is unavailable."""
    try:
        from .models import SiteConfiguration
        return getattr(SiteConfiguration.load(), field_name, None)
    except Exception:
        # DB not ready (e.g. during migrations) — behave as if no override.
        return None


# Renamed env vars -> their deprecated legacy names (still read as a fallback).
# Only consulted through os.getenv, never through settings — which is what makes
# the SMTP aliases safe despite Django defining EMAIL_HOST and friends itself.
_LEGACY_ENV = {
    "SMTP_HOST": "EMAIL_HOST",
    "SMTP_PORT": "EMAIL_PORT",
    "SMTP_USER": "EMAIL_HOST_USER",
    "SMTP_PASSWORD": "EMAIL_HOST_PASSWORD",
    "EMAIL_FROM": "DEFAULT_FROM_EMAIL",
}


def _env(key, default=None):
    val = os.getenv(key)
    if (val is None or val == "") and key in _LEGACY_ENV:
        val = os.getenv(_LEGACY_ENV[key])
    if val is None or val == "":
        val = getattr(settings, key, None)
    if val is None:
        return default
    return val


def get_setting(key, default=None):
    """Resolve a string setting: DB override, then env, then settings, then default."""
    field = _STR_KEYS.get(key)
    if field:
        ov = _override(field)
        if ov:
            return ov
    return _env(key, default)


def get_int(key, default=0):
    """Resolve an integer setting: DB override, then env, then settings, then default.

    These live on the singleton as real integer fields with their own defaults, so
    the DB is normally the source of truth; the env fallback only carries the value
    when the DB cannot be read (during migrations, say). A non-positive or
    unparseable value is treated as absent rather than honoured — a rate limit of
    zero would lock everyone out on their first attempt.
    """
    field = _INT_KEYS.get(key)
    if field:
        ov = _override(field)
        try:
            if ov is not None and int(ov) > 0:
                return int(ov)
        except (TypeError, ValueError):
            pass
    raw = _env(key)
    try:
        if raw is not None and int(raw) > 0:
            return int(raw)
    except (TypeError, ValueError):
        pass
    return default


def get_bool(key, default=False):
    """Resolve a boolean flag with tri-state override semantics."""
    field = _BOOL_KEYS.get(key)
    if field:
        ov = _override(field)
        if ov in ("1", "0"):
            return ov == "1"
    raw = _env(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in _TRUTHY


def phone_calls_enabled() -> bool:
    """Can this installation place phone calls? On unless switched off.

    Settings → General, or PHONE_CALLS_ENABLED in .env. Off, nothing offers a
    call, new meetings default to in person, and the call endpoints refuse —
    so a navigator cannot ring a client from a platform that was never meant
    to. Calls already made, and their recordings, stay readable.
    """
    return get_bool("PHONE_CALLS_ENABLED", default=True)


def brand_name():
    return get_setting("BRAND_NAME", getattr(settings, "BRAND_NAME", ""))


def brand_logo():
    return get_setting("BRAND_LOGO", getattr(settings, "BRAND_LOGO", ""))


def brand_logo_uploaded_url():
    """Return the URL of an uploaded logo file, or None if none is set."""
    try:
        from .models import SiteConfiguration
        f = SiteConfiguration.load().brand_logo_file
        return f.url if f else None
    except Exception:
        return None
