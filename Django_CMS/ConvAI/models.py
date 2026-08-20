from django.db import models
import os
import uuid
from django.conf import settings
from django.utils import timezone
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator, FileExtensionValidator
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

# Create your models here.

from django.contrib.auth.models import AbstractUser
from phonenumber_field.modelfields import PhoneNumberField

def _audio_upload_to(instance, filename):
    # store under VOICE_RECORDINGS_DIR/<conversation_id>/
    base = getattr(settings, "VOICE_RECORDINGS_DIR", "voice_recordings")
    return os.path.join(base, instance.conversation_id, filename)

class ConvAIUser(AbstractUser):
    phone_number = PhoneNumberField(blank=True, null=True)
    agent = models.ForeignKey(
        'Agent',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text=_("Optional default LangGraph agent for this user (used for testing/API)."),
        related_name="users_with_default_agent",
    )

    class Language(models.TextChoices):
        ES_PE = "es-pe", "Español (Perú)"
        PT_BR = "pt-br", "Português (Brasil)"
        EN_GB = "en-gb", "English (UK)"
        IT = "it", "Italiano"
        KO = "ko", "한국어"
        ZH_HANS = "zh-hans", "简体中文"

    # Small per-user display choices that belong to the person rather than the
    # browser. The next-call card's dismissal lived in localStorage, which meant
    # putting it away on a laptop and finding it again on a phone.
    #   next_call_off    — the card is switched off entirely
    #   next_call_hidden — a panel token; that one call is put away until it is
    #                      done and another becomes next
    dashboard_prefs = models.JSONField(blank=True, default=dict)

    # Blank = follow the system default language.
    preferred_language = models.CharField(
        max_length=10, blank=True, default="", choices=Language.choices,
        help_text=_("Preferred interface language; blank uses the system default."),
    )




class Message(models.Model):
    conversation_id = models.CharField(max_length=300)
    user = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    user_message = models.TextField()

    response_message = models.TextField()

    input_audio_file   = models.FileField(
        upload_to=_audio_upload_to,
        blank=True, null=True,
        help_text="Original user audio (webm/…)"
    )
    response_audio_file = models.FileField(
        upload_to=_audio_upload_to,
        blank=True, null=True,
        help_text="TTS response MP3"
    )

    liked    = models.BooleanField(default=False, help_text="Usuario marcó me gusta")
    disliked = models.BooleanField(default=False, help_text="Usuario marcó no me gusta")
    warning  = models.BooleanField(default=False, help_text="Usuario marcó advertencia")
    dangerous  = models.BooleanField(default=False, help_text="Usuario marcó dangerous")

    class Meta:
        ordering = ["timestamp"]
        indexes = [
            models.Index(fields=["conversation_id", "user", "timestamp"]),
        ]

    def __str__(self):
        return f"[{self.timestamp}] {self.user} → {self.conversation_id}"

class CallRecording(models.Model):
    recording_sid = models.CharField(max_length=100)
    from_number = models.CharField(max_length=100)
    to_number = models.CharField(max_length=100)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    duration = models.IntegerField()
    filename = models.CharField(max_length=100, null=True)

    # Whisper transcription + LLM post-processed summary (filled on demand).
    transcript = models.TextField(blank=True, default="", help_text="Whisper transcription of the call audio")
    transcript_summary = models.TextField(blank=True, default="", help_text="LLM summary of the transcript")
    transcribed_at = models.DateTimeField(null=True, blank=True)
    # Whisper returns every segment with a start and an end; the plain text
    # above threw them away. Kept as [{"start": 12.4, "end": 18.1, "text": "…",
    # "speaker": 1|2|null}] so the panel can run a timestamp down the side and
    # jump the player to a line. `speaker` is only filled when the recording
    # has two channels to tell the parties apart — see utils.transcribe_audio.
    transcript_segments = models.JSONField(blank=True, default=list,
                                           help_text="Whisper segments: start, end, text, speaker")
    # Moments worth jumping to, each anchored to a segment rather than to a
    # timestamp the model wrote itself, so a moment cannot point at audio that
    # is not there. [{"text": "…", "segment": 4, "start": 132.0}]
    transcript_moments = models.JSONField(blank=True, default=list,
                                          help_text="Key moments, each anchored to a segment")


