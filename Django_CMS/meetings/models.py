"""What an online meeting needs to remember.

Everything here hangs off ``ConvAI.Meeting`` (modality ONLINE). The audio a
meeting produces ends up as an ordinary ``ConvAI.CallRecording`` — owned by the
core, so recordings survive this app being switched off or removed.
"""
from __future__ import annotations

import secrets
import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

TRISTATE = [("", _("Use the .env default")), ("1", _("On")), ("0", _("Off"))]

_CONFIG_CACHE_KEY = "meetings_settings_singleton"


def _public_id() -> str:
    # 16 characters of base32-ish randomness: the half of the link that names
    # the invite. The other half is an HMAC (see links.py), so this does not
    # need to be secret — it only needs to be unguessable enough not to leak
    # how many invites exist.
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _nonce() -> str:
    return secrets.token_urlsafe(18)


class MeetingsSettings(models.Model):
    """Runtime settings for online meetings (Settings → Online meetings).

    A singleton, owned by this app rather than added to the core's
    SiteConfiguration, so removing the app removes its settings with it.
    Connection details for LiveKit are deliberately *not* here: the LiveKit
    server, the web app and the agent workers must share one key pair, so it
    lives in the environment they all read (see online_meetings.md).
    """

    enabled = models.CharField(
        max_length=1, choices=TRISTATE, blank=True, default="",
        help_text=_("Off by default. While off, online meetings cannot be booked "
                    "or joined; existing ones stay readable."),
    )
    record_by_default = models.BooleanField(
        default=True,
        help_text=_("Record each participant's audio (never video) unless switched "
                    "off for a meeting. Recordings are transcribed in the background."),
    )
    auto_admit = models.BooleanField(
        default=False,
        help_text=_("Let invited clients straight in once you have joined, instead "
                    "of waiting for you to admit them."),
    )
    link_early_minutes = models.PositiveSmallIntegerField(
        default=30,
        help_text=_("How long before the start time a client's link starts working."),
    )
    link_late_hours = models.PositiveSmallIntegerField(
        default=3,
        help_text=_("How long after the start time a client's link keeps working."),
    )
    max_live_rooms = models.PositiveSmallIntegerField(
        default=4,
        help_text=_("Meetings that can run at the same time. Protects a small "
                    "server; each room with an assistant uses roughly 300 MB."),
    )
    max_minutes = models.PositiveSmallIntegerField(
        default=90,
        help_text=_("A room still open after this long is closed automatically."),
    )
    assistant_agent = models.ForeignKey(
        "ConvAI.Agent", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
        limit_choices_to={"kind": "prompt"},
        help_text=_("The prompt-based agent behind “Ask the assistant” in a meeting. "
                    "Its prompt and, if it has one, its knowledge base are used."),
    )
    interviewer_voice = models.CharField(
        max_length=40, blank=True, default="",
        help_text=_("Voice for the meeting agents. Blank uses the Azure Realtime voice."),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Online meetings settings"
        verbose_name_plural = "Online meetings settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(_CONFIG_CACHE_KEY)

    @classmethod
    def load(cls) -> "MeetingsSettings":
        obj = cache.get(_CONFIG_CACHE_KEY)
        if obj is None:
            obj, _created = cls.objects.get_or_create(pk=1)
            cache.set(_CONFIG_CACHE_KEY, obj, 30)
        return obj

    def __str__(self):
        return "Online meetings settings"


class MeetingInvite(models.Model):
    """One person's way into one online meeting: the link they are sent.

    The link is ``/m/<public_id>/<token>`` where the token is an HMAC of the
    public id and ``nonce`` (links.py). Nothing secret is stored, so the same
    link can be sent again; changing ``nonce`` invalidates every copy of it.
    """

    class Invitee(models.TextChoices):
        CAREGIVER = "caregiver", _("Caregiver")
        CLIENT = "client", _("Client")
        OTHER = "other", _("Other")

    class Principal(models.TextChoices):
        # The seam for client accounts later: a link is what proves who you are
        # today. A signed-in client would be a second principal kind.
        LINK = "link", _("Link")

    meeting = models.ForeignKey("ConvAI.Meeting", on_delete=models.CASCADE,
                                related_name="online_invites")
    public_id = models.CharField(max_length=24, unique=True, default=_public_id)
    nonce = models.CharField(max_length=48, default=_nonce)
    invitee = models.CharField(max_length=12, choices=Invitee.choices,
                               default=Invitee.CAREGIVER)
    patient = models.ForeignKey("ConvAI.Patient", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="+")
    caregiver = models.ForeignKey("ConvAI.Caregiver", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    # First name only: what the lobby greets them with and the room labels
    # their tile with. Never the surname — a forwarded link shows as little
    # about the person as it can while still being recognisable to them.
    display_name = models.CharField(max_length=80, blank=True, default="")
    language = models.CharField(max_length=10, blank=True, default="")
    record_allowed = models.BooleanField(default=True)
    principal = models.CharField(max_length=12, choices=Principal.choices,
                                 default=Principal.LINK)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=120, blank=True, default="")
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_opened_at = models.DateTimeField(null=True, blank=True)
    open_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invite {self.public_id} ({self.invitee}) for meeting {self.meeting_id}"

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def identity(self) -> str:
        """The LiveKit identity this invite joins as. Stable, so a second
        device on the same link replaces the first rather than doubling up."""
        return f"inv-{self.public_id}"

    def rotate(self):
        self.nonce = _nonce()
        self.revoked_at = None
        self.revoked_reason = ""
        self.save(update_fields=["nonce", "revoked_at", "revoked_reason"])

    def revoke(self, reason: str):
        if self.revoked_at:
            return
        self.revoked_at = timezone.now()
        self.revoked_reason = reason[:120]
        self.save(update_fields=["revoked_at", "revoked_reason"])


class InviteDelivery(models.Model):
    """One sending of a link, by whom, how, and whether it went."""

    class Channel(models.TextChoices):
        EMAIL = "email", _("Email")
        SMS = "sms", _("SMS")
        WHATSAPP = "whatsapp", _("WhatsApp")

    invite = models.ForeignKey(MeetingInvite, on_delete=models.CASCADE,
                               related_name="deliveries")
    channel = models.CharField(max_length=12, choices=Channel.choices)
    to_masked = models.CharField(max_length=80, blank=True, default="")
    ok = models.BooleanField(default=False)
    error = models.CharField(max_length=300, blank=True, default="")
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="+")
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]


