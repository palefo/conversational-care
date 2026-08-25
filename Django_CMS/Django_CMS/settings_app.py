import os 
from .settings_default import *  # Import shared settings


APP_NAME = "ConvAI"
# Templates are discovered via APP_DIRS (ConvAI/templates/), organised into
# functional subfolders (dashboard/, calls/, patients/, ...).
STATICFILES_DIRS = [
    BASE_DIR / 'ConvAI/static/app',
    ]

# Hosts and trusted origins are configured per deployment via .env (comma-separated),
# so no brand/customer domains are hard-coded here.
#   ALLOWED_HOSTS=example.com,www.example.com
#   CSRF_TRUSTED_ORIGINS=https://example.com,https://www.example.com
_DEFAULT_HOSTS = "localhost,127.0.0.1,0.0.0.0"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", _DEFAULT_HOSTS).split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
SECURE_SSL_REDIRECT = False

LOGIN_URL = '/login/'
# F9 fix: was '' → reverse('') NoReverseMatch (HTTP 500) on Navigator login.
LOGIN_REDIRECT_URL = 'dashboard'

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


USE_TZ = True
TIME_ZONE = os.getenv("PLATFORM_TZ", "Europe/London")  
USE_THOUSAND_SEPARATOR = False

INSTALLED_APPS += ["markdownify", "widget_tweaks"]
# MC-001/F6 fix: DEBUG defaults to False; only on when DJANGO_DEBUG is truthy.
DEBUG = os.getenv("DJANGO_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")


# Internationalization
USE_I18N = True

# Which language does the platform run in?
_PL = (os.getenv("PLATFORM_LANG", "English") or "").strip().lower()

if _PL.startswith(("es", "spa", "span")):
    LANGUAGE_CODE = "es-pe"
elif _PL.startswith(("pt", "por", "br")):
    LANGUAGE_CODE = "pt-br"
elif _PL.startswith(("it", "ita")):
    LANGUAGE_CODE = "it"
elif _PL.startswith(("ko", "kor")):
    LANGUAGE_CODE = "ko"
elif _PL.startswith(("zh", "chi", "man")):
    LANGUAGE_CODE = "zh-hans"
else:
    LANGUAGE_CODE = "en-gb"

LANGUAGES = [
    ("es-pe", "Español"),
    ("pt-br", "Português"),
    ("en-gb", "English"),
    ("it", "Italiano"),
    ("ko", "한국어"),
    ("zh-hans", "简体中文"),
]

# Optional but handy to avoid any stale cookie affecting future flips:
LANGUAGE_COOKIE_NAME = f"django_language_{LANGUAGE_CODE}"

# Make sure Django finds your compiled .mo files
LOCALE_PATHS = [
    BASE_DIR / "locale",          # Django_CMS/locale/<lang>/LC_MESSAGES/*.mo
    BASE_DIR / "ConvAI" / "locale",  # if you keep app-local translations too
]

MARKDOWNIFY = {
    "default": {
        "WHITELIST_TAGS": [
            "p", "br", "ul", "ol", "li", "strong", "em", "a", "blockquote",
            "code", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "hr"
        ],
        "WHITELIST_ATTRS": {
            "*": ["href", "title", "src", "alt"]
        },
        "MARKDOWN_EXTENSIONS": [
            "markdown.extensions.extra",       # GitHub-style extras (tables, etc.)
            "markdown.extensions.nl2br"        # Treat newlines as <br> breaks
        ],
        # "BLEACH": False,  # (Optional: disable sanitization entirely if content is trusted)
    }
}

DATABASES = {
    'default': {
        "ENGINE":   os.getenv("DB_ENGINE"),
        "NAME":     os.getenv("DB_NAME"),
        "USER":     os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST":     os.getenv("DB_HOST"),
        "PORT":     os.getenv("DB_PORT"),
        "OPTIONS":  # If using psycopg3 it should require sslmode:
        {
            "sslmode": os.getenv("DB_SSLMODE", "require")
        },
    }
}

