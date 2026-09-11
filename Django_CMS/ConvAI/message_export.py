"""Message export: every stored message as one CSV, for analysis elsewhere.

Off by default (``MESSAGE_EXPORT_ENABLED``). Most installations never need their
conversations to leave the platform; the ones that do are usually running a
study. The flag is what keeps a one-click download of every client's messages
out of the installations that have no use for it. See message_export.md.

*One row per message, not per Message row.* A ``Message`` holds a whole
exchange — what the client sent and what the agent answered — and analysis
wants those as separate observations with a speaker column. So each row is
split in two, and ``message_id`` stays on both halves so they can be paired
again.

*Nobody is named.* People appear as internal ids (``patient_id``,
``app_user_id``). ``Message.user`` holds a phone number or an email address;
it is used to work out who spoke and then dropped. The text itself is exported
as written — it can still contain whatever a client chose to type, and the
export does not try to scrub it.
"""
import csv
import datetime as dt
import uuid

from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Alert, Agent, Conversation, Meeting, Message, Patient
from .site_config import get_bool

# (column, what it holds). The order here is the order in the file, and the
# settings tab renders this list as the file's description, so the two cannot
# disagree.
COLUMN_NOTES = [
    ("message_id", _("Stored exchange this message belongs to. A client's message and the reply to it share one.")),
    ("conversation_id", _("Conversation the message belongs to. Messages the platform sent on its own have a key like reminder-12 instead.")),
    ("conversation_kind", _("chat, reminder, alert, care_plan, or other.")),
    ("turn", _("Position of the exchange within its conversation, from 1. Counted over the whole conversation, even when the date range cuts it.")),
    ("timestamp_utc", _("When the exchange was stored, in UTC (ISO 8601). Both halves of an exchange carry the same time.")),
    ("timestamp_local", _("The same moment in the platform timezone, with its offset.")),
    ("speaker", _("client, caregiver, app_user (someone signed in to the platform), unknown, agent, or platform.")),
    ("patient_id", _("Internal id of the client the message concerns. Empty when it cannot be worked out.")),
    ("app_user_id", _("Internal id of the platform account a chat came from (test users, API clients). Empty otherwise.")),
    ("agent_id", _("Internal id of the agent attached to the conversation.")),
    ("agent_name", _("Name of that agent.")),
    ("text", _("The message text. A leading ' is added to text starting with = + - or @, so spreadsheets do not run it as a formula.")),
    ("has_audio", _("1 if the message was a voice note or was answered with audio.")),
    ("liked", _("Staff feedback on the agent's reply: 1 or 0. Empty on the client's rows.")),
    ("disliked", _("As above.")),
    ("warning", _("As above.")),
    ("dangerous", _("As above.")),
]
COLUMNS = [name for name, _note in COLUMN_NOTES]

# Rows the platform writes to record what it sent on its own — a reminder, an
# alert message, a care plan — are keyed "<prefix>-<id>" rather than by a
# conversation UUID. See utils.send_whatsapp_reminder, mailer, views.alerts
# and views.media.
_LOGGED_KINDS = {"reminder": "reminder", "alert": "alert", "careplan": "care_plan"}

# A spreadsheet runs a cell starting with one of these as a formula, and the
# text here is typed by whoever messages the platform's number. An apostrophe
# in front is the usual defence (OWASP, "CSV injection"). It stays visible in
# the data, so analysis code can strip a leading "'" if it matters.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# Rows per chunk handed to the response. One chunk per row makes the server
# spend its time on tiny writes; this keeps each chunk to a few hundred KB.
_CHUNK_ROWS = 500


def enabled() -> bool:
    """Whether this installation offers message export at all. Off by default."""
    return get_bool("MESSAGE_EXPORT_ENABLED", False)


def _kind_of(conversation_id):
    """``(kind, ref)``: what a conversation key is, and the id inside it.

    For a chat, ``ref`` is the canonical UUID string, so a key stored in another
    spelling still finds its Conversation.
    """
    try:
        return "chat", str(uuid.UUID(conversation_id))
    except (TypeError, ValueError):
        pass
    prefix, _sep, ref = (conversation_id or "").partition("-")
    kind = _LOGGED_KINDS.get(prefix)
    if kind and ref.isdigit():
        return kind, int(ref)
    return "other", None


def _cell(text):
    if text and text.startswith(_FORMULA_START):
        return "'" + text
    return text


def _flag(value):
    return "1" if value else "0"