class Caregiver(models.Model):
    """The person who actually answers the phone.

    Relationship and involvement are here because a name and a number describe
    a contact record, not a person — and because knowing who picks up changes
    how a call opens. Both are free text: "daughter", "neighbour" and "paid
    carer" are all real answers and no fixed list survives contact with them.
    """
    name = models.TextField()
    lastname = models.TextField()
    phone_number = PhoneNumberField(blank=True, null=True)
    relationship = models.CharField(
        max_length=60, blank=True,
        help_text="How they are related to the client — daughter, neighbour, paid carer",
    )
    involvement = models.CharField(
        max_length=120, blank=True,
        help_text="How involved they usually are — for example, usually on the call",
    )

    def __str__(self):
        return f"{self.name} {self.lastname}"

class ContactTerm(models.Model):
    """A way a client can ask to be contacted.

    Five ship with the platform; anyone can add one when the list does not have
    what a client actually said. New terms join the shared vocabulary rather
    than belonging to the client they were written for — the next navigator
    with the same situation should find it already there instead of inventing a
    near-duplicate. Slugs, not ids, so the stored preferences stay readable and
    survive a reseed.
    """
    slug = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=40)
    is_standard = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        ConvAIUser, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="contact_terms_added",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Standard first, then whatever the service has added, alphabetically —
        # so the familiar five never move as the list grows.
        ordering = ["-is_standard", "label"]

    def __str__(self):
        return self.label


