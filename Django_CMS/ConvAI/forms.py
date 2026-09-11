import json
import secrets

from django import forms
from .models import Meeting, Patient, Question, Answer, Agent, SiteConfiguration
from datetime import date
from django.contrib.auth import get_user_model
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
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
                  'scheduled_protocols', 'scheduled_time']
        widgets = {
            'patient': forms.Select(attrs={'class': 'form-select'}),
            'modality': forms.Select(attrs={'class': 'form-select'}),
            'location': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': _("Address or place"),
            }),
            'type': forms.Select(attrs={'class': 'form-select'}),
            'scheduled_protocols': forms.CheckboxSelectMultiple(),
        }
        labels = {
            'patient': _('Client'),
            'modality': _('How'),
            'location': _('Where'),
            'type': _('Type'),
            'scheduled_protocols': _('Protocols to address'),
        }
        # The model's help_text is written for whoever reads the schema: it
        # spells out integer codes and is partly in Spanish. None of it belongs
        # on a form a navigator fills in, and the labels already say enough.
        help_texts = {
            'location': '',
            'modality': '',
            'type': '',
            'scheduled_protocols': '',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Every protocol in the platform, by its real name. This used to be a
        # single choice over Meeting.Protocol — ten placeholder labels that were
        # never anybody's protocol — with real titles patched over whichever
        # numbers happened to exist. That is why it could offer protocols nobody
        # had created and could never offer an eleventh that someone had.
        #
        # Not narrowed to the client's own programme: the client is chosen in
        # this same form, so there is nothing to narrow by until they are, and a
        # one-off against a protocol they are not on is a real thing to want.
        # The dialog marks which are on the chosen client's programme — see
        # protocol_owners below.
        from .models import Protocol as ProtocolRecord
        field = self.fields.get('scheduled_protocols')
        if field is not None:
            field.required = False
            field.queryset = ProtocolRecord.objects.order_by('number')

    def protocol_owners(self):
        """{protocol id: [client id, …]} over the clients this form can pick.

        Lets the dialog float the chosen client's own protocols to the top the
        moment they are chosen, without a round trip. Scoped to the patient
        queryset, which the view has already narrowed to who the user may see.
        """
        from .models import Patient
        owners = {}
        pairs = (Patient.objects
                 .filter(pk__in=self.fields['patient'].queryset.values('pk'))
                 .values_list('protocols__id', 'pk'))
        for protocol_id, patient_id in pairs:
            if protocol_id:
                owners.setdefault(protocol_id, []).append(patient_id)
        return owners

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
            "reminder_channel",
            "twilio_sms_from", "sms_template_start_infection_sid",
            "sms_template_start_infection_text", "whatsapp_template_care_plan_sid",
        ]
        labels = {
            "reminder_channel": _("Meeting reminder channel"),
            "twilio_sms_from": _("SMS sender number"),
            "sms_template_start_infection_sid": _("Infection alert SMS template SID"),
            "sms_template_start_infection_text": _("Infection alert SMS text"),
            "whatsapp_template_care_plan_sid": _("Care plan WhatsApp template SID"),
        }
        help_texts = {
            "reminder_channel": _(
                "How the Send reminder button contacts a caregiver. Email goes to "
                "the caregiver's address, falling back to the client's, and needs "
                "a working provider under the Email tab."
            ),
        }
        widgets = {
            "reminder_channel": forms.Select(attrs=_SELECT),
            "twilio_sms_from": forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}),
            "sms_template_start_infection_sid": forms.TextInput(attrs={**_INPUT, "placeholder": "HX…"}),
            "sms_template_start_infection_text": forms.Textarea(attrs={**_INPUT, "rows": 3}),
            "whatsapp_template_care_plan_sid": forms.TextInput(attrs={**_INPUT, "placeholder": "HX…"}),
        }