class _Lookup:
    """Everything the rows need from other tables, fetched once up front.

    Only for the conversations that are actually in the export, so a narrow
    date range stays a narrow set of queries.
    """

    def __init__(self, conversation_ids):
        self.kinds = {cid: _kind_of(cid) for cid in conversation_ids}

        def refs(kind):
            return {ref for k, ref in self.kinds.values() if k == kind}

        self.conversations = {
            str(c["id"]): c
            for c in Conversation.objects.filter(id__in=refs("chat"))
            .values("id", "patient_id", "agent_id", "user_id")
        }
        self.agents = dict(Agent.objects.values_list("id", "name"))
        # What the logged rows point at, reduced to the client they concern.
        self.patient_of = {
            "reminder": dict(Meeting.objects.filter(id__in=refs("reminder"))
                             .values_list("id", "patient_id")),
            "alert": dict(Alert.objects.filter(id__in=refs("alert"))
                          .values_list("id", "patient_id")),
            "care_plan": {pk: pk for pk in Patient.objects.filter(id__in=refs("care_plan"))
                          .values_list("id", flat=True)},
        }

        # Contact -> the clients it belongs to. A caregiver can look after more
        # than one client, so a number alone only settles who the client is
        # when it belongs to exactly one of them. Clients and caregivers are
        # kept apart because a self-registered client is their own caregiver:
        # the same number is on both records, and it is the client speaking.
        self.clients, self.caregivers = {}, {}
        for p in Patient.objects.values("id", "phone_number", "email",
                                        "caregiver__phone_number", "caregiver__email"):
            for contact in (p["phone_number"], p["email"]):
                if contact:
                    self.clients.setdefault(self._key(contact), set()).add(p["id"])
            for contact in (p["caregiver__phone_number"], p["caregiver__email"]):
                if contact:
                    self.caregivers.setdefault(self._key(contact), set()).add(p["id"])

    @staticmethod
    def _key(contact):
        return str(contact).strip().lower()

    def speaker(self, sender, conversation):
        key = self._key(sender)
        if key in self.clients:
            return "client"
        if key in self.caregivers:
            return "caregiver"
        if conversation and conversation["user_id"]:
            return "app_user"
        return "unknown"

    def patient(self, kind, ref, conversation, sender):
        if conversation and conversation["patient_id"]:
            return conversation["patient_id"]
        if kind in self.patient_of and self.patient_of[kind].get(ref):
            return self.patient_of[kind][ref]
        key = self._key(sender)
        for owners in (self.clients.get(key), self.caregivers.get(key)):
            if owners and len(owners) == 1:
                return next(iter(owners))
        return None


def rows(start=None, end=None):
    """Yield one list per message, in COLUMNS order.

    ``start`` and ``end`` are aware datetimes bounding ``Message.timestamp``
    (start inclusive, end exclusive); either can be None. Rows come grouped by
    conversation and in order within it.
    """
    qs = Message.objects.all()
    if start is not None:
        qs = qs.filter(timestamp__gte=start)
    if end is not None:
        qs = qs.filter(timestamp__lt=end)

    look = _Lookup(set(qs.order_by().values_list("conversation_id", flat=True).distinct()))

    # A range that starts partway through a conversation should still number
    # its exchanges from the conversation's real beginning, or turn 1 in the
    # file would be turn 40 in fact.
    before = {}
    if start is not None:
        before = dict(
            Message.objects.filter(timestamp__lt=start)
            .order_by().values("conversation_id")
            .annotate(n=Count("id")).values_list("conversation_id", "n")
        )

    current, turn = None, 0
    for m in qs.order_by("conversation_id", "timestamp", "id").iterator(chunk_size=2000):
        if m.conversation_id != current:
            current, turn = m.conversation_id, before.get(m.conversation_id, 0)
        turn += 1

        kind, ref = look.kinds.get(m.conversation_id) or _kind_of(m.conversation_id)
        conv = look.conversations.get(ref) if kind == "chat" else None
        patient_id = look.patient(kind, ref, conv, m.user)
        agent_id = conv["agent_id"] if conv else None
        shared = {
            "message_id": m.id,
            "conversation_id": m.conversation_id,
            "conversation_kind": kind,
            "turn": turn,
            "timestamp_utc": m.timestamp.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
            "timestamp_local": timezone.localtime(m.timestamp).isoformat(timespec="seconds"),
            "patient_id": patient_id or "",
            "app_user_id": (conv["user_id"] if conv else None) or "",
            "agent_id": agent_id or "",
            "agent_name": look.agents.get(agent_id, "") if agent_id else "",
        }

        # An empty side is not a message. The rows the platform logs for what
        # it sent have no client half at all.
        if m.user_message:
            yield _ordered({
                **shared,
                "speaker": look.speaker(m.user, conv),
                "text": _cell(m.user_message),
                "has_audio": _flag(m.input_audio_file),
                "liked": "", "disliked": "", "warning": "", "dangerous": "",
            })
        if m.response_message:
            yield _ordered({
                **shared,
                "speaker": "agent" if kind in ("chat", "other") else "platform",
                "text": _cell(m.response_message),
                "has_audio": _flag(m.response_audio_file),
                "liked": _flag(m.liked), "disliked": _flag(m.disliked),
                "warning": _flag(m.warning), "dangerous": _flag(m.dangerous),
            })


def _ordered(row):
    return [row[c] for c in COLUMNS]


class _Echo:
    """A file-like object csv.writer can write to that just hands the line back."""

    def write(self, value):
        return value


def csv_chunks(start=None, end=None):
    """The CSV file as a stream of text chunks, header first.

    UTF-8 with a byte-order mark: without it Excel reads the file as the local
    code page and every accent — and every Korean or Chinese message — comes
    out as mojibake. pandas and R's readr both skip the mark.
    """
    writer = csv.writer(_Echo())
    yield "\ufeff" + writer.writerow(COLUMNS)
    batch = []
    for row in rows(start, end):
        batch.append(writer.writerow(row))
        if len(batch) >= _CHUNK_ROWS:
            yield "".join(batch)
            batch = []
    if batch:
        yield "".join(batch)