class Patient(models.Model):
    name = models.TextField()
    lastname = models.TextField()
    phone_number = PhoneNumberField(blank=True, null=True)
    caregiver = models.ForeignKey(
        Caregiver,
        on_delete=models.SET_NULL,
        related_name="patient",
        null=True
    )
    navigator = models.ForeignKey(
        ConvAIUser,
        on_delete=models.SET_NULL,
        related_name="patients",
        null=True
    )

    details = models.TextField(blank=True, help_text="Markdown")
    details_editable = models.TextField(blank=True, help_text="Markdown")

    # How this person has said they want to be contacted, as a list of keys from
    # Patient.CONTACT_TERMS. Stored rather than assumed: the platform decides
    # when to ring people, and it should be doing so on terms they set. A list
    # keeps it additive — a new term is a new key, not a migration.
    contact_terms = models.JSONField(blank=True, default=list)

    care_plan = models.FileField(
        upload_to="care_plans/",
        blank=True,
        null=True,
        help_text="Sube aquí el Plan de Cuidado en PDF (máx. 5 MB)."
    )

    agent = models.ForeignKey(
        'Agent',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Which LangGraph agent will handle this patient's chat"
    )

    current_thread_id = models.CharField(
        max_length=36,
        blank=True,
        null=True,
        help_text="UUID4 of the active chat thread; resets on /quit"
    )

    # The navigator's switch for this client's agent. Per client rather than per
    # alert, which is where it used to live: two alerts about the same person
    # could disagree about whether their agent was running, and only one of them
    # could be right. Turning it off stops the agent replying — see
    # process_message_for_patient — it does not stop messages arriving.
    chatbot_enabled = models.BooleanField(
        default=True,
        help_text="When off, the agent stops replying to this client's messages."
    )
    chatbot_off_reason = models.CharField(max_length=200, blank=True)
    chatbot_off_at = models.DateTimeField(null=True, blank=True)
    chatbot_off_by = models.ForeignKey(
        'ConvAIUser', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='chatbots_disabled'
    )

    tester_account = models.OneToOneField(
        ConvAIUser,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="test_patient",
        help_text="User in group 'PatientTester' linked to this patient"
    )

    # --- Automation lifecycle -------------------------------------------------
    # While an automation (e.g. the Protocol QA agent) is running, `agent` points
    # at the automation agent and these fields remember the context to re-inject
    # on every inbound turn plus how to revert once it finishes or times out.
    automation_prev_agent = models.ForeignKey(
        'Agent',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        help_text="Agent to restore when the running automation ends."
    )
    automation_meeting = models.ForeignKey(
        'Meeting',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="+",
        help_text="Meeting the running automation is collecting answers for."
    )
    automation_protocol = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Protocol number the running automation is working through."
    )
    automation_expires_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Sliding idle deadline for the automation; empty means no automation is active."
    )

    @property
    def automation_active(self) -> bool:
        """True while an automation is assigned (context present, not yet reverted)."""
        return self.automation_expires_at is not None

    def append_details_markdown(self, text: str, *, editable: bool = True) -> str:
        """
        Append-only audit-ish log in markdown.

        - editable=True  -> writes to details_editable
        - editable=False -> writes to details

        Returns the full updated markdown string.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("Cannot append empty text.")

        ts = timezone.localtime(timezone.now())
        # Human timestamp (pick any format you like)
        ts_human = ts.strftime("%d %b %Y, %H:%M")  # e.g. "13 Feb 2026, 14:05"

        entry = (
            "\n\n---\n"
            f"**{ts_human}**\n\n"
            f"{text}\n"
        )

        if editable:
            current = self.details_editable or ""
            self.details_editable = (current + entry).lstrip("\n")
            return self.details_editable
        else:
            current = self.details or ""
            self.details = (current + entry).lstrip("\n")
            return self.details


    def __str__(self):
        return f"{self.name} {self.lastname}"

class Meeting(models.Model):
    class MeetingType(models.IntegerChoices):
        REGULAR    = 1, _("Protocol")
        ONBOARDING = 0, _("Onboarding")
        FINAL      = 2, _("Final")
        INITIAL    = 3, _("Initial")
        SEGUIMIENTO = 4, _("Follow-up")

    class Status(models.IntegerChoices):
        PENDING   = 0, _("Pending")
        COMPLETED = 1, _("Complete")
        INTERRUPTED = 2, _("Interrupted")
        NOT_ANSWERED = 3, _("Not answered")
        # A call that was planned and deliberately did not happen. It leaves
        # the queue but stays in the history: deleting it would make the record
        # say the call was never arranged, which is a different fact.
        CANCELLED = 4, _("Cancelled")

    cancel_reason = models.CharField(max_length=200, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # When someone recorded how the call went. Distinct from scheduled_time,
    # which is when it was meant to happen: a call recorded late — or early,
    # from a diary entry days ahead — would otherwise sit in Happened under a
    # date it did not happen on, sometimes one still in the future.
    ended_at = models.DateTimeField(null=True, blank=True)

    class Modality(models.IntegerChoices):
        """How the meeting happens.

        Everything before this was a phone bridge between the navigator and the
        caregiver, so PHONE is the default and existing rows keep their meaning.
        An in-person meeting has somewhere to be instead of a number to ring,
        which is why `location` travels with it.
        """
        PHONE = 0, _("Phone call")
        IN_PERSON = 1, _("In person")

    class Protocol(models.IntegerChoices):
        PROTOCOL_1  = 1, _("1. Protocol 1")
        PROTOCOL_2  = 2, _("2. Protocol 2")
        PROTOCOL_3  = 3, _("3. Protocol 3")
        PROTOCOL_4  = 4, _("4. Protocol 4")
        PROTOCOL_5  = 5, _("5. Protocol 5")
        PROTOCOL_6  = 6, _("6. Protocol 6")
        PROTOCOL_7  = 7, _("7. Protocol 7")
        PROTOCOL_8  = 8, _("8. Protocol 8")
        FINAL_CALL  = 9, _("9. Final call")
        SEGUIMIENTO = 10, _("10. Follow-up")

    modality = models.IntegerField(
        choices=Modality.choices,
        default=Modality.PHONE,
        help_text="Phone call or in-person meeting",
    )
    location = models.CharField(
        max_length=200,
        blank=True,
        help_text="Where an in-person meeting takes place",
    )

    scheduled_protocol = models.IntegerField(
        choices=Protocol.choices,
        null=True,
        blank=True,
        help_text="Protocolo programado (1–8) o llamada final (9)"
    )
    executed_protocol = models.IntegerField(
        choices=Protocol.choices,
        null=True,
        blank=True,
        help_text="Protocolo ejecutado (1–8) o llamada final (9)"
    )

    scheduled_time = models.DateTimeField()
    created_time = models.DateTimeField(auto_now_add=True)
    patient = models.ForeignKey(
        Patient,
        on_delete=models.CASCADE,
        related_name="meetings"
    )

    type = models.IntegerField(
        choices=MeetingType.choices,
        default=MeetingType.REGULAR,
        help_text="0 = Onboarding, 1 = Regular, 2 = Final, 3 = Initial"
    )

    status = models.IntegerField(
        choices=Status.choices,
        default=Status.PENDING,
        help_text="0 = pending, 1 = completed, 2 = interrupted, 3 = not answered"
    )

    retries = models.PositiveIntegerField(default=0, help_text="Retries")

    step_0 = models.BooleanField(default=False)
    step_1 = models.BooleanField(default=False)
    step_2 = models.BooleanField(default=False)
    step_3 = models.BooleanField(default=False)
    step_4 = models.BooleanField(default=False)
    step_5 = models.BooleanField(default=False)
    step_6 = models.BooleanField(default=False)
    step_7 = models.BooleanField(default=False)
    step_8 = models.BooleanField(default=False)

    # LLM summary of the protocol(s) filled during this meeting (filled on demand).
    protocol_summary = models.TextField(blank=True, default="", help_text="LLM summary of this meeting's protocol answers")
    protocol_summarized_at = models.DateTimeField(null=True, blank=True)

    # Free-text notes are Note rows (see the Note model), not a field here. The
    # single overwritten blob that used to live at Meeting.notes was migrated
    # away in 0067 and the columns dropped in 0072.

    @property
    def happened_at(self):
        """When this meeting actually became a past event.

        Lists of what has happened order and date themselves by this. The panel
        still shows scheduled_time, because when it was meant to be is a
        different fact and worth keeping.
        """
        return self.ended_at or self.cancelled_at or self.scheduled_time

    @property
    def panel_token(self):
        """What ?item= must be for this meeting's detail panel.

        Matches the tokens views/patients.py builds for timeline rows, so any
        list can mark its open row with `panel_item.token == x.panel_token`.
        """
        return f"meeting-{self.pk}"

    class Meta:
        ordering = ["-scheduled_time"]

    def __str__(self):
        return f"Meeting with {self.patient} at {self.scheduled_time}"


class Protocol(models.Model):
    """A self-contained protocol (1 … 8, llamada final, etc.)."""
    number      = models.PositiveSmallIntegerField(unique=True)
    title       = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["number"]
        verbose_name        = "Protocol"
        verbose_name_plural = "Protocols"

    def __str__(self):
        return f"{self.number}. {self.title}"


class Question(models.Model):
    """A single markdown question belonging to a protocol."""
    protocol   = models.ForeignKey(
        Protocol, related_name="questions", on_delete=models.CASCADE
    )
    order      = models.PositiveSmallIntegerField()
    prompt_md  = models.TextField(help_text="Explicación / pregunta en Markdown")

    class Meta:
        ordering        = ["protocol", "order"]
        unique_together = [("protocol", "order")]

    def __str__(self):
        return f"P{self.protocol.number}-Q{self.order}"


class Answer(models.Model):
    """
    One answer per (meeting, question) pair.
    Blank answers are not stored (view logic deletes row if left empty).
    """
    # Whether the caregiver texted this back or a navigator typed it. The panel
    # tints the two differently: on a call half answered by text, whose words
    # these are changes what you do with them.
    by_text = models.BooleanField(
        default=False,
        help_text="True when the protocol_qa automation captured this from a message",
    )
    meeting  = models.ForeignKey(
        "Meeting", related_name="answers", on_delete=models.CASCADE
    )
    question = models.ForeignKey(
        Question, related_name="answers", on_delete=models.CASCADE
    )
    response = models.TextField()

    class Meta:
        unique_together = [("meeting", "question")]
        ordering = ["question__order"]

class Agent(models.Model):
    class Kind(models.TextChoices):
        # 'remote' agents run on a separate LangGraph server (host:port).
        REMOTE = "remote", _("Remote")
        # 'native' agents ship with Conversational Care and run in-process.
        NATIVE = "native", _("Native")
        # 'prompt' agents are user-created, run in-process, and use a stored
        # system prompt with no tools.
        PROMPT = "prompt", _("Prompt-based")

    name = models.CharField(max_length=100, unique=True)

    kind = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.REMOTE, db_index=True,
        help_text=_("Remote = runs on a LangGraph server (host:port). "
                    "Native = ships with the platform. "
                    "Prompt-based = in-process agent driven by a stored prompt."),
    )
    native_key = models.CharField(
        max_length=64, blank=True, default="",
        help_text=_("For native agents: which built-in graph to run "
                    "(e.g. 'loopback', 'link_worker')."),
    )

    # For prompt-based agents: the system prompt used to drive the LLM.
    system_prompt = models.TextField(
        blank=True, default="",
        help_text=_("System prompt for prompt-based agents. Used as the system "
                    "message on every turn."),
    )

    # For prompt-based agents: converse over the GPT Realtime API (speech-to-
    # speech) instead of the text chat + STT/TTS pipeline. The test page and the
    # tester chat render a live voice-call UI for these agents. Configured under
    # Settings → Agents → Azure OpenAI Realtime.
    realtime_enabled = models.BooleanField(
        default=False,
        help_text=_("Prompt-based agents only: chat through the real-time voice "
                    "API (live speech in/out) instead of the text chat."),
    )

    # Model behind in-process agents (native + prompt-based). Blank uses the
    # platform default (DEFAULT_AGENT_MODEL). May be provider-prefixed, e.g.
    # 'openai/gpt-4.1-mini', 'anthropic/claude-sonnet-4-6'. Under USE_AZURE it is
    # the Azure deployment name. Ignored by remote agents. See ConvAI.llm_factory.
    model = models.CharField(
        max_length=200, blank=True, default="",
        help_text=_("Model behind this agent, e.g. 'openai/gpt-4.1-mini' or "
                    "'anthropic/claude-sonnet-4-6'. Blank uses the platform default. "
                    "Not used by remote agents."),
    )

    # Remote-agent connection details (unused for native agents).
    langgraph_name = models.CharField(max_length=100, blank=True, default="")
    host = models.CharField(max_length=100, blank=True, default="")
    port = models.PositiveIntegerField(null=True, blank=True)

    # Who the LLM should imitate (role/persona)
    classification_role = models.TextField(
        blank=True,
        max_length=4096,
        help_text=(
            "Role/persona to imitate for classification, e.g. "
            "'A senior Peruvian nurse technician trained in dementia caregiver triage'."
            "Also, include instructions on when should this be considered important that a human reviews the conversation.\n"
            "Continue the frase 'You are ...'"
        ),
    )

    # How the abstract should be written
    abstract_instruction = models.CharField(
        max_length=1024,
        blank=True,
        default="≤80 words summary, in the conversation language.",
        help_text="Instruction for producing the abstract.",
    )

    # Detector definitions: label → instruction on how to detect
    detectors = models.JSONField(
        default=dict,
        blank=True,
        help_text="JSON object mapping detector label → instruction/prompt.",
    )

    tts_voice_id = models.CharField(max_length=40, blank=True, null=True)

    def __str__(self):
        return f"{self.name} @ {self.host}:{self.port}"


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    started_at = models.DateTimeField(default=timezone.now)
    last_message_at = models.DateTimeField(default=timezone.now)
    patient = models.ForeignKey(Patient, null=True, blank=True, on_delete=models.SET_NULL, db_index=True)
    agent   = models.ForeignKey(Agent,   null=True, blank=True, on_delete=models.SET_NULL, db_index=True)
    # WB-01 fix: bind a conversation/thread to the owning user so threads are not shared globally.
    user    = models.ForeignKey('ConvAIUser', null=True, blank=True, on_delete=models.SET_NULL,
                                db_index=True, related_name='conversations')
    # feedback
    rating   = models.PositiveSmallIntegerField(null=True, blank=True)
    feedback = models.TextField(blank=True)

    summary       = models.TextField(blank=True, help_text="Automatic short abstract")
    topic         = models.CharField(max_length=120, blank=True, help_text="Classification label/topic")
    is_important  = models.BooleanField(default=False, db_index=True, help_text="Requires human attention?")
    visited       = models.BooleanField(default=False, db_index=True, help_text="Has been reviewed in dashboard?")
    analyzed      = models.BooleanField(default=False, db_index=True, help_text="Has analysis been run?")
    analyzed_at   = models.DateTimeField(null=True, blank=True)

    auto_flags = models.JSONField(
        default=dict, blank=True,
        help_text="Auto detector booleans keyed by label (produced by automatic classification)."
    )
    human_flags = models.JSONField(
        default=dict, blank=True,
        help_text="Human validation of detector booleans, keyed by the same labels."
    )

    class Meta:
        ordering = ["-last_message_at"]
        indexes = [
            models.Index(fields=["is_important", "visited"]),
            models.Index(fields=["analyzed"]),
            models.Index(fields=["last_message_at"]),
        ]

    def __str__(self):
        return f"{self.id}"


class SelfRegistration(models.Model):
    class State(models.IntegerChoices):
        REGISTERED = 0, "Registered"
        APPROVED   = 1, "Approved"

    name         = models.CharField(max_length=120)
    lastname     = models.CharField(max_length=120)
    phone_number = PhoneNumberField(blank=True, null=True)
    email        = models.EmailField(blank=True, null=True)
    details      = models.JSONField(blank=True, default=dict, help_text="Arbitrary extra fields for this application")
    state        = models.IntegerField(choices=State.choices, default=State.REGISTERED, db_index=True)

    created_at   = models.DateTimeField(default=timezone.now)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["state", "created_at"])]

    def __str__(self):
        return f"{self.name} {self.lastname} ({self.get_state_display()})"


### Alerting system

class Alert(models.Model):
    class AlertType(models.IntegerChoices):
        DEFAULT = 0, "Default"
        CONVERSATION = 1, "Conversation"
        EVENT = 2, "Event"
        API_REGISTERED = 3, "API Registered"

    class AlertStatus(models.IntegerChoices):
        CREATED = 0, "Created"
        IN_PROGRESS = 1, "In Progress"
        RESOLVED = 2, "Resolved"

    # Minor typo fix: Prioryty -> Priority
    class Priority(models.IntegerChoices):
        HIGH = 1, "High"
        MEDIUM = 2, "Medium"
        LOW = 3, "Low"

    priority = models.IntegerField(choices=Priority.choices, default=Priority.LOW)
    status = models.IntegerField(choices=AlertStatus.choices, default=AlertStatus.CREATED)
    title = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    data = models.JSONField(blank=True, default=dict, help_text="Additional data for the alert")
    user = models.ForeignKey(ConvAIUser, on_delete=models.CASCADE, null=True, blank=True, related_name="alerts_assigned")
    patient = models.ForeignKey('Patient', on_delete=models.CASCADE, null=True, blank=True, related_name="alerts")

    # Who created this alert (kept for audit)
    created_by = models.ForeignKey(ConvAIUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts_created")
    alert_type = models.IntegerField(choices=AlertType.choices, default=AlertType.DEFAULT)

    # Webhook support (optional)
    #webhook_url = models.URLField(blank=True, null=True)
    acted_at = models.DateTimeField(blank=True, null=True)
    last_action_ok = models.BooleanField(default=False)
    last_action_status = models.IntegerField(blank=True, null=True)
    last_action_body = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["alert_type"]),
            models.Index(fields=["-created_at"]),
        ]
        constraints = [
            # Ensure at least one of (user, patient) is present
            models.CheckConstraint(
                name="alert_has_user_or_patient",
                check=~(Q(user__isnull=True) & Q(patient__isnull=True)),
            ),
        ]

    def clean(self):
        # App-level guard mirroring DB constraint; gives a friendly error
        if not self.user and not self.patient:
            raise ValidationError("An alert must reference a user, a patient, or both.")

    def __str__(self):
        return f"[{self.get_priority_display()}] {self.title or 'Alert'} ({self.get_status_display()})"


# --- Runtime configuration overrides ---------------------------------------
# Singleton row whose non-empty fields override the matching .env values at
# runtime. Resolution + fallback logic lives in ConvAI/site_config.py.

SITE_CONFIG_CACHE_KEY = "site_configuration_singleton"

# Tri-state used for boolean flags so a blank value means "use the .env default".
_HX_SID = RegexValidator(r"^HX[0-9a-fA-F]{32}$", _("Must be a Twilio SID like HX + 32 hex characters."))
_E164 = RegexValidator(r"^\+?[1-9]\d{6,14}$", _("Enter a phone number in E.164 format, e.g. +447700900000."))


class SiteConfiguration(models.Model):
    """Single-row table of runtime overrides for environment configuration."""

    TRISTATE = [
        ("", _("Use .env default")),
        ("1", _("On")),
        ("0", _("Off")),
    ]

    # --- Behaviour / feature flags (live) ---
    hide_meeting_steps = models.CharField(max_length=1, choices=TRISTATE, blank=True, default="")
    enable_automations = models.CharField(max_length=1, choices=TRISTATE, blank=True, default="")
    self_registration_enabled = models.CharField(max_length=1, choices=TRISTATE, blank=True, default="")
    self_reg_agent_name = models.CharField(max_length=255, blank=True, default="")
    send_care_plan = models.CharField(max_length=1, choices=TRISTATE, blank=True, default="")
    whatsapp_audio_enabled = models.CharField(max_length=1, choices=TRISTATE, blank=True, default="")

    # --- Integrations (secrets, live) ---
    twilio_account_sid = models.CharField(max_length=255, blank=True, default="")
    twilio_auth_token = models.CharField(max_length=255, blank=True, default="")
    platform_phone = models.CharField(max_length=20, blank=True, default="", validators=[_E164])
    openai_api_key = models.CharField(max_length=255, blank=True, default="")
    elevenlabs_api_key = models.CharField(max_length=255, blank=True, default="")
    elevenlabs_voice_id = models.CharField(max_length=255, blank=True, default="")

    # --- Messaging templates (live) ---
    twilio_sms_from = models.CharField(max_length=20, blank=True, default="", validators=[_E164])
    sms_template_start_infection_sid = models.CharField(max_length=34, blank=True, default="", validators=[_HX_SID])
    sms_template_start_infection_text = models.TextField(blank=True, default="")
    whatsapp_template_care_plan_sid = models.CharField(max_length=34, blank=True, default="", validators=[_HX_SID])

    # --- Branding (live) ---
    brand_name = models.CharField(max_length=255, blank=True, default="")
    brand_logo = models.CharField(max_length=255, blank=True, default="")
    brand_logo_file = models.FileField(
        upload_to="branding/", blank=True, null=True,
        validators=[FileExtensionValidator(["png", "jpg", "jpeg", "svg", "webp", "gif"])],
    )

    # --- Agent models / LLM providers (live) ---
    # Default model for in-process agents when an Agent has no explicit model.
    default_agent_model = models.CharField(max_length=200, blank=True, default="")
    # Provider API keys (secrets). Blank falls back to the environment.
    anthropic_api_key = models.CharField(max_length=255, blank=True, default="")
    google_api_key = models.CharField(max_length=255, blank=True, default="")
    mistral_api_key = models.CharField(max_length=255, blank=True, default="")
    deepseek_api_key = models.CharField(max_length=255, blank=True, default="")
    # Azure routing (tri-state; blank = follow the .env value).
    use_azure = models.CharField(max_length=1, choices=TRISTATE, blank=True, default="")
    azure_openai_endpoint = models.CharField(max_length=255, blank=True, default="")
    azure_openai_api_key = models.CharField(max_length=255, blank=True, default="")
    azure_openai_api_version = models.CharField(max_length=64, blank=True, default="")
    azure_anthropic_endpoint = models.CharField(max_length=255, blank=True, default="")
    azure_anthropic_api_key = models.CharField(max_length=255, blank=True, default="")
    azure_mistral_endpoint = models.CharField(max_length=255, blank=True, default="")
    azure_mistral_api_key = models.CharField(max_length=255, blank=True, default="")
    azure_deepseek_endpoint = models.CharField(max_length=255, blank=True, default="")
    azure_deepseek_api_key = models.CharField(max_length=255, blank=True, default="")
    # Azure OpenAI Realtime (speech-to-speech voice agents). Endpoint/key blank
    # falls back to the Azure OpenAI endpoint/key above.
    azure_realtime_endpoint = models.CharField(max_length=255, blank=True, default="")
    azure_realtime_api_key = models.CharField(max_length=255, blank=True, default="")
    azure_realtime_deployment = models.CharField(max_length=100, blank=True, default="")
    azure_realtime_voice = models.CharField(max_length=40, blank=True, default="")
    # Only needed when the resource exposes just the preview Realtime API: the
    # Azure region hosting the preview WebRTC gateway (e.g. 'swedencentral').
    azure_realtime_webrtc_region = models.CharField(max_length=40, blank=True, default="")

    # --- Editable content (live) ---
    # Markdown source for the Help page. Blank falls back to the shipped default
    # (see ConvAI.default_help.DEFAULT_HELP_MARKDOWN) until an admin edits it.
    help_markdown = models.TextField(blank=True, default="")

    # --- Agent Hosts (live) ---
    agent_allowed_hosts = models.TextField(blank=True, default="", help_text="Comma-separated list of allowed agent hosts")

    # --- Summarization prompts (live) ---
    # Base prompts used to post-process meeting protocols and call transcripts.
    # Blank falls back to the shipped defaults in ConvAI.default_prompts.
    meeting_summary_prompt = models.TextField(blank=True, default="", help_text="Base prompt for summarizing a meeting's protocol answers")
    transcript_summary_prompt = models.TextField(blank=True, default="", help_text="Base prompt for summarizing a call transcript")
    transcript_moments_prompt = models.TextField(blank=True, default="", help_text="Base prompt for pulling key moments out of a call transcript")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site configuration"
        permissions = [("access_configuration", "Can access configuration")]

    def __str__(self):
        return "Site configuration"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce singleton
        super().save(*args, **kwargs)
        cache.delete(SITE_CONFIG_CACHE_KEY)

    def delete(self, *args, **kwargs):  # pragma: no cover - guard
        pass  # never delete the singleton

    @classmethod
    def load(cls):
        obj = cache.get(SITE_CONFIG_CACHE_KEY)
        if obj is None:
            obj, _created = cls.objects.get_or_create(pk=1)
            cache.set(SITE_CONFIG_CACHE_KEY, obj, 30)
        return obj

class SeenMark(models.Model):
    """One navigator has opened one system-created item.

    Only alerts and chatbot conversations carry this: a call you scheduled
    yourself was never news to you, so it has nothing to be read.

    Keyed by the panel's own token (``alert-12``, ``chat-3-2026-08-04``) rather
    than a foreign key, because a chat "item" is a patient and a day rather than
    a row — there is no table to point at. The panel already builds that token,
    so nothing new has to agree on a format.

    Per user on purpose. A shared flag would mean a supervisor opening the queue
    clears the mark for the navigator who still has not seen it.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="seen_marks"
    )
    token = models.CharField(max_length=64)
    seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "token"], name="seenmark_once_per_user"),
        ]
        indexes = [models.Index(fields=["user", "token"])]

    def __str__(self):
        return f"{self.user_id}:{self.token}"

    @classmethod
    def mark(cls, user, token):
        """Record that this user has opened this item. Idempotent."""
        if not (user and getattr(user, "is_authenticated", False) and token):
            return
        if not token.startswith(("alert-", "chat-")):
            return
        cls.objects.update_or_create(user=user, token=token)

    @classmethod
    def seen_tokens(cls, user, tokens):
        """The subset of `tokens` this user has already opened."""
        if not (user and getattr(user, "is_authenticated", False) and tokens):
            return set()
        return set(
            cls.objects.filter(user=user, token__in=list(tokens))
            .values_list("token", flat=True)
        )


