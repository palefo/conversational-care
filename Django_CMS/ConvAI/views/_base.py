# ── Standard library ────────────────────────────────────────────────────────────
import base64
import calendar
import io
import json
import os
import uuid
import datetime as dt
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus, urlencode
from uuid import UUID

# ── Third-party ─────────────────────────────────────────────────────────────────
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
import qrcode

# ── Django ─────────────────────────────────────────────────────────────────────
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.paginator import Paginator
from django.db.models import F, Q, Count
from django.db.models.functions import TruncDate
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseServerError,
    JsonResponse,
)
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse, path
from django.utils import timezone, formats
from django.utils.text import slugify
from django.utils.translation import gettext as _
from ..site_config import get_setting, get_bool
from ..roles import is_admin, is_navigator, is_tester, admin_required, navigator_required, tester_required
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from rest_framework.authtoken.models import Token

# ── Local apps ─────────────────────────────────────────────────────────────────
from ..forms import (
    CarePlanForm,
    MeetingFilterForm,
    MeetingForm,
    PatientDetailsForm,
    ProtocolAnswerForm,
    GeneralConfigForm,
    SenseiConfigForm,
    IntegrationsConfigForm,
    MessagingConfigForm,
    EmailConfigForm,
    BrandingConfigForm,
    AgentConfigForm,
    PromptsConfigForm,
)
from ..models import (
    ContactTerm,
    Answer,
    CallLeg,
    CallRecording,
    Conversation,
    Meeting,
    Message,
    Note,
    Patient,
    Caregiver,
    Protocol,
    Question,
    SelfRegistration,
    Agent,
    Alert,
    SeenMark,
    SiteConfiguration,
    SummaryEdit,
)
from ..utils_conversation_classification import (
    build_message_rows_for_conv,
    classify_conversation_with_llm,
    display_label,
)
from ..utils import (
    generate_response_langgraph,
    generate_response_with_agent,
    get_platform_phone,
    get_path_audio,
    get_recordings_from_twilio,
    make_phone_conference,
    process_received_message,
    process_message_for_patient,
    start_automation,
    end_automation,
    automation_context,
    automation_turn,
    save_message,
    patient_message_q,
    send_whatsapp_reminder,
    send_meeting_reminder,
    can_send_reminder,
    synthesize_speech_elevenlabs,
    transcribe_audio,
    send_whatsapp_text,
    send_whatsapp_text_result,
    send_sms_text,
    send_sms_with_template,
    append_system_note_to_langgraph,
    _get_or_create_thread,
    is_audio_content_type,
    ext_for_content_type,
    download_twilio_media,
    save_bytes,
    build_signed_download_token,
    verify_and_consume_download_token,
    synthesize_speech_elevenlabs,
    resolve_tts_voice_id,
    send_whatsapp_template_with_media,
)

from twilio.request_validator import RequestValidator

# Single source of truth: resolved in settings to a private dir on the media
# volume (falls back to env override). See file_storage.md.
VOICE_RECORDINGS_DIR = settings.VOICE_RECORDINGS_DIR
# WHATSAPP_AUDIO_ENABLED is resolved live via get_bool("WHATSAPP_AUDIO_ENABLED")
# at each use site so DB overrides take effect without a restart.

import re

_CONFIG_FORMS = {
    "general": GeneralConfigForm,
    "integrations": IntegrationsConfigForm,
    "messaging": MessagingConfigForm,
    "email": EmailConfigForm,
    "branding": BrandingConfigForm,
    "agents": AgentConfigForm,
    "prompts": PromptsConfigForm,
    "sensei": SenseiConfigForm,
}


__all__ = ['admin_required', 'navigator_required', 'tester_required', 'is_admin', 'is_navigator', 'is_tester', 'Agent', 'AgentConfigForm', 'Alert', 'Answer', 'BrandingConfigForm', 'CallLeg', 'CallRecording', 'CarePlanForm', 'Caregiver', 'ContactTerm', 'Conversation', 'Count', 'F', 'FileResponse', 'EmailConfigForm', 'GeneralConfigForm', 'Http404', 'HttpResponse', 'HttpResponseBadRequest', 'HttpResponseForbidden', 'HttpResponseServerError', 'IntegrationsConfigForm', 'JsonResponse', 'LoginView', 'Meeting', 'MeetingFilterForm', 'MeetingForm', 'Message', 'MessagingConfigForm', 'Note', 'Paginator', 'Patient', 'PatientDetailsForm', 'PromptsConfigForm', 'Protocol', 'ProtocolAnswerForm', 'Q', 'Question', 'RequestValidator', 'slugify', 'SeenMark', 'SelfRegistration', 'SenseiConfigForm', 'SiteConfiguration', 'SummaryEdit', 'Token', 'TruncDate', 'UUID', 'VOICE_RECORDINGS_DIR', '_', '_CONFIG_FORMS', '_get_or_create_thread', 'append_system_note_to_langgraph', 'base64', 'build_message_rows_for_conv', 'build_signed_download_token', 'calendar', 'classify_conversation_with_llm', 'csrf_exempt', 'display_label', 'date', 'datetime', 'download_twilio_media', 'dt', 'ext_for_content_type', 'formats', 'generate_response_langgraph', 'generate_response_with_agent', 'get_bool', 'get_platform_phone', 'get_object_or_404', 'get_path_audio', 'get_recordings_from_twilio', 'get_setting', 'io', 'is_audio_content_type', 'json', 'load_dotenv', 'login_required', 'logout', 'make_phone_conference', 'messages', 'os', 'path', 'process_received_message', 'process_message_for_patient', 'patient_message_q', 'start_automation', 'end_automation', 'automation_context', 'automation_turn', 'qrcode', 'quote_plus', 're', 'redirect', 'relativedelta', 'render', 'require_GET', 'require_POST', 'resolve_tts_voice_id', 'reverse', 'save_bytes', 'save_message', 'send_sms_text', 'send_sms_with_template', 'send_whatsapp_reminder', 'send_meeting_reminder', 'can_send_reminder', 'send_whatsapp_template_with_media', 'send_whatsapp_text', 'send_whatsapp_text_result', 'settings', 'staff_member_required', 'synthesize_speech_elevenlabs', 'timedelta', 'timezone', 'transcribe_audio', 'urlencode', 'uuid', 'verify_and_consume_download_token']
