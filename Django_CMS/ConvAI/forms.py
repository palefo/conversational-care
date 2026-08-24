from django import forms
from .models import Meeting, Patient, Question, Answer, Agent, SiteConfiguration
from datetime import date
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _

class MeetingForm(forms.ModelForm):
    scheduled_time = forms.DateTimeField(
        widget=forms.DateTimeInput(
            attrs={
                'type': 'datetime-local',
                'class': 'form-control',
            }
        ),
        input_formats=['%Y-%m-%dT%H:%M'],
        label=_("Scheduled time"),
    )

    class Meta:
        model = Meeting
        fields = ['patient', 'modality', 'location', 'type',
                  'scheduled_protocol', 'scheduled_time']
        widgets = {
            'patient': forms.Select(attrs={'class': 'form-select'}),
            'modality': forms.Select(attrs={'class': 'form-select'}),
            'location': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': _("Address or place"),
            }),
            'type': forms.Select(attrs={'class': 'form-select'}),
            'scheduled_protocol': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'patient': _('Client'),
            'modality': _('How'),
            'location': _('Where'),
            'type': _('Type'),
            'scheduled_protocol': _('Scheduled protocol'),
        }
        # The model's help_text is written for whoever reads the schema: it
        # spells out integer codes and is partly in Spanish. None of it belongs
        # on a form a navigator fills in, and the labels already say enough.
        help_texts = {
            'location': '',
            'modality': '',
            'type': '',
            'scheduled_protocol': '',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Meeting.Protocol's labels are placeholders ("3. Protocol 3"). Where a
        # real Protocol record exists, offer the name a navigator recognises —
        # the same substitution the panel and the lists already make.
        from .models import Protocol as ProtocolRecord
        titles = dict(ProtocolRecord.objects.values_list('number', 'title'))
        field = self.fields.get('scheduled_protocol')
        if field and titles:
            field.choices = [
                (value, f"{value}. {titles[value]}" if titles.get(value) else label)
                for value, label in field.choices
            ]

    def clean(self):
        """A meeting you turn up to needs somewhere to turn up."""
        data = super().clean()
        if (data.get('modality') == Meeting.Modality.IN_PERSON
                and not (data.get('location') or '').strip()):
            self.add_error('location', _("Say where the meeting takes place."))
        return data



class MeetingFilterForm(forms.Form):
    patient = forms.ModelChoiceField(
        queryset=Patient.objects.select_related('caregiver').order_by('name', 'lastname'),
        required=False,
        label=_("Client – Caregiver"),
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    date = forms.DateField(
        required=False,
        label=_("Date"),
        widget=forms.DateInput(attrs={
            'type': 'date',
            'class': 'form-control',
        })
    )
    status = forms.ChoiceField(
        required=False,
        label=_("State"),
        choices=[('', _('All'))] + list(Meeting.Status.choices),
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    type = forms.ChoiceField(
        required=False,
        label=_("Call type"),
        choices=[('', _('All'))] + list(Meeting.MeetingType.choices),
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # personalizar etiquetas de paciente–cuidador en el dropdown
        self.fields['patient'].label_from_instance = lambda obj: (
            f"{obj.name} {obj.lastname}  –  {obj.caregiver.name} {obj.caregiver.lastname}"
            if obj.caregiver else
            f"{obj.name} {obj.lastname}  –  —"
        )

class PatientDetailsForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['details_editable']
        widgets = {
            'details_editable': forms.Textarea(
                attrs={
                    'class': 'form-control markdown-editor',
                    'rows': 10,
                    'placeholder': _('Write the details here in Markdown...')
                }
            ),
        }
        help_texts = {
            'details_editable': _('Supports Markdown syntax.')
        }

class CarePlanForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ["care_plan"]
        widgets = {
            "care_plan": forms.ClearableFileInput(attrs={"class": "form-control"})
        }
        labels = {
            "care_plan": _("Care plan (PDF)")
        }
        help_texts = {
            "care_plan": _("PDF files only, maximum 5 MB.")
        }

    def clean_care_plan(self):
        pdf = self.cleaned_data.get("care_plan")
        if not pdf:
            return None

        # 1) Must be PDF
        if not pdf.name.lower().endswith(".pdf"):
            raise forms.ValidationError(_("The file must have a .pdf extension."))

        # 2) Validate MIME (optional, but recommended)
        if pdf.content_type != "application/pdf":
            raise forms.ValidationError(_("The file must be a valid PDF."))

        # 3) Limit size to 5 MB
        if pdf.size > 5 * 1024 * 1024:
            raise forms.ValidationError(_("The PDF cannot exceed 5 MB."))

        return pdf
    


class ProtocolAnswerForm(forms.Form):
    """
    Dynamically builds a textarea for every question
    belonging to a given protocol.
    """
    def __init__(self, *, meeting, protocol, **kwargs):
        super().__init__(**kwargs)
        self.meeting   = meeting
        self.protocol  = protocol

        existing = {
            a.question_id: a for a in
            Answer.objects.filter(meeting=meeting,
                                  question__protocol=protocol)
        }

        for q in protocol.questions.all():
            self.fields[f"q_{q.id}"] = forms.CharField(
                label="",  # label rendered from markdown, not field label
                required=False,
                widget=forms.Textarea(attrs={"rows": 3}),
                initial=existing.get(q.id).response if existing.get(q.id) else ""
            )

    def save(self):
        """
        Creates / updates / deletes Answer rows so that
        only non-empty answers remain.
        """
        for field_name, value in self.cleaned_data.items():
            q_id = int(field_name.split("_")[1])
            value = value.strip()

            try:
                ans = Answer.objects.get(
                    meeting=self.meeting, question_id=q_id
                )
            except Answer.DoesNotExist:
                ans = None

            if value:
                if ans:
                    # A navigator editing an answer makes it theirs, so the
                    # "came back by text" tint goes with the change. Only an
                    # untouched reply should still read as the caregiver's.
                    changed = ans.response != value
                    ans.response = value
                    if changed:
                        ans.by_text = False
                    ans.save(update_fields=["response", "by_text"])
                else:
                    Answer.objects.create(
                        meeting=self.meeting,
                        question_id=q_id,
                        response=value
                    )
            else:
                # user cleared text → delete stored answer
                if ans:
                    ans.delete()


# --------------------------------------------------------------------------
# Settings page: runtime configuration override forms (one per tab section).
# All bind to the SiteConfiguration singleton.
# --------------------------------------------------------------------------

_SELECT = {"class": "form-select"}
_INPUT = {"class": "form-control"}


class SecretPreserveMixin:
    """Renders secret fields as empty password inputs; a blank submission keeps
    the stored value instead of clearing it."""
    secret_fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._secret_initial = {
            f: (getattr(self.instance, f, "") or "") for f in self.secret_fields
        }
        for f in self.secret_fields:
            self.fields[f].required = False
            has_value = bool(self._secret_initial[f])
            self.fields[f].widget = forms.PasswordInput(
                render_value=False,
                attrs={
                    "class": "form-control",
                    "autocomplete": "new-password",
                    "placeholder": _("•••• set (leave blank to keep)") if has_value else "",
                },
            )

    def clean(self):
        cleaned = super().clean()
        for f in self.secret_fields:
            if not cleaned.get(f):
                cleaned[f] = self._secret_initial[f]
        return cleaned


class GeneralConfigForm(forms.ModelForm):
    class Meta:
        model = SiteConfiguration
        fields = [
            "hide_meeting_steps", "enable_automations", "self_registration_enabled",
            "self_reg_agent_name", "send_care_plan", "whatsapp_audio_enabled",
        ]
        labels = {
            "hide_meeting_steps": _("Hide meeting steps"),
            "enable_automations": _("Enable automations"),
            "self_registration_enabled": _("Enable self-registration"),
            "self_reg_agent_name": _("Self-registration agent"),
            "send_care_plan": _("Send care plan"),
            "whatsapp_audio_enabled": _("Enable WhatsApp audio"),
        }
        widgets = {
            "hide_meeting_steps": forms.Select(attrs=_SELECT),
            "enable_automations": forms.Select(attrs=_SELECT),
            "self_registration_enabled": forms.Select(attrs=_SELECT),
            "send_care_plan": forms.Select(attrs=_SELECT),
            "whatsapp_audio_enabled": forms.Select(attrs=_SELECT),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Offer the self-registration agent as a dropdown of existing agents.
        agent_names = list(Agent.objects.order_by("name").values_list("name", flat=True))
        choices = [("", _("Use .env default"))] + [(n, n) for n in agent_names]
        current = self.instance.self_reg_agent_name
        if current and current not in agent_names:
            choices.append((current, current))
        self.fields["self_reg_agent_name"] = forms.ChoiceField(
            choices=choices, required=False, label=_("Self-registration agent"),
            widget=forms.Select(attrs=_SELECT),
            initial=current,
        )


class IntegrationsConfigForm(SecretPreserveMixin, forms.ModelForm):
    secret_fields = ("twilio_account_sid", "twilio_auth_token", "openai_api_key", "elevenlabs_api_key")

    class Meta:
        model = SiteConfiguration
        fields = [
            "twilio_account_sid", "twilio_auth_token", "platform_phone",
            "openai_api_key", "elevenlabs_api_key", "elevenlabs_voice_id",
        ]
        labels = {
            "twilio_account_sid": _("Twilio Account SID"),
            "twilio_auth_token": _("Twilio Auth Token"),
            "platform_phone": _("Platform phone number"),
            "openai_api_key": _("OpenAI API key"),
            "elevenlabs_api_key": _("ElevenLabs API key"),
            "elevenlabs_voice_id": _("ElevenLabs voice ID"),
        }
        widgets = {
            "platform_phone": forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}),
            "elevenlabs_voice_id": forms.TextInput(attrs=_INPUT),
        }


class MessagingConfigForm(forms.ModelForm):
    class Meta:
        model = SiteConfiguration
        fields = [
            "twilio_sms_from", "sms_template_start_infection_sid",
            "sms_template_start_infection_text", "whatsapp_template_care_plan_sid",
        ]
        labels = {
            "twilio_sms_from": _("SMS sender number"),
            "sms_template_start_infection_sid": _("Infection alert SMS template SID"),
            "sms_template_start_infection_text": _("Infection alert SMS text"),
            "whatsapp_template_care_plan_sid": _("Care plan WhatsApp template SID"),
        }
        widgets = {
            "twilio_sms_from": forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}),
            "sms_template_start_infection_sid": forms.TextInput(attrs={**_INPUT, "placeholder": "HX…"}),
            "sms_template_start_infection_text": forms.Textarea(attrs={**_INPUT, "rows": 3}),
            "whatsapp_template_care_plan_sid": forms.TextInput(attrs={**_INPUT, "placeholder": "HX…"}),
        }


class PromptsConfigForm(forms.ModelForm):
    """Base prompts used to post-process meeting protocols and call transcripts.

    Blank falls back to the shipped defaults in ConvAI.default_prompts.
    """
    class Meta:
        model = SiteConfiguration
        fields = ["meeting_summary_prompt", "transcript_summary_prompt",
                  "transcript_moments_prompt"]
        labels = {
            "meeting_summary_prompt": _("Meeting protocol summary prompt"),
            "transcript_summary_prompt": _("Call transcript summary prompt"),
            "transcript_moments_prompt": _("Call transcript key moments prompt"),
        }
        help_texts = {
            "meeting_summary_prompt": _(
                "Base prompt for summarizing a meeting's protocol answers. "
                "Leave blank to use the built-in default. The meeting's protocol(s) "
                "and answers are appended automatically."
            ),
            "transcript_summary_prompt": _(
                "Base prompt for post-processing a Whisper call transcript into a summary. "
                "Leave blank to use the built-in default. The transcript is appended automatically."
            ),
            "transcript_moments_prompt": _(
                "Base prompt for picking the key moments out of a call transcript. "
                "Leave blank to use the built-in default. The transcript's numbered "
                "segments are appended automatically, and the reply is read back as "
                "one \u201csegment number|sentence\u201d line per moment."
            ),
        }
        widgets = {
            "meeting_summary_prompt": forms.Textarea(attrs={**_INPUT, "rows": 8}),
            "transcript_summary_prompt": forms.Textarea(attrs={**_INPUT, "rows": 8}),
            "transcript_moments_prompt": forms.Textarea(attrs={**_INPUT, "rows": 8}),
        }


class ClientForm(forms.ModelForm):
    """Create a client (Patient) with an optional caregiver. Navigators are
    auto-assigned; only admins may choose the navigator."""
    caregiver_name = forms.CharField(required=False, label=_("Caregiver first name"), widget=forms.TextInput(attrs=_INPUT))
    caregiver_lastname = forms.CharField(required=False, label=_("Caregiver last name"), widget=forms.TextInput(attrs=_INPUT))
    caregiver_phone = forms.CharField(required=False, label=_("Caregiver phone"), widget=forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}))

    class Meta:
        model = Patient
        fields = ["name", "lastname", "phone_number", "navigator", "agent"]
        labels = {
            "name": _("First name"), "lastname": _("Last name"), "phone_number": _("Phone"),
            "navigator": _("Navigator"), "agent": _("Agent"),
        }
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "lastname": forms.TextInput(attrs=_INPUT),
            "phone_number": forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}),
            "navigator": forms.Select(attrs=_SELECT),
            "agent": forms.Select(attrs=_SELECT),
        }

    def __init__(self, *args, is_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["lastname"].required = True
        self.fields["agent"].required = False
        if is_admin:
            self.fields["navigator"].required = False
            self.fields["navigator"].queryset = get_user_model().objects.filter(
                groups__name="Navigator"
            ).order_by("username")
        else:
            # Navigators can't choose — they are auto-assigned as the navigator.
            self.fields.pop("navigator")


class AgentForm(forms.ModelForm):
    """App-level create/edit form for **remote** agents (LangGraph server)."""
    class Meta:
        model = Agent
        fields = [
            "name", "langgraph_name", "host", "port",
            "classification_role", "abstract_instruction", "detectors", "tts_voice_id",
        ]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "langgraph_name": forms.TextInput(attrs=_INPUT),
            "host": forms.TextInput(attrs=_INPUT),
            "port": forms.NumberInput(attrs=_INPUT),
            "classification_role": forms.Textarea(attrs={**_INPUT, "rows": 4}),
            "abstract_instruction": forms.TextInput(attrs=_INPUT),
            "detectors": forms.Textarea(attrs={**_INPUT, "rows": 4, "placeholder": '{"label": "instruction", ...}'}),
            "tts_voice_id": forms.TextInput(attrs=_INPUT),
        }