# --- Round 2 hardening ---
# AS-09/F1 + R3-01: dedicated download-token signing key. MUST come from the
# environment; if unset, fall back to a random per-process key (NO usable committed
# default — a value in source could be used to forge tokens).
import secrets as _secrets
DOWNLOAD_TOKEN_KEY = os.getenv("DOWNLOAD_TOKEN_KEY") or _secrets.token_urlsafe(48)
# AS-06/F8: SSRF allow-list of permitted LangGraph agent hosts.
AGENT_ALLOWED_HOSTS = [h.strip() for h in os.getenv("AGENT_ALLOWED_HOSTS", "mock-agent").split(",") if h.strip()]
# MC-007: keep the CSRF cookie out of JavaScript's reach.
CSRF_COOKIE_HTTPONLY = True
# MC-004: security headers (HSTS effective behind HTTPS edge; CSP via middleware below).
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
MIDDLEWARE = MIDDLEWARE + ["ConvAI.security_headers.SecurityHeadersMiddleware"]

### API ###

INSTALLED_APPS += [
    "rest_framework",
    "rest_framework.authtoken",
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.UserRateThrottle",   # general per-user throttle
        "rest_framework.throttling.ScopedRateThrottle", # per-endpoint scopes
    ],
    "DEFAULT_THROTTLE_RATES": {
        "user": "120/min",       # overall
        "messages": "60/min",    # our /messages endpoint
    },
}

BRAND_LOGO = os.getenv("BRAND_LOGO", "app/images/conversational-care_logo.png")
BRAND_NAME = os.getenv("BRAND_NAME", "conversational-care.ai")

# Inbound Twilio (WhatsApp/SMS) webhook path. Configurable so the endpoint is
# not tied to a brand-specific prefix. No leading/trailing slash.
TWILIO_WEBHOOK_PATH = os.getenv("TWILIO_WEBHOOK_PATH", "webhooks/whatsapp").strip("/")
HIDE_MEETING_STEPS = os.getenv("HIDE_MEETING_STEPS", "0").strip().lower() in ("1", "true", "yes", "on")
ENABLE_AUTOMATIONS = os.getenv("ENABLE_AUTOMATIONS", "0").strip().lower() in ("1", "true", "yes", "on")


# --- Async WhatsApp/SMS reply toggle (from .env) ---
# When ON, inbound Twilio webhooks are answered immediately and the actual reply
# is generated on an in-process worker pool (no Redis/broker needed). See
# async_replies.md for the full design.
ASYNC_WHATSAPP_REPLY = (
    os.getenv("ASYNC_WHATSAPP_REPLY", "0").strip().lower() in ("1", "true", "yes", "on")
)

# Number of background worker threads used to deliver replies asynchronously.
# Higher values allow more concurrent replies at the cost of more memory/CPU per
# web process. Ignored when ASYNC_WHATSAPP_REPLY is off.
WHATSAPP_WORKERS = max(1, int(os.getenv("WHATSAPP_WORKERS", "2") or "2"))

# Number of background worker threads used to ingest RAG documents (read →
# chunk → embed). Separate from WHATSAPP_WORKERS so a long upload cannot sit in
# front of a waiting WhatsApp reply. Two is plenty: the work is dominated by
# waiting on the embeddings API, and each thread holds a DB connection.
RAG_WORKERS = max(1, int(os.getenv("RAG_WORKERS", "2") or "2"))


# --- Outbound email (see email.md) ---
# One backend for everything: it picks Azure Communication Services or SMTP at
# send time from the live configuration, so Django's own password-reset mail and
# the platform's reminders travel the same way, and switching provider in
# Settings → Email needs no restart.
EMAIL_BACKEND = "ConvAI.mailer.PlatformEmailBackend"
# Read once at boot and only used as a placeholder — PlatformEmailBackend
# replaces it with the configured sender on every message.
DEFAULT_FROM_EMAIL = os.getenv("EMAIL_FROM", "") or "no-reply@localhost"
SERVER_EMAIL = DEFAULT_FROM_EMAIL
# How long a password-reset link stays valid. Three hours: long enough to
# survive a message sitting unread over lunch, short enough that a forwarded or
# archived mail is not a standing key to the account.
PASSWORD_RESET_TIMEOUT = int(os.getenv("PASSWORD_RESET_TIMEOUT", "10800") or "10800")