class MeetingSession(models.Model):
    """One opening of a meeting's room, from Start to End.

    Usually one per meeting; a second if the navigator ends and starts again.
    The LiveKit room is named after ``uuid`` — never after the client — because
    room names end up in LiveKit's logs.
    """

    class Status(models.TextChoices):
        LIVE = "live", _("Live")
        ENDED = "ended", _("Ended")

    class Recording(models.TextChoices):
        OFF = "off", _("Not recording")
        STARTING = "starting", _("Starting")
        ON = "on", _("Recording")
        LOST = "lost", _("Recorder lost")
        STOPPED = "stopped", _("Stopped")

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    meeting = models.ForeignKey("ConvAI.Meeting", on_delete=models.CASCADE,
                                related_name="online_sessions")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.LIVE)
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=60, blank=True, default="")
    room_sid = models.CharField(max_length=64, blank=True, default="")
    auto_admit = models.BooleanField(default=False)

    recording_enabled = models.BooleanField(default=True)
    recording_state = models.CharField(max_length=10, choices=Recording.choices,
                                       default=Recording.OFF)
    recorder_seen_at = models.DateTimeField(null=True, blank=True)
    recorder_dispatches = models.PositiveSmallIntegerField(default=0)
    recording = models.ForeignKey("ConvAI.CallRecording", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    last_human_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            # One live room per meeting: two navigators pressing Start at once
            # must end up in the same room, not two.
            models.UniqueConstraint(fields=["meeting"], condition=models.Q(status="live"),
                                    name="one_live_session_per_meeting"),
        ]

    def __str__(self):
        return f"Session {self.uuid} ({self.status}) for meeting {self.meeting_id}"

    @property
    def room_name(self) -> str:
        return f"cc-{self.uuid.hex}"

    @property
    def is_live(self) -> bool:
        return self.status == self.Status.LIVE


class LobbyEntry(models.Model):
    """Someone with a link, waiting to be let in (or let in, or turned away)."""

    class State(models.TextChoices):
        WAITING = "waiting", _("Waiting")
        ADMITTED = "admitted", _("Admitted")
        DENIED = "denied", _("Turned away")
        LEFT = "left", _("Left")

    meeting = models.ForeignKey("ConvAI.Meeting", on_delete=models.CASCADE,
                                related_name="+")
    invite = models.ForeignKey(MeetingInvite, on_delete=models.CASCADE,
                               related_name="lobby_entries")
    session = models.ForeignKey(MeetingSession, null=True, blank=True,
                                on_delete=models.CASCADE, related_name="lobby")
    state = models.CharField(max_length=10, choices=State.choices, default=State.WAITING)
    created_at = models.DateTimeField(auto_now_add=True)
    seen_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["meeting", "invite"],
                                    name="one_lobby_entry_per_invite"),
        ]


class Attendance(models.Model):
    """Who was in the room, from when to when — the meeting's register."""

    class Kind(models.TextChoices):
        STAFF = "staff", _("Staff")
        INVITEE = "invitee", _("Invitee")
        AGENT = "agent", _("Agent")

    session = models.ForeignKey(MeetingSession, on_delete=models.CASCADE,
                                related_name="attendance")
    identity = models.CharField(max_length=96)
    participant_sid = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=120, blank=True, default="")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    role = models.CharField(max_length=20, blank=True, default="")
    invite = models.ForeignKey(MeetingInvite, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="+")
    joined_at = models.DateTimeField(default=timezone.now)
    left_at = models.DateTimeField(null=True, blank=True)
    left_reason = models.CharField(max_length=40, blank=True, default="")

    class Meta:
        ordering = ["joined_at"]


class AgentRun(models.Model):
    """One agent dispatched into a room: the recorder, the interviewer, or the
    assistant. The agent worker reports back against this row (internal API),
    and the room UI reads its state from it."""

    class Role(models.TextChoices):
        SCRIBE = "scribe", _("Recorder")
        INTERVIEWER = "interviewer", _("Interviewer")
        ASSISTANT = "assistant", _("Assistant")

    class State(models.TextChoices):
        DISPATCHED = "dispatched", _("Connecting")
        RUNNING = "running", _("Running")
        PAUSED = "paused", _("Paused")
        FINISHED = "finished", _("Finished")
        STOPPED = "stopped", _("Stopped")
        FAILED = "failed", _("Failed")

    LIVE_STATES = (State.DISPATCHED, State.RUNNING, State.PAUSED)

    session = models.ForeignKey(MeetingSession, on_delete=models.CASCADE,
                                related_name="agent_runs")
    role = models.CharField(max_length=12, choices=Role.choices)
    state = models.CharField(max_length=12, choices=State.choices,
                             default=State.DISPATCHED)
    protocol = models.ForeignKey("ConvAI.Protocol", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    respondent_identity = models.CharField(max_length=96, blank=True, default="")
    respondent_name = models.CharField(max_length=80, blank=True, default="")
    controller_identity = models.CharField(max_length=96, blank=True, default="")
    language = models.CharField(max_length=10, blank=True, default="")
    agent_identity = models.CharField(max_length=96, blank=True, default="")
    dispatch_id = models.CharField(max_length=64, blank=True, default="")
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    last_status_at = models.DateTimeField(null=True, blank=True)
    answers_saved = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.role} run {self.pk} ({self.state})"

    @property
    def is_live(self) -> bool:
        return self.state in self.LIVE_STATES


class RecordingSegment(models.Model):
    """One stretch of one person's audio, as written by the recorder.

    A participant who reconnects, or a recording that rotates its file every
    few minutes, produces several. The mixdown and the transcript are built
    from these rows alone, so whatever produced them — the in-room recorder
    today, LiveKit Egress at scale — the rest does not change.
    """

    session = models.ForeignKey(MeetingSession, on_delete=models.CASCADE,
                                related_name="segments")
    identity = models.CharField(max_length=96)
    label = models.CharField(max_length=120, blank=True, default="")
    role = models.CharField(max_length=20, blank=True, default="")
    track_sid = models.CharField(max_length=64)
    seq = models.PositiveIntegerField(default=0)
    path = models.CharField(max_length=500)
    codec = models.CharField(max_length=20, blank=True, default="ogg/opus")
    start_utc_ms = models.BigIntegerField()
    end_utc_ms = models.BigIntegerField(null=True, blank=True)
    bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_utc_ms", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["session", "identity", "track_sid", "seq"],
                                    name="one_row_per_segment"),
        ]


class RecordingConsent(models.Model):
    """What someone with a link was told about recording, and when they agreed.

    Append-only, like ConvAI.ConsentRecord: a record of a notice shown is only
    worth anything if it cannot be edited after the fact.
    """

    invite = models.ForeignKey(MeetingInvite, on_delete=models.CASCADE,
                               related_name="consents")
    session = models.ForeignKey(MeetingSession, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="+")
    accepted = models.BooleanField(default=True)
    notice_version = models.CharField(max_length=20, default="1")
    notice_text = models.TextField(blank=True, default="")
    user_agent = models.CharField(max_length=300, blank=True, default="")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("RecordingConsent is append-only.")
        super().save(*args, **kwargs)


class WebhookEvent(models.Model):
    """LiveKit webhook ids already handled, so a redelivery is a no-op."""

    event_id = models.CharField(max_length=80, unique=True)
    event = models.CharField(max_length=40)
    received_at = models.DateTimeField(auto_now_add=True)