class Note(models.Model):
    """Something a person wrote about one call, meeting, alert or conversation.

    Before this there was a single ``Meeting.notes`` text field, overwritten on
    every save: no author, no time, one note per meeting and none at all for the
    other three kinds. A note is a small record with a person attached, so it is
    a row.

    The parent is an explicit nullable FK per kind rather than a generic
    relation. It is more columns, but the queries stay simple, the database
    keeps the integrity, and permission checks can follow the parent object
    through code that already knows how to authorise it.
    """

    body = models.TextField()
    author = models.ForeignKey(
        'ConvAIUser', null=True, blank=True, on_delete=models.SET_NULL,
        related_name="notes_written",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    meeting = models.ForeignKey(
        Meeting, null=True, blank=True, on_delete=models.CASCADE, related_name="notes_list")
    recording = models.ForeignKey(
        CallRecording, null=True, blank=True, on_delete=models.CASCADE, related_name="notes_list")
    alert = models.ForeignKey(
        Alert, null=True, blank=True, on_delete=models.CASCADE, related_name="notes_list")
    conversation = models.ForeignKey(
        Conversation, null=True, blank=True, on_delete=models.CASCADE, related_name="notes_list")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self):
        return f"Note {self.pk} by {self.author or 'unknown'}"

    @property
    def parent(self):
        return self.meeting or self.recording or self.alert or self.conversation

    @property
    def patient(self):
        """The client a note belongs to, whichever kind it hangs off.

        Permissions are decided per client, so every note has to be able to name
        one without the caller knowing which parent it has.
        """
        parent = self.parent
        if parent is None:
            return None
        if isinstance(parent, CallRecording):
            # A recording carries phone numbers rather than a client FK, so it
            # is matched the same way views/summaries.py matches it.
            nums = {str(parent.to_number or ""), str(parent.from_number or "")}
            nums.discard("")
            if not nums:
                return None
            return Patient.objects.filter(
                models.Q(phone_number__in=nums) | models.Q(caregiver__phone_number__in=nums)
            ).first()
        return getattr(parent, "patient", None)
