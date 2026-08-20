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
}

_BOOL_KEYS = {
    "HIDE_MEETING_STEPS": "hide_meeting_steps",
    "ENABLE_AUTOMATIONS": "enable_automations",
    "SELF_REGISTRATION_ENABLED": "self_registration_enabled",
    "SEND_CARE_PLAN": "send_care_plan",
    "WHATSAPP_AUDIO_ENABLED": "whatsapp_audio_enabled",
    "USE_AZURE": "use_azure",
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
_LEGACY_ENV = {}


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