# Free-text model input backed by a datalist of common suggestions (rendered in
# the template). Admins can also type any provider-prefixed id or Azure deployment.
_MODEL_INPUT = {**_INPUT, "list": "model-suggestions", "placeholder": "openai/gpt-4.1-mini",
                "autocomplete": "off"}


class PromptAgentForm(forms.ModelForm):
    """App-level create/edit form for **prompt-based** agents.

    These run in-process using the stored ``system_prompt`` as the system
    message. Two subtypes, selected by ``rag_enabled``: plain (no tools) and
    RAG-based, which adds a search tool over documents uploaded against the
    agent on its own Knowledge base page.
    """
    class Meta:
        model = Agent
        fields = [
            "name", "system_prompt", "model", "rag_enabled", "rag_top_k",
            "realtime_enabled",
            "classification_role", "abstract_instruction", "detectors", "tts_voice_id",
        ]
        labels = {
            "realtime_enabled": _("Real-time voice agent"),
            "rag_enabled": _("Knowledge base (RAG)"),
            "rag_top_k": _("Extracts per search"),
        }
        help_texts = {
            "realtime_enabled": _(
                "Converse by live voice (GPT Realtime over Azure) instead of the "
                "text chat. The model above is ignored; the Realtime deployment "
                "from Settings → Agents is used."
            ),
            "rag_enabled": _(
                "Give the agent a search tool over documents you upload. Save "
                "first, then add documents from the agent's Knowledge base page. "
                "Switching this off keeps the documents and their vectors — the "
                "agent just stops being able to search them."
            ),
            "rag_top_k": _("How many document extracts each search returns. "
                           "5 suits most knowledge bases; raise it for long "
                           "documents, lower it for short ones."),
        }
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "system_prompt": forms.Textarea(attrs={**_INPUT, "rows": 10,
                "placeholder": "You are a helpful assistant…"}),
            "model": forms.TextInput(attrs=_MODEL_INPUT),
            "rag_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "rag_top_k": forms.NumberInput(attrs={**_INPUT, "min": 1, "max": 20}),
            "realtime_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "classification_role": forms.Textarea(attrs={**_INPUT, "rows": 4}),
            "abstract_instruction": forms.TextInput(attrs=_INPUT),
            "detectors": forms.Textarea(attrs={**_INPUT, "rows": 4, "placeholder": '{"label": "instruction", ...}'}),
            "tts_voice_id": forms.TextInput(attrs=_INPUT),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["system_prompt"].required = True

    def clean(self):
        cleaned = super().clean()
        # Real-time voice runs speech-to-speech against the Realtime API, which
        # this agent does not drive tool calls through. Allowing both would put
        # a knowledge base on an agent that can never consult it.
        if cleaned.get("rag_enabled") and cleaned.get("realtime_enabled"):
            raise forms.ValidationError(
                _("A knowledge base cannot be combined with real-time voice: "
                  "real-time agents converse directly with the voice API and "
                  "cannot call the search tool. Pick one.")
            )
        top_k = cleaned.get("rag_top_k")
        if top_k is not None and top_k > 20:
            self.add_error("rag_top_k", _("Use 20 or fewer — more extracts "
                                          "crowd out the conversation."))
        return cleaned