class EmailConfigForm(SecretPreserveMixin, forms.ModelForm):
    """Outbound email: which provider carries it, and its credentials.

    Both providers' fields are on one form rather than two, because an admin
    switching from SMTP to Azure should not lose what they had typed for the
    other. Only the selected provider's values are ever read (see
    ConvAI.mailer), so the unused half sits harmlessly.
    """
    secret_fields = ("azure_email_connection_string", "azure_email_access_key", "smtp_password")

    class Meta:
        model = SiteConfiguration
        fields = [
            "email_provider", "email_from", "email_from_name", "email_reply_to",
            "azure_email_connection_string", "azure_email_endpoint", "azure_email_access_key",
            "smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_security",
        ]
        labels = {
            "email_provider": _("Email provider"),
            "email_from": _("Sender address"),
            "email_from_name": _("Sender display name"),
            "email_reply_to": _("Reply-to address"),
            "azure_email_connection_string": _("Connection string"),
            "azure_email_endpoint": _("Endpoint"),
            "azure_email_access_key": _("Access key"),
            "smtp_host": _("SMTP host"),
            "smtp_port": _("Port"),
            "smtp_user": _("Username"),
            "smtp_password": _("Password"),
            "smtp_security": _("Encryption"),
        }
        help_texts = {
            "email_from": _("Must be a verified sender on the provider's domain, "
                            "e.g. 'donotreply@mail.example.com'."),
            "email_from_name": _("Shown as the sender's name. Blank uses the brand name. "
                                 "Azure ignores this — its display name comes from the "
                                 "sender username configured on the domain."),
            "email_reply_to": _("Where replies go, if anywhere. Comma-separated for more than one."),
            "azure_email_connection_string": _("The whole string from the Azure portal. "
                                               "Leave blank to use the endpoint and access key below instead."),
            "azure_email_endpoint": _("e.g. 'https://<resource>.uk.communication.azure.com/'. "
                                      "Ignored when a connection string is set."),
            "smtp_port": _("Blank uses 587 for STARTTLS and 465 for SSL/TLS."),
        }
        widgets = {
            "email_provider": forms.Select(attrs=_SELECT),
            "email_from": forms.EmailInput(attrs={**_INPUT, "placeholder": "donotreply@mail.example.com"}),
            "email_from_name": forms.TextInput(attrs=_INPUT),
            "email_reply_to": forms.TextInput(attrs={**_INPUT, "placeholder": "support@example.com"}),
            "azure_email_endpoint": forms.TextInput(
                attrs={**_INPUT, "placeholder": "https://<resource>.uk.communication.azure.com/"}),
            "smtp_host": forms.TextInput(attrs={**_INPUT, "placeholder": "smtp.example.com"}),
            "smtp_port": forms.TextInput(attrs={**_INPUT, "placeholder": "587", "inputmode": "numeric"}),
            "smtp_user": forms.TextInput(attrs={**_INPUT, "autocomplete": "off"}),
            "smtp_security": forms.Select(attrs=_SELECT),
        }

    def clean_smtp_port(self):
        port = (self.cleaned_data.get("smtp_port") or "").strip()
        if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
            raise forms.ValidationError(_("Enter a port number between 1 and 65535, or leave it blank."))
        return port


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
    caregiver_email = forms.EmailField(required=False, label=_("Caregiver e-mail"), widget=forms.EmailInput(attrs={**_INPUT, "placeholder": "carer@example.com"}))

    class Meta:
        model = Patient
        fields = ["name", "lastname", "phone_number", "email", "navigator", "agent",
                  "protocols"]
        labels = {
            "name": _("First name"), "lastname": _("Last name"), "phone_number": _("Phone"),
            "email": _("E-mail"), "navigator": _("Navigator"), "agent": _("Agent"),
            "protocols": _("Protocols"),
        }
        help_texts = {
            "email": _("Used for email reminders when the caregiver has no address."),
            "protocols": _(
                "The protocols this client works through. Only these appear in "
                "their call panel. Unticking one never deletes answers already "
                "recorded against it."
            ),
        }
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "lastname": forms.TextInput(attrs=_INPUT),
            "phone_number": forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}),
            "email": forms.EmailInput(attrs={**_INPUT, "placeholder": "client@example.com"}),
            "navigator": forms.Select(attrs=_SELECT),
            "agent": forms.Select(attrs=_SELECT),
            "protocols": forms.CheckboxSelectMultiple(),
        }

    def __init__(self, *args, is_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["lastname"].required = True
        self.fields["agent"].required = False
        # A new client starts on nothing. An empty panel asks which protocols
        # this person is on; a full one answers it wrongly, for everybody, which
        # is the state this replaces.
        self.fields["protocols"].required = False
        self.fields["protocols"].queryset = (
            self.fields["protocols"].queryset.order_by("number")
        )
        if is_admin:
            self.fields["navigator"].required = False
            self.fields["navigator"].queryset = get_user_model().objects.filter(
                groups__name="Navigator"
            ).order_by("username")
        else:
            # Navigators can't choose — they are auto-assigned as the navigator.
            self.fields.pop("navigator")


class DetectorTableWidget(forms.Widget):
    """The editor for what an agent watches for, and what it does about it.

    ``Agent.detectors`` was a JSON textarea, which was honest about the storage
    and useless as a control: the field decided whether a caregiver's message
    reached a human, and editing it meant hand-writing JSON with no indication
    that "raises" was even a key you could write. It is a table now — label,
    instruction, whether it raises an alert, at what priority.

    Both stored shapes render. ``{label: "instruction"}`` predates alerts and
    means detect-but-do-not-raise, which is exactly what those rows did, so
    they come back with the box unticked rather than being quietly promoted.

    The self-harm row is drawn first and cannot be unticked. It is compiled in
    (see ``utils_conversation_classification.SAFETY_DETECTOR``) and applies
    whether or not it appears here; showing it greyed is how an admin finds out
    it exists, instead of wondering why an alert they never configured fired.
    """

    template_name = None

    PRIORITIES = ((1, _("High")), (2, _("Medium")), (3, _("Low")))

    def _rows(self, value):
        """Normalized rows for rendering, safety floor first."""
        from .utils_conversation_classification import (
            SAFETY_LABEL, SAFETY_DETECTOR, normalize_detectors,
        )
        if isinstance(value, str):
            try:
                value = json.loads(value or "{}")
            except ValueError:
                value = {}
        dets = normalize_detectors(value)
        own = dets.pop(SAFETY_LABEL, None)
        safety = (SAFETY_DETECTOR.instruction if own is None
                  else (own.instruction or SAFETY_DETECTOR.instruction))
        return safety, list(dets.values())

    def render(self, name, value, attrs=None, renderer=None):
        safety_instruction, rows = self._rows(value)

        head = format_html(
            '<thead><tr>'
            '<th class="dt-c-label">{}</th><th>{}</th>'
            '<th class="dt-c-raise">{}</th><th class="dt-c-prio">{}</th>'
            '<th class="dt-c-x"></th></tr></thead>',
            _("Label"), _("How to detect"), _("Raise alert"), _("Priority"),
        )

        locked = format_html(
            '<tr class="dt-row dt-row-locked">'
            '<td><span class="dt-lock-label">{}</span>'
            '<span class="dt-lock-note">{}</span></td>'
            '<td class="dt-lock-instr">{}</td>'
            '<td class="dt-mid"><input type="checkbox" checked disabled></td>'
            '<td><span class="dt-prio-high">{}</span></td>'
            '<td></td></tr>',
            _("Self-harm"), _("built in"), safety_instruction, _("High"),
        )

        body = [locked] + [self._row(name, i, d) for i, d in enumerate(rows)]

        return format_html(
            '<div class="dt-wrap" data-dt data-dt-name="{}" data-dt-next="{}">'
            '<table class="dt-table">{}<tbody data-dt-body>{}</tbody></table>'
            '<button type="button" class="dt-add" data-dt-add>+ {}</button>'
            '<p class="dt-help">{}</p></div>{}',
            name, len(rows), head, mark_safe("".join(body)),
            _("Add detector"),
            _("Unticked detectors still flag the conversation for review — they "
              "just do not put it in anyone's queue."),
            mark_safe(self._style() + self._script()),
        )

    def id_for_label(self, id_):
        """No single control to point a label at, so don't claim one."""
        return ""

    def _row(self, name, i, d):
        options = format_html_join(
            "", '<option value="{}"{}>{}</option>',
            ((v, mark_safe(' selected' if v == d.priority else ''), label)
             for v, label in self.PRIORITIES),
        )
        return format_html(
            '<tr class="dt-row">'
            '<td><input type="text" class="form-control" name="{n}_label_{i}" value="{lb}"></td>'
            '<td><input type="text" class="form-control" name="{n}_instr_{i}" value="{ins}"></td>'
            '<td class="dt-mid"><input type="checkbox" name="{n}_raise_{i}"{ck}></td>'
            '<td><select class="form-select" name="{n}_prio_{i}">{opt}</select></td>'
            '<td class="dt-mid"><button type="button" class="dt-x" data-dt-x '
            'aria-label="{rm}">&times;</button></td>'
            '</tr>',
            n=name, i=i, lb=d.label, ins=d.instruction,
            ck=mark_safe(" checked" if d.raises else ""), opt=options, rm=_("Remove"),
        )

    def value_from_datadict(self, data, files, name):
        """Rebuild the JSON object from the table's indexed inputs.

        Indexed rather than parallel ``getlist`` arrays because an unticked
        checkbox submits nothing at all: with parallel lists the ticks would
        slide onto the wrong rows the moment one was cleared.
        """
        out = {}
        prefix = f"{name}_label_"
        for key in data:
            if not key.startswith(prefix):
                continue
            idx = key[len(prefix):]
            label = (data.get(key) or "").strip()
            if not label:
                continue
            try:
                priority = int(data.get(f"{name}_prio_{idx}") or 2)
            except (TypeError, ValueError):
                priority = 2
            out[label] = {
                "instruction": (data.get(f"{name}_instr_{idx}") or "").strip(),
                "raises": bool(data.get(f"{name}_raise_{idx}")),
                "priority": priority if priority in (1, 2, 3) else 2,
            }
        return out

    def _style(self):
        """Carried by the widget rather than the app stylesheet.

        This renders on the Agents page and inside Django admin, and admin does
        not load the app's CSS. One copy that travels with the markup beats two
        that drift.
        """
        return """
<style>
.dt-table { width: 100%; border-collapse: collapse; font-size: .8125rem; }
.dt-table th { text-align: left; font-weight: 500; color: #64748b;
  padding: 0 .5rem .4rem 0; border-bottom: 1px solid #e2e8f0; }
.dt-table td { padding: .45rem .5rem .45rem 0; vertical-align: middle;
  border-bottom: 1px solid #f1f5f9; }
.dt-c-label { width: 22%; } .dt-c-raise { width: 5.5rem; }
.dt-c-prio { width: 7rem; } .dt-c-x { width: 2rem; }
.dt-mid { text-align: center; }
.dt-table input[type=text], .dt-table select { width: 100%; }
.dt-row-locked td { background: #fafafa; color: #64748b; }
.dt-lock-label { display: block; font-weight: 500; color: #334155; }
.dt-lock-note { display: block; font-size: .6875rem; color: #94a3b8; }
.dt-lock-instr { font-size: .75rem; line-height: 1.45; }
.dt-prio-high { display: inline-block; font-size: .75rem; padding: .1rem .5rem;
  border-radius: .75rem; background: #fee2e2; color: #b91c1c; }
.dt-x { border: 0; background: none; cursor: pointer; color: #94a3b8;
  font-size: 1.1rem; line-height: 1; padding: 0 .25rem; }
.dt-x:hover { color: #b91c1c; }
.dt-add { margin-top: .6rem; font-size: .8125rem; padding: .3rem .7rem;
  border: 1px solid #cbd5e1; border-radius: .375rem; background: #fff; cursor: pointer; }
.dt-help { font-size: .75rem; color: #64748b; margin: .5rem 0 0; }
</style>
"""

    def _script(self):
        return """
<script>
(function () {
  document.querySelectorAll('[data-dt]:not([data-dt-ready])').forEach(function (w) {
    w.setAttribute('data-dt-ready', '1');
    var body = w.querySelector('[data-dt-body]');
    var name = w.getAttribute('data-dt-name');
    var next = parseInt(w.getAttribute('data-dt-next'), 10) || 0;
    w.querySelector('[data-dt-add]').addEventListener('click', function () {
      var i = next++;
      var tr = document.createElement('tr');
      tr.className = 'dt-row';
      tr.innerHTML =
        '<td><input type="text" class="form-control" name="' + name + '_label_' + i + '"></td>' +
        '<td><input type="text" class="form-control" name="' + name + '_instr_' + i + '"></td>' +
        '<td class="dt-mid"><input type="checkbox" name="' + name + '_raise_' + i + '"></td>' +
        '<td><select class="form-select" name="' + name + '_prio_' + i + '">' +
          '<option value="1">High</option>' +
          '<option value="2" selected>Medium</option>' +
          '<option value="3">Low</option></select></td>' +
        '<td class="dt-mid"><button type="button" class="dt-x" data-dt-x>&times;</button></td>';
      body.appendChild(tr);
      tr.querySelector('input').focus();
    });
    body.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-dt-x]');
      if (btn) btn.closest('tr').remove();
    });
  });
})();
</script>
"""


# The description is one line about what the agent is for, shown on its card on
# the Agents page. Two rows rather than a single-line input: an admin writing one
# should see the whole sentence, and 200 characters do not fit in a text box.
_DESCRIPTION_WIDGET = forms.Textarea(attrs={
    **_INPUT, "rows": 2, "maxlength": 200,
    "placeholder": _("Answers questions about medication from the uploaded leaflets."),
})


class AgentForm(forms.ModelForm):
    """App-level create/edit form for **remote** agents (LangGraph server)."""
    class Meta:
        model = Agent
        fields = [
            "name", "description", "langgraph_name", "host", "port",
            "classification_role", "abstract_instruction", "detectors", "tts_voice_id",
        ]
        labels = {"description": _("Description")}
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "description": _DESCRIPTION_WIDGET,
            "langgraph_name": forms.TextInput(attrs=_INPUT),
            "host": forms.TextInput(attrs=_INPUT),
            "port": forms.NumberInput(attrs=_INPUT),
            "classification_role": forms.Textarea(attrs={**_INPUT, "rows": 4}),
            "abstract_instruction": forms.TextInput(attrs=_INPUT),
            "detectors": DetectorTableWidget(),
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
            "name", "description", "system_prompt", "model", "rag_enabled", "rag_top_k",
            "realtime_enabled",
            "classification_role", "abstract_instruction", "detectors", "tts_voice_id",
        ]
        labels = {
            "description": _("Description"),
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
            "description": _DESCRIPTION_WIDGET,
            "system_prompt": forms.Textarea(attrs={**_INPUT, "rows": 10,
                "placeholder": "You are a helpful assistant…"}),
            "model": forms.TextInput(attrs=_MODEL_INPUT),
            "rag_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "rag_top_k": forms.NumberInput(attrs={**_INPUT, "min": 1, "max": 20}),
            "realtime_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "classification_role": forms.Textarea(attrs={**_INPUT, "rows": 4}),
            "abstract_instruction": forms.TextInput(attrs=_INPUT),
            "detectors": DetectorTableWidget(),
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
    the admin-tunable knobs are editable here: the description shown on the
    Agents page, the model behind the agent, and the TTS voice.
    """
    class Meta:
        model = Agent
        fields = ["description", "model", "tts_voice_id"]
        labels = {"description": _("Description")}
        widgets = {
            "description": _DESCRIPTION_WIDGET,
            "model": forms.TextInput(attrs=_MODEL_INPUT),
            "tts_voice_id": forms.TextInput(attrs=_INPUT),
        }


class SenseiAgentForm(forms.ModelForm):
    """App-level create/edit form for **Sensei** agents.

    Deliberately thin. A Sensei agent has no connection settings of its own —
    the endpoint, function key and id secret are installation-wide and live in
    Settings -> Sensei — so what is left is how the agent presents itself and
    how its conversations are classified. There is no model field either: the
    answering model runs on Sensei's side, not ours.
    """
    class Meta:
        model = Agent
        fields = [
            "name", "description",
            "classification_role", "abstract_instruction", "detectors", "tts_voice_id",
        ]
        labels = {"description": _("Description")}
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "description": _DESCRIPTION_WIDGET,
            "classification_role": forms.Textarea(attrs={**_INPUT, "rows": 4}),
            "abstract_instruction": forms.TextInput(attrs=_INPUT),
            "detectors": DetectorTableWidget(),
            "tts_voice_id": forms.TextInput(attrs=_INPUT),
        }


class SenseiConfigForm(SecretPreserveMixin, forms.ModelForm):
    """Settings -> Sensei: the switch, the endpoint, and the two secrets."""
    secret_fields = ("sensei_function_key", "sensei_user_id_secret")

    class Meta:
        model = SiteConfiguration
        fields = [
            "sensei_enabled", "sensei_api_url",
            "sensei_function_key", "sensei_user_id_secret",
        ]
        labels = {
            "sensei_enabled": _("Enable Sensei agents"),
            "sensei_api_url": _("Sensei API URL"),
            "sensei_function_key": _("Sensei function key"),
            "sensei_user_id_secret": _("User-id secret"),
        }
        help_texts = {
            "sensei_enabled": _("Off by default. When off, Sensei agents cannot "
                                "be created and existing ones stop answering."),
            "sensei_api_url": _("The full endpoint, e.g. "
                                "https://<function-app>.azurewebsites.net/api/send_message"),
            "sensei_function_key": _("Sent as the 'x-functions-key' header."),
            "sensei_user_id_secret": _("Keys the opaque per-client id sent to Sensei, so "
                                       "Sensei never sees a real client identifier. Generated "
                                       "automatically if left blank. Changing it makes every "
                                       "client log in to Sensei again."),
        }
        widgets = {
            "sensei_enabled": forms.Select(attrs=_SELECT),
            "sensei_api_url": forms.TextInput(attrs={
                **_INPUT,
                "placeholder": "https://<function-app>.azurewebsites.net/api/send_message",
            }),
        }

    def clean(self):
        cleaned = super().clean()
        # A blank secret on first save is the normal case, not an error to make
        # the admin solve: nobody should be inventing HMAC keys by hand, and an
        # empty one would silently break every Sensei turn.
        if not cleaned.get("sensei_user_id_secret"):
            cleaned["sensei_user_id_secret"] = secrets.token_urlsafe(32)
        return cleaned


class ExportConfigForm(forms.ModelForm):
    """Settings -> Export: the switch that makes message export available."""

    class Meta:
        model = SiteConfiguration
        fields = ["message_export_enabled"]
        labels = {
            "message_export_enabled": _("Enable message export"),
        }
        help_texts = {
            "message_export_enabled": _("Off by default. When on, admins can download "
                                        "every stored message as a CSV file from this tab."),
        }
        widgets = {
            "message_export_enabled": forms.Select(attrs=_SELECT),
        }


class NavigatorForm(forms.Form):
    """Admin-only creation of a Navigator account (username, e-mail, password)."""
    username = forms.CharField(max_length=150, label=_("Username"), widget=forms.TextInput(attrs=_INPUT))
    # Optional, but an account without one can never recover its own password —
    # the reset link has nowhere to go. See email.md.
    email = forms.EmailField(
        required=False, label=_("E-mail"),
        help_text=_("Needed for password recovery. Can be added later."),
        widget=forms.EmailInput(attrs={**_INPUT, "placeholder": "user@example.com"}),
    )
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
    caregiver_email = forms.EmailField(required=False, label=_("Caregiver e-mail"), widget=forms.EmailInput(attrs={**_INPUT, "placeholder": "carer@example.com"}))

    class Meta:
        model = Patient
        fields = ["name", "lastname", "phone_number", "email", "navigator", "agent",
                  "protocols"]
        labels = {
            "name": _("First name"), "lastname": _("Last name"), "phone_number": _("Phone"),
            "email": _("E-mail"), "navigator": _("Navigator"), "agent": _("Agent"),
            "protocols": _("Protocols"),
        }
        help_texts = {
            "email": _("Used for email reminders when the caregiver has no address."),
            "protocols": _(
                "The protocols this client works through. Only these appear in "
                "their call panel. Unticking one never deletes answers already "
                "recorded against it."
            ),
        }
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "lastname": forms.TextInput(attrs=_INPUT),
            "phone_number": forms.TextInput(attrs={**_INPUT, "placeholder": "+51999999999"}),
            "email": forms.EmailInput(attrs={**_INPUT, "placeholder": "client@example.com"}),
            "navigator": forms.Select(attrs=_SELECT),
            "agent": forms.Select(attrs=_SELECT),
            "protocols": forms.CheckboxSelectMultiple(),
        }

    def __init__(self, *args, is_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["lastname"].required = True
        self.fields["agent"].required = False
        # A new client starts on nothing. An empty panel asks which protocols
        # this person is on; a full one answers it wrongly, for everybody, which
        # is the state this replaces.
        self.fields["protocols"].required = False
        self.fields["protocols"].queryset = (
            self.fields["protocols"].queryset.order_by("number")
        )
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
            self.fields["caregiver_email"].initial = caregiver.email
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