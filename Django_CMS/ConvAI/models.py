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
    class Leg(models.IntegerChoices):
        """Which side of a conference this is a recording of.

        A conference is two calls, not one — see make_phone_conference. One
        goes out to the client side and one to the navigator's own phone, and
        both are recorded. Only the first is a record of the client.
        """
        DYAD = 0, _("Client side")
        CTN = 1, _("Navigator side")

    recording_sid = models.CharField(max_length=100)
    from_number = models.CharField(max_length=100)
    to_number = models.CharField(max_length=100)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    duration = models.IntegerField()
    filename = models.CharField(max_length=100, null=True)

    # Whose recording this is, and which call it came from.
    #
    # The two phone numbers above used to be the only answer to both questions,
    # and a phone number is not an identity. It cannot say *which* client: one
    # number can belong to two of them — a caregiver who looks after one client
    # and is themself another, or a shared household line — and the same call
    # was then drawn on both timelines. It cannot even say whether a client is
    # involved at all: the navigator's own leg of a conference has a staff
    # number on it, and matching on numbers filed it against whichever client
    # happened to share that number.
    #
    # None of this ever had to be inferred. Twilio hands back a Call SID for
    # each leg at the moment it is placed, when the meeting and the client are
    # both in hand; see CallLeg, which is where that is written down, and
    # get_recordings_from_twilio, which joins the audio back to it on call_sid.
    #
    # All four stay nullable. Recordings made before any of this exists have
    # none of it and must keep rendering, so every surface falls back to
    # matching numbers for rows where `patient` is null.
    meeting = models.ForeignKey(
        'Meeting', null=True, blank=True, on_delete=models.SET_NULL,
        related_name="recordings",
        help_text=_("The call this recording came from, where it is known."),
    )
    patient = models.ForeignKey(
        'Patient', null=True, blank=True, on_delete=models.SET_NULL,
        related_name="recordings",
        help_text=_("Whose recording this is. Kept alongside the meeting rather "
                    "than read through it, so a call placed outside a meeting "
                    "still has an owner."),
    )
    leg = models.SmallIntegerField(
        choices=Leg.choices, null=True, blank=True,
        help_text=_("Which side of the conference this recording is of."),
    )
    call_sid = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text=_("Twilio's Call SID — the join back to the leg that was placed."),
    )

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

    @property
    def is_navigator_leg(self):
        """True when this is a recording of the navigator's own phone.

        Their leg is close to a duplicate of the client's — that one is
        dual-channel and already carries both sides of the conference — and it
        is never a record of contact with the client, so client-facing lists
        leave it out.
        """
        return self.leg == self.Leg.CTN

    def owner_patients(self):
        """Every client this recording could belong to.

        Exactly one where the recording names one. Where it does not, this is
        the old rule — match the numbers — and it can legitimately return more
        than one client, which is the whole problem the relation above exists to
        stop. Callers that need a single answer use resolve_patient; callers
        deciding whether someone may reach the audio use this, because a legacy
        recording on a shared number belongs, as far as anything can tell, to
        every client on that number.
        """
        if self.patient_id:
            return Patient.objects.filter(pk=self.patient_id)
        nums = {str(self.to_number or ""), str(self.from_number or "")}
        nums.discard("")
        if not nums:
            return Patient.objects.none()
        return Patient.objects.filter(
            Q(phone_number__in=nums) | Q(caregiver__phone_number__in=nums)
        )

    def resolve_patient(self):
        """The one client this recording belongs to, or None.

        Prefers what the call wrote down when it was placed. Falls back to
        matching numbers only for rows that have nothing written down, where it
        picks the first of possibly several — a guess, kept because a legacy
        recording nobody can reach is worse than one filed under the wrong name.
        """
        if self.patient_id:
            return self.patient
        return self.owner_patients().select_related("caregiver", "navigator").first()

    @classmethod
    def for_patient(cls, patient, numbers=()):
        """This client's recordings, newest first.

        Two rules rather than one. A recording that names its client is that
        client's and nobody else's — that is what the relation is for, and a
        number it happens to share with someone else no longer drags it onto
        their timeline. A recording that names nobody falls back to matching
        `numbers`, which is how rows made before the relation existed still find
        their way home.

        The navigator's own leg is left out of both. It is filed under the call
        it belongs to and reachable from there, but it is a recording of staff
        and was never this client's contact history.
        """
        cond = Q(patient=patient)
        nums = [n for n in (numbers or ()) if n]
        if nums:
            cond |= Q(patient__isnull=True, to_number__in=nums)
        return (cls.objects
                .filter(cond)
                .exclude(leg=cls.Leg.CTN)
                .order_by('-start_time'))


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
    # Where an email reminder goes when REMINDER_CHANNEL is 'email'. The
    # caregiver is tried first and the client second, because the caregiver is
    # who the call is actually arranged with.
    email = models.EmailField(blank=True, default="")
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
    # Fallback recipient for email reminders when the caregiver has no address.
    email = models.EmailField(blank=True, default="")
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

    # The protocols this person actually works through — their programme.
    #
    # The panel used to list every protocol in the platform for every client,
    # because existing was the only thing that put one on screen. Which
    # protocols apply to someone is a decision about them, so it is recorded
    # against them. Empty on a new client on purpose: an empty panel asks the
    # question, a full one answers it wrongly.
    protocols = models.ManyToManyField(
        'Protocol',
        blank=True,
        related_name='patients',
        help_text="Protocols this client works through. Shown in their call panel.",
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
        """Legacy. Frozen — do not add to it, do not offer it to anyone.

        These labels are placeholders that were never the names of any real
        protocol: a picker built from them offered eight protocols nobody had
        created and could never offer an eleventh that someone had. What a call
        covers now lives in `scheduled_protocols` / `executed_protocols`, which
        point at actual Protocol records.

        Kept only so the two integer columns below still validate while they
        wait to be dropped.
        """
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

    class DialTarget(models.IntegerChoices):
        """Whose phone this call rings on the client side.

        Every call the platform had ever placed went to the caregiver — see
        make_phone_call — so CAREGIVER is 0 and every row written before this
        keeps meaning exactly what it did. CLIENT is new: a client with a phone
        of their own could not be reached at all, and "we rang Manuel himself"
        is a fact about the call worth keeping rather than one to be guessed
        back out of a phone number afterwards.
        """
        CAREGIVER = 0, _("Caregiver")
        CLIENT = 1, _("Client")

    modality = models.IntegerField(
        choices=Modality.choices,
        default=Modality.PHONE,
        help_text="Phone call or in-person meeting",
    )
    dial_target = models.IntegerField(
        choices=DialTarget.choices,
        default=DialTarget.CAREGIVER,
        help_text="Which of the client's two numbers the bridge rings",
    )
    # A call placed from the client page rather than one that was booked.
    #
    # It is a Meeting like any other, because that is what makes it a call the
    # platform can hold: the recording is attributed through it, it carries the
    # protocols and the notes, and it is closed with an outcome like the rest.
    # What this flag says is only that nobody arranged it beforehand — so the
    # lists can stop calling it a "Scheduled call", which is the one thing it
    # is not.
    unscheduled = models.BooleanField(
        default=False,
        help_text="Placed on the spot rather than booked in advance",
    )
    location = models.CharField(
        max_length=200,
        blank=True,
        help_text="Where an in-person meeting takes place",
    )

    # What this call covers, and what it turned out to cover.
    #
    # Both were a single integer against the placeholder list above, so a call
    # that worked through the session note and the IQCODE had to claim it did
    # one of them. They are relations now, and they point at protocols that
    # exist.
    scheduled_protocols = models.ManyToManyField(
        'Protocol',
        blank=True,
        related_name='scheduled_meetings',
        help_text="Protocols this call is booked to address.",
    )
    executed_protocols = models.ManyToManyField(
        'Protocol',
        blank=True,
        related_name='executed_meetings',
        help_text="Protocols actually covered, recorded when the call is closed.",
    )

    # Legacy, read once by migration 0077 and never written again. They are the
    # single-protocol version of the two relations above and are scheduled for
    # removal; nothing should read them.
    scheduled_protocol = models.IntegerField(
        choices=Protocol.choices,
        null=True,
        blank=True,
        help_text="Deprecated — superseded by scheduled_protocols.",
    )
    executed_protocol = models.IntegerField(
        choices=Protocol.choices,
        null=True,
        blank=True,
        help_text="Deprecated — superseded by executed_protocols.",
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
    def dial_recipient(self):
        """Who this call rings, or None when there is nobody to ring.

        The two sides of dial_target are different kinds of object — a
        Caregiver row and the Patient themself — and every surface that asks
        "who is on this call" wants the same three things off either one. So
        they are answered here rather than by an `if` repeated in the view, the
        panel and the picker.

        None when the chosen side has no number: a caregiver who was never
        recorded, or a client whose own number is blank. That is the same
        answer as "this call cannot be placed", which is what the callers do
        with it.
        """
        if self.modality == Meeting.Modality.IN_PERSON:
            return None
        if self.dial_target == Meeting.DialTarget.CLIENT:
            who = self.patient
        else:
            who = self.patient.caregiver if self.patient_id else None
        return who if (who and who.phone_number) else None

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


class CallLeg(models.Model):
    """One outbound call placed for one meeting.

    A conference is two calls — the navigator's phone and the client side — and
    Twilio answers each with a Call SID the instant it is placed. Those SIDs
    were being discarded: make_phone_conference had no return statement at all,
    so the one fact that ties a recording to the call it came from was created
    and thrown away, and every surface downstream was left to work it out again
    from phone numbers, which cannot.

    This is where that fact waits. The audio arrives minutes or hours later
    carrying nothing but its own SID and two numbers; get_recordings_from_twilio
    joins on call_sid and copies meeting, patient and leg onto the recording.

    A row here records a call that was placed, not a recording that exists. A
    leg nobody answered, or one placed with recording switched off, simply never
    gets one, and that is not a fault — it is what an unanswered call looks
    like.
    """

    call_sid = models.CharField(
        max_length=64, unique=True,
        help_text=_("Twilio's Call SID for this leg."),
    )
    meeting = models.ForeignKey(
        Meeting, on_delete=models.CASCADE, related_name="legs",
    )
    # Denormalised from the meeting for the same reason CallRecording keeps it:
    # so the answer survives the meeting being deleted, and so filling in a
    # recording costs one read rather than a join.
    patient = models.ForeignKey(
        Patient, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="call_legs",
    )
    leg = models.SmallIntegerField(choices=CallRecording.Leg.choices)
    to_number = models.CharField(max_length=100, blank=True, default="")
    conference_name = models.CharField(max_length=64, blank=True, default="")
    placed_at = models.DateTimeField(auto_now_add=True)
    placed_by = models.ForeignKey(
        'ConvAIUser', null=True, blank=True, on_delete=models.SET_NULL,
        related_name="call_legs_placed",
    )

    class Meta:
        ordering = ["-placed_at"]

    def __str__(self):
        return f"{self.get_leg_display()} leg {self.call_sid} of meeting {self.meeting_id}"


class Protocol(models.Model):
    """A self-contained protocol (1 … 8, llamada final, etc.)."""
    number      = models.PositiveSmallIntegerField(unique=True)
    title       = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    # Some protocols are asked once — the Basic Information Request, where
    # asking twice would be a mistake. Others are instruments meant to be
    # re-taken: the IQCODE compares someone with how they were, and a score
    # only means anything next to the last one.
    #
    # This changes how answers are *read*, never how they are stored — they
    # have always been kept one per question per call. A repeatable protocol
    # gives each call its own round with the earlier ones beneath it, and never
    # reads as finished.
    repeatable = models.BooleanField(
        default=False,
        help_text="This protocol is answered again on later calls, each call its own round.",
    )

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
        # 'sensei' agents forward the turn to the external Sensei service over
        # its REST endpoint (see ConvAI.sensei and agents.md). Deliberately a
        # kind of its own rather than a flavour of REMOTE: remote agents speak
        # LangGraph over host:port, Sensei speaks a small JSON operation
        # protocol over HTTPS, and the two share no connection settings.
        SENSEI = "sensei", _("Sensei")

    name = models.CharField(max_length=100, unique=True)

    # One line on what the agent is *for*, shown on its card on the Agents page
    # so the list reads as a roster rather than four names and a model id. Native
    # agents ship with one (seeded in migration 0082); every kind can edit it.
    description = models.CharField(
        max_length=200, blank=True, default="",
        help_text=_("One line on what this agent does. Shown on its card on the "
                    "Agents page, where about 90 characters fit."),
    )

    kind = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.REMOTE, db_index=True,
        help_text=_("Remote = runs on a LangGraph server (host:port). "
                    "Native = ships with the platform. "
                    "Prompt-based = in-process agent driven by a stored prompt. "
                    "Sensei = forwards the turn to the external Sensei service."),
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

    # For prompt-based agents: the RAG subtype. When on, the agent gains a
    # `search_documents` tool backed by the documents uploaded against it
    # (see RagDocument / RagChunk). Off = a plain prompt agent with no tools.
    rag_enabled = models.BooleanField(
        default=False,
        help_text=_("Prompt-based agents only: give the agent a searchable "
                    "knowledge base built from documents you upload."),
    )
    # How many chunks the retrieval tool returns per search.
    rag_top_k = models.PositiveSmallIntegerField(
        default=5, validators=[MinValueValidator(1)],
        help_text=_("How many document extracts the search tool returns per query."),
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
        # host:port only identifies a *remote* agent. Every other kind has none,
        # so the old unconditional form rendered "Loopback @ :None" in admin
        # dropdowns and anywhere else an agent is listed by name.
        if self.kind == self.Kind.REMOTE and self.host:
            return f"{self.name} @ {self.host}:{self.port}"
        return self.name


# ---------------------------------------------------------------------------
# RAG-based prompt agents (see agents.md → "RAG-based agents")
#
# The knowledge base is deliberately *lightweight*: no external vector store, no
# extra service. A document's text is split into chunks, each chunk is embedded
# once, and the vector is kept on the row as a packed float32 blob. Retrieval
# loads the (few thousand) vectors for one agent and scores them with numpy.
#
# Because the vectors live with the document, switching a document **off** is
# just a boolean — the embeddings are kept and never recomputed when it comes
# back on. Only deleting the document throws them away.
# ---------------------------------------------------------------------------
def _rag_upload_to(instance, filename):
    """Store uploads under ``media/rag_documents/<agent_id>/<uuid><ext>``.

    The stored name is randomised: two people may upload ``notes.pdf`` for the
    same agent, and the display name is kept separately in ``original_name``.
    """
    ext = os.path.splitext(filename)[1].lower()[:10]
    return os.path.join("rag_documents", str(instance.agent_id or "unassigned"),
                        f"{uuid.uuid4().hex}{ext}")


class RagDocument(models.Model):
    """One uploaded source document in a RAG agent's knowledge base.

    Ingestion (extract → chunk → embed) runs on the background pool and the
    progress fields below are the *only* record of it, so a browser that
    reloads — or a user who closes the tab — picks the job back up simply by
    reading these rows.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Queued")
        EXTRACTING = "extracting", _("Reading text")
        CHUNKING = "chunking", _("Splitting into chunks")
        EMBEDDING = "embedding", _("Computing vectors")
        READY = "ready", _("Ready")
        FAILED = "failed", _("Failed")

    # Statuses that mean "a worker should be on this right now". Used to spot
    # jobs orphaned by a restart (see `is_stalled`).
    ACTIVE_STATUSES = ("pending", "extracting", "chunking", "embedding")

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="rag_documents")
    file = models.FileField(
        upload_to=_rag_upload_to,
        validators=[FileExtensionValidator(allowed_extensions=["txt", "md", "pdf", "docx"])],
    )
    original_name = models.CharField(max_length=255)
    size_bytes = models.PositiveIntegerField(default=0)

    # Off = excluded from retrieval, vectors kept. On/off costs nothing.
    enabled = models.BooleanField(default=True, db_index=True)

    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    error = models.TextField(blank=True, default="")

    chunk_total = models.PositiveIntegerField(default=0)
    chunk_done = models.PositiveIntegerField(default=0)
    # Characters of extracted text — shown in the UI, and 0 means "nothing
    # readable in this file" (e.g. a scanned PDF with no text layer).
    char_count = models.PositiveIntegerField(default=0)

    # Which embedding model produced the stored vectors. Kept per document so a
    # later change of model is visible rather than silently mixing vector
    # spaces; mismatched documents are skipped at retrieval time.
    embedding_model = models.CharField(max_length=120, blank=True, default="")
    embedding_dim = models.PositiveIntegerField(default=0)

    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    # Touched on every progress tick, so it doubles as the worker's heartbeat.
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        # Named explicitly: retrieval and the knowledge-base page both filter on
        # exactly this triple, and an explicit name keeps the migration and the
        # model in step (an auto-generated one is a hash of the column list).
        indexes = [models.Index(fields=["agent", "enabled", "status"],
                                name="rag_doc_agent_enabled_idx")]

    def __str__(self):
        return f"{self.original_name} ({self.agent_id})"

    @property
    def progress(self) -> int:
        """Percent complete, 0-100, for the progress bar."""
        if self.status == self.Status.READY:
            return 100
        if self.status == self.Status.FAILED:
            return 0
        if self.status == self.Status.PENDING:
            return 0
        if self.status == self.Status.EXTRACTING:
            return 5
        if self.status == self.Status.CHUNKING:
            return 15
        if not self.chunk_total:
            return 20
        # Embedding spans 20→100%.
        return min(99, 20 + int(80 * self.chunk_done / self.chunk_total))

    def is_stalled(self, seconds: int = 300) -> bool:
        """True if this job claims to be running but its worker went away.

        A process restart (deploy, crash) leaves rows mid-ingest with nobody
        working them. The heartbeat in ``updated_at`` is how we tell.
        """
        if self.status not in self.ACTIVE_STATUSES:
            return False
        return (timezone.now() - self.updated_at).total_seconds() > seconds


class RagChunk(models.Model):
    """One embedded slice of a ``RagDocument``.

    ``embedding`` is the raw little-endian float32 vector, L2-normalised at
    write time so similarity is a plain dot product.
    """
    document = models.ForeignKey(RagDocument, on_delete=models.CASCADE, related_name="chunks")
    ordinal = models.PositiveIntegerField(default=0)
    text = models.TextField()
    embedding = models.BinaryField()

    class Meta:
        ordering = ["document_id", "ordinal"]
        indexes = [models.Index(fields=["document", "ordinal"],
                                name="rag_chunk_doc_ordinal_idx")]

    def __str__(self):
        return f"{self.document_id}#{self.ordinal}"


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

    # Every choice list below keeps a blank first entry with the same meaning as
    # TRISTATE's: "no override, use whatever .env says".
    EMAIL_PROVIDERS = [
        ("", _("Use .env default")),
        ("azure", _("Azure Communication Services")),
        ("smtp", _("SMTP")),
    ]

    SMTP_SECURITY = [
        ("", _("Use .env default")),
        ("tls", _("STARTTLS (port 587)")),
        ("ssl", _("SSL/TLS (port 465)")),
        ("none", _("None")),
    ]

    REMINDER_CHANNELS = [
        ("", _("Use .env default")),
        ("whatsapp", _("WhatsApp")),
        ("email", _("Email")),
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

    # --- Email (live) ---
    # Which provider carries outbound mail. Everything the platform sends —
    # password-reset links, meeting reminders, the test message — goes through
    # ConvAI.mailer.PlatformEmailBackend, which reads these on every send, so
    # changing provider or credentials here needs no restart.
    email_provider = models.CharField(max_length=10, choices=EMAIL_PROVIDERS, blank=True, default="")
    email_from = models.CharField(max_length=254, blank=True, default="")
    email_from_name = models.CharField(max_length=120, blank=True, default="")
    email_reply_to = models.CharField(max_length=254, blank=True, default="")
    # Azure Communication Services Email. Either paste the whole connection
    # string, or give the endpoint and access key and let the platform assemble
    # one from them.
    azure_email_connection_string = models.CharField(max_length=500, blank=True, default="")
    azure_email_endpoint = models.CharField(max_length=255, blank=True, default="")
    azure_email_access_key = models.CharField(max_length=500, blank=True, default="")
    # SMTP. Port is a CharField so blank keeps the tri-state meaning the rest of
    # this table uses: empty falls back to .env, not to zero.
    smtp_host = models.CharField(max_length=255, blank=True, default="")
    smtp_port = models.CharField(max_length=6, blank=True, default="")
    smtp_user = models.CharField(max_length=255, blank=True, default="")
    smtp_password = models.CharField(max_length=255, blank=True, default="")
    smtp_security = models.CharField(max_length=5, choices=SMTP_SECURITY, blank=True, default="")

    # Which channel a meeting reminder goes out on.
    reminder_channel = models.CharField(max_length=10, choices=REMINDER_CHANNELS, blank=True, default="")

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

    # Embeddings behind RAG-based prompt agents. Blank uses
    # 'text-embedding-3-small' — multilingual, and the cheapest of the OpenAI
    # embedding models. Under USE_AZURE the deployment name is taken from
    # azure_embedding_deployment (falling back to the model name).
    rag_embedding_model = models.CharField(max_length=120, blank=True, default="")
    azure_embedding_deployment = models.CharField(max_length=100, blank=True, default="")

    # --- Sensei (live) ---
    # Off by default, and deliberately so: most installations have no Sensei
    # service to talk to, and the flag is what keeps the whole feature — the
    # agent kind, its settings, and its create button — out of their way. See
    # agents.md -> "Sensei agents".
    sensei_enabled = models.CharField(max_length=1, choices=TRISTATE, blank=True, default="")
    sensei_api_url = models.CharField(max_length=500, blank=True, default="")
    sensei_function_key = models.CharField(max_length=500, blank=True, default="")
    # HMAC key behind the opaque per-patient id sent to Sensei. Sensei never
    # learns who a patient is; it only ever sees a stable digest. Rotating this
    # value orphans every Sensei-side account, so it is generated once and left
    # alone (see ConvAI.sensei.external_user_id).
    sensei_user_id_secret = models.CharField(max_length=200, blank=True, default="")

    # --- Message export (live) ---
    # Off by default, like Sensei: downloading every client's messages is
    # something a study needs, not something every installation should offer.
    # See message_export.md.
    message_export_enabled = models.CharField(max_length=1, choices=TRISTATE, blank=True, default="")

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
            # A recording names its client where the call wrote one down, and
            # falls back to matching numbers where it did not — one rule, kept
            # on the model so every surface asks the same question.
            return parent.resolve_patient()
        return getattr(parent, "patient", None)


class SummaryEdit(models.Model):
    """Who last replaced a generated overview with their own words.

    The overview is the one block in a panel the model writes rather than a
    person, which is the whole reason it is drawn in violet instead of the
    product blue. The moment someone edits it that stops being true, so the
    fact is recorded rather than guessed at: the panel drops the generated
    styling, names the author, and warns before regenerating over the top of
    what they wrote.

    The parent is an explicit nullable one-to-one per kind, following Note
    rather than a generic relation, so a deleted parent takes its edit record
    with it and permission checks can follow the parent object through code
    that already knows how to authorise it. One row per parent: this records
    the current state of the text, not a revision history.
    """

    author = models.ForeignKey(
        'ConvAIUser', null=True, blank=True, on_delete=models.SET_NULL,
        related_name="summary_edits",
    )
    edited_at = models.DateTimeField(auto_now=True)

    meeting = models.OneToOneField(
        Meeting, null=True, blank=True, on_delete=models.CASCADE, related_name="summary_edit")
    recording = models.OneToOneField(
        CallRecording, null=True, blank=True, on_delete=models.CASCADE, related_name="summary_edit")
    conversation = models.OneToOneField(
        Conversation, null=True, blank=True, on_delete=models.CASCADE, related_name="summary_edit")
    # Only alerts the classifier raised have a generated overview to correct.
    # One a person raised is already their own words, and the panel leaves it
    # read-only rather than offering to edit what they just typed.
    alert = models.OneToOneField(
        'Alert', null=True, blank=True, on_delete=models.CASCADE, related_name="summary_edit")

    class Meta:
        ordering = ["-edited_at"]

    def __str__(self):
        return f"SummaryEdit {self.pk} by {self.author or 'unknown'}"

    @property
    def parent(self):
        return self.meeting or self.recording or self.conversation or self.alert