class NativeAgentForm(forms.ModelForm):
    """Edit form for **native** agents.

    Native agents ship with the platform (their graph/tools are code), so only
    the admin-tunable knobs are editable here: the model behind the agent and the
    TTS voice.
    """
    class Meta:
        model = Agent
        fields = ["model", "tts_voice_id"]
        widgets = {
            "model": forms.TextInput(attrs=_MODEL_INPUT),
            "tts_voice_id": forms.TextInput(attrs=_INPUT),
        }


class NavigatorForm(forms.Form):
    """Admin-only creation of a Navigator account (username + password)."""
    username = forms.CharField(max_length=150, label=_("Username"), widget=forms.TextInput(attrs=_INPUT))
    password = forms.CharField(label=_("Password"), widget=forms.PasswordInput(attrs={**_INPUT, "autocomplete": "new-password"}))

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(_("A user with that username already exists."))
        return username


def _patients_without_tester():
    return Patient.objects.select_related("caregiver").filter(
        tester_account__isnull=True
    ).order_by("name", "lastname")


class TestUserForm(forms.Form):
    """Admin-only creation of a Test-user account for a client.

    Only the client is chosen — the username and password are generated. A
    client may have at most one test user, so the dropdown lists only clients
    that don't already have one.
    """
    patient = forms.ModelChoiceField(
        queryset=_patients_without_tester(),
        label=_("Client"), widget=forms.Select(attrs=_SELECT),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Re-evaluate each request so a client that just got a tester disappears.
        self.fields["patient"].queryset = _patients_without_tester()

    def clean_patient(self):
        patient = self.cleaned_data.get("patient")
        if patient and getattr(patient, "tester_account_id", None):
            raise forms.ValidationError(_("This client already has a test user."))
        return patient


class EditUserForm(forms.ModelForm):
    """Edit an existing account (admin only)."""
    class Meta:
        model = get_user_model()
        fields = ["username", "email", "is_active"]
        labels = {
            "username": _("Username"),
            "email": _("E-mail"),
            "is_active": _("Active"),
        }
        widgets = {
            "username": forms.TextInput(attrs=_INPUT),
            "email": forms.EmailInput(attrs=_INPUT),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        qs = get_user_model().objects.filter(username__iexact=username)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_("A user with that username already exists."))
        return username


class PatientForm(forms.ModelForm):
    """Edit a client's core fields, including the agent that handles their chat
    and the linked caregiver's contact details."""
    caregiver_name = forms.CharField(required=False, label=_("Caregiver first name"), widget=forms.TextInput(attrs=_INPUT))
    caregiver_lastname = forms.CharField(required=False, label=_("Caregiver last name"), widget=forms.TextInput(attrs=_INPUT))
    caregiver_phone = forms.CharField(required=False, label=_("Caregiver phone"), widget=forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}))

    class Meta:
        model = Patient
        fields = ["name", "lastname", "phone_number", "navigator", "agent"]
        labels = {
            "name": _("First name"), "lastname": _("Last name"), "phone_number": _("Phone"),
            "navigator": _("Navigator"), "agent": _("Agent"),
        }
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "lastname": forms.TextInput(attrs=_INPUT),
            "phone_number": forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}),
            "navigator": forms.Select(attrs=_SELECT),
            "agent": forms.Select(attrs=_SELECT),
        }

    def __init__(self, *args, is_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["lastname"].required = True
        self.fields["agent"].required = False
        if is_admin:
            self.fields["navigator"].required = False
            self.fields["navigator"].queryset = get_user_model().objects.filter(
                groups__name="Navigator"
            ).order_by("username")
        else:
            # Navigators can't reassign the client to someone else.
            self.fields.pop("navigator")

        # Pre-fill caregiver fields from the linked caregiver, if any.
        caregiver = getattr(self.instance, "caregiver", None)
        if caregiver:
            self.fields["caregiver_name"].initial = caregiver.name
            self.fields["caregiver_lastname"].initial = caregiver.lastname
            self.fields["caregiver_phone"].initial = (
                str(caregiver.phone_number) if caregiver.phone_number else ""
            )


class HelpContentForm(forms.ModelForm):
    """Single-field form to edit the Help page Markdown (admin only)."""

    class Meta:
        model = SiteConfiguration
        fields = ["help_markdown"]
        labels = {"help_markdown": _("Help page content (Markdown)")}
        help_texts = {
            "help_markdown": _("Supports Markdown. Leave blank to restore the shipped default content."),
        }
        widgets = {
            "help_markdown": forms.Textarea(attrs={"rows": 20, "class": "form-control"}),
        }


class BrandingConfigForm(forms.ModelForm):
    class Meta:
        model = SiteConfiguration
        fields = ["brand_name", "brand_logo_file", "brand_logo"]
        labels = {
            "brand_name": _("Brand name"),
            "brand_logo_file": _("Brand logo (upload)"),
            "brand_logo": _("Brand logo (static path)"),
        }
        help_texts = {
            "brand_logo_file": _("PNG, JPG or SVG, max 2 MB. Overrides the static path below."),
            "brand_logo": _("Fallback used when no logo file is uploaded."),
        }
        widgets = {
            "brand_name": forms.TextInput(attrs=_INPUT),
            "brand_logo_file": forms.ClearableFileInput(attrs={**_INPUT, "accept": "image/*"}),
            "brand_logo": forms.TextInput(attrs={**_INPUT, "placeholder": "app/images/logo.png"}),
        }

    def clean_brand_logo_file(self):
        f = self.cleaned_data.get("brand_logo_file")
        # Only validate size on a freshly uploaded file (has size attr).
        if f and hasattr(f, "size") and f.size > 2 * 1024 * 1024:
            raise forms.ValidationError(_("The logo cannot exceed 2 MB."))
        return f

class AgentConfigForm(SecretPreserveMixin, forms.ModelForm):
    """Agent runtime config: default model, provider API keys, and Azure routing.

    (The OpenAI key lives in the Integrations tab; this covers the other
    providers, Azure, and the default model.) Secret keys are write-only: blank
    submissions keep the stored value.
    """
    secret_fields = (
        "anthropic_api_key", "google_api_key", "mistral_api_key", "deepseek_api_key",
        "azure_openai_api_key", "azure_anthropic_api_key", "azure_mistral_api_key",
        "azure_deepseek_api_key", "azure_realtime_api_key",
    )

    class Meta:
        model = SiteConfiguration
        fields = [
            "default_agent_model",
            "anthropic_api_key", "google_api_key", "mistral_api_key", "deepseek_api_key",
            "agent_allowed_hosts",
            "use_azure",
            "azure_openai_endpoint", "azure_openai_api_key", "azure_openai_api_version",
            "azure_anthropic_endpoint", "azure_anthropic_api_key",
            "azure_mistral_endpoint", "azure_mistral_api_key",
            "azure_deepseek_endpoint", "azure_deepseek_api_key",
            "azure_realtime_endpoint", "azure_realtime_api_key",
            "azure_realtime_deployment", "azure_realtime_voice",
            "azure_realtime_webrtc_region",
            "rag_embedding_model", "azure_embedding_deployment",
        ]
        labels = {
            "default_agent_model": _("Default agent model"),
            "anthropic_api_key": _("Anthropic API key"),
            "google_api_key": _("Google API key"),
            "mistral_api_key": _("Mistral API key"),
            "deepseek_api_key": _("DeepSeek API key"),
            "agent_allowed_hosts": _("Allowed agent hosts (comma-separated)"),
            "use_azure": _("Use Azure"),
            "azure_openai_endpoint": _("Azure OpenAI endpoint"),
            "azure_openai_api_key": _("Azure OpenAI key"),
            "azure_openai_api_version": _("Azure OpenAI API version"),
            "azure_anthropic_endpoint": _("Azure Anthropic endpoint"),
            "azure_anthropic_api_key": _("Azure Anthropic key"),
            "azure_mistral_endpoint": _("Azure Mistral endpoint"),
            "azure_mistral_api_key": _("Azure Mistral key"),
            "azure_deepseek_endpoint": _("Azure DeepSeek endpoint"),
            "azure_deepseek_api_key": _("Azure DeepSeek key"),
            "azure_realtime_endpoint": _("Azure Realtime endpoint"),
            "azure_realtime_api_key": _("Azure Realtime key"),
            "azure_realtime_deployment": _("Realtime deployment"),
            "azure_realtime_voice": _("Realtime voice"),
            "azure_realtime_webrtc_region": _("Realtime WebRTC region"),
            "rag_embedding_model": _("Embedding model"),
            "azure_embedding_deployment": _("Azure embedding deployment"),
        }
        help_texts = {
            "default_agent_model": _("Used when an agent has no explicit model, e.g. "
                                     "'openai/gpt-4.1-mini'. Blank uses the .env default."),
            "use_azure": _("Route models to Azure instead of the public provider APIs."),
            "azure_deepseek_endpoint": _("Blank reuses the Azure Mistral endpoint/key."),
            "azure_realtime_endpoint": _("Blank reuses the Azure OpenAI endpoint/key."),
            "azure_realtime_deployment": _("Deployment used by real-time voice agents, "
                                           "e.g. 'gpt-realtime'."),
            "azure_realtime_voice": _("Output voice for real-time voice agents. "
                                      "Blank uses 'marin'."),
            "azure_realtime_webrtc_region": _("Only for resources on the preview "
                                              "Realtime API: the resource's region, "
                                              "e.g. 'swedencentral' or 'eastus2'."),
            "rag_embedding_model": _("Used to index and search RAG agents' "
                                     "documents. Blank uses "
                                     "'text-embedding-3-small'. Changing it "
                                     "makes existing documents unsearchable "
                                     "until they are re-uploaded."),
            "azure_embedding_deployment": _("Under Azure, the deployment serving "
                                            "the embedding model above. Blank "
                                            "reuses the model name."),
        }
        widgets = {
            "default_agent_model": forms.TextInput(attrs={**_INPUT, "list": "model-suggestions",
                                                          "placeholder": "openai/gpt-4.1-mini"}),
            "agent_allowed_hosts": forms.Textarea(attrs={**_INPUT, "rows": 3}),
            "use_azure": forms.Select(attrs=_SELECT),
            "azure_openai_endpoint": forms.TextInput(attrs={**_INPUT, "placeholder": "https://<resource>.openai.azure.com/"}),
            "azure_openai_api_version": forms.TextInput(attrs={**_INPUT, "placeholder": "2024-12-01-preview"}),
            "azure_anthropic_endpoint": forms.TextInput(attrs=_INPUT),
            "azure_mistral_endpoint": forms.TextInput(attrs=_INPUT),
            "azure_deepseek_endpoint": forms.TextInput(attrs=_INPUT),
            "azure_realtime_endpoint": forms.TextInput(attrs={**_INPUT, "placeholder": "https://<resource>.openai.azure.com/"}),
            "azure_realtime_deployment": forms.TextInput(attrs={**_INPUT, "placeholder": "gpt-realtime"}),
            "azure_realtime_voice": forms.TextInput(attrs={**_INPUT, "placeholder": "marin"}),
            "azure_realtime_webrtc_region": forms.TextInput(attrs={**_INPUT, "placeholder": "swedencentral"}),
            "rag_embedding_model": forms.TextInput(attrs={**_INPUT, "placeholder": "text-embedding-3-small"}),
            "azure_embedding_deployment": forms.TextInput(attrs={**_INPUT, "placeholder": "text-embedding-3-small"}),
        }