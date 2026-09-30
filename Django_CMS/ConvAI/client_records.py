"""What a member of staff may ask about their clients, answered in one place.

The Link Worker v2 tools and the client-record REST endpoints are both thin
layers over this module. That is the point of it: the permission rule, the
privacy rule, the shape of every answer and the access log live here once, so
the assistant and the API cannot drift into disagreeing about who may read what.

Three rules, the same ones the screens apply:

* **Who.** An admin sees every client; anybody else sees the clients they are
  the navigator of (``visible_patients``). A client outside that is answered
  exactly as a client that does not exist — ``NotVisible`` either way — so the
  answer never confirms somebody is on the platform.
* **What.** Summaries, never transcripts. A conversation the client hid from
  their link worker gives its machine summary and nothing else, as on the
  client's page (see conversation_privacy.md); message bodies are never read.
* **Logged.** Every function that returns a client's record writes a
  ``RecordAccess`` row per client it revealed, before returning.

Every function returns plain JSON-able data. The tools hand it to the model as
JSON and the endpoints return it as-is, so both describe a record identically.
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .roles import is_admin

# Bounds on what one answer can carry. An assistant's context and an API
# response are both better with less; each list says when it was cut.
MAX_CLIENTS = 25
MAX_MEETINGS = 20
MAX_HITS = 30
MAX_RECENT = 5
SNIPPET = 90
DETAILS_CHARS = 1500


class NotVisible(Exception):
    """No such client, or not one this person may see. Deliberately the same."""


class BadQuestion(ValueError):
    """A question that cannot be answered as asked, with wording safe to show.

    The only exception these functions raise on purpose besides NotVisible. The
    tools and the endpoints put its message back to whoever asked; anything else
    is a fault, and is treated as one.
    """


class Ambiguous(BadQuestion):
    """A protocol reference that matches no protocol, or more than one."""


# ----------------------------------------------------------------------------
# Who may see whom
# ----------------------------------------------------------------------------

def visible_patients(user):
    """The clients ``user`` may read: everyone for an admin, their own otherwise."""
    from .models import Patient

    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return Patient.objects.none()
    qs = Patient.objects.select_related("navigator", "caregiver")
    if is_admin(user):
        return qs
    return qs.filter(navigator=user)


def _patient(user, patient_id):
    try:
        pk = int(patient_id)
    except (TypeError, ValueError):
        raise NotVisible()
    patient = visible_patients(user).filter(pk=pk).first()
    if patient is None:
        raise NotVisible()
    return patient


# ----------------------------------------------------------------------------
# The access log
# ----------------------------------------------------------------------------

def _log(user, patients, action, via, detail=""):
    """One RecordAccess row per client revealed; one with no client if none were."""
    from .models import RecordAccess

    label = (user.get_username() if user else "")[:150]
    rows = [
        RecordAccess(user=user, user_label=label, patient=p,
                     patient_label=_name(p)[:200], action=action, via=via,
                     detail=(detail or "")[:200])
        for p in patients
    ] or [RecordAccess(user=user, user_label=label, action=action, via=via,
                       detail=(detail or "")[:200])]
    RecordAccess.objects.bulk_create(rows)


# ----------------------------------------------------------------------------
# Small shapes
# ----------------------------------------------------------------------------

def _name(p) -> str:
    return f"{p.name} {p.lastname}".strip()


def _when(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M") if dt else None


def _person(user):
    if user is None:
        return None
    return (user.get_full_name() or user.get_username()).strip()


def _phone(value):
    return str(value) if value else None


def _client_ref(p) -> dict:
    return {"id": p.pk, "name": _name(p)}


def _clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _plain(md: str) -> str:
    """A question's Markdown as one readable line."""
    text = re.sub(r"[*_`#>]+", "", md or "")
    return " ".join(text.split())


def _meeting(m, *, protocols=True) -> dict:
    out = {
        "id": m.pk,
        "scheduled": _when(m.scheduled_time),
        "type": m.get_type_display(),
        "status": m.get_status_display(),
        "modality": m.get_modality_display(),
    }
    if m.location:
        out["location"] = m.location
    if protocols:
        out["protocols"] = [f"{p.number}. {p.title}" for p in m.scheduled_protocols.all()]
    return out


def _word_at(text: str, needle: str) -> int:
    """Where ``needle`` first starts a word in ``text``, or -1."""
    m = re.search(r"(^|\W)(" + re.escape(needle) + ")", text or "", re.IGNORECASE)
    return m.start(2) if m else -1


def _starts_word(text, needle) -> bool:
    return _word_at(text or "", needle) >= 0


def _snippet(text: str, needle: str) -> str:
    text = " ".join((text or "").split())
    i = _word_at(text, needle)
    if i < 0:
        return _clip(text, SNIPPET * 2)
    start, end = max(0, i - SNIPPET), min(len(text), i + len(needle) + SNIPPET)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


# ----------------------------------------------------------------------------
# Protocols
# ----------------------------------------------------------------------------

def resolve_protocol(ref):
    """A protocol from its number, or from words in its title.

    "3", 3 and "iqcode" all find protocol 3. Words that match several titles are
    refused with the candidates named, rather than answering about whichever
    happened to sort first.
    """
    from .models import Protocol

    text = str(ref if ref is not None else "").strip()
    if not text:
        raise Ambiguous("Say which protocol: its number or words from its title.")
    if text.isdigit():
        p = Protocol.objects.filter(number=int(text)).first()
        if p is None:
            raise Ambiguous(f"There is no protocol {text}.")
        return p
    matches = list(Protocol.objects.filter(title__icontains=text))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = ", ".join(f"{p.number}. {p.title}" for p in Protocol.objects.all())
        raise Ambiguous(f'No protocol title contains "{text}". Protocols: {known or "none"}.')
    raise Ambiguous("That matches several protocols: "
                    + ", ".join(f"{p.number}. {p.title}" for p in matches) + ".")


def _alerts_withheld(alerts, user):
    """Ids of alerts raised from a conversation this reader may not see into."""
    from . import conversation_privacy
    from .models import Alert

    by_conv = {}
    for a in alerts:
        cid = (a.data or {}).get("conversation_id") if isinstance(a.data, dict) else None
        if a.alert_type == Alert.AlertType.CONVERSATION and cid:
            by_conv.setdefault(str(cid), []).append(a.pk)
    hidden = conversation_privacy.withheld_ids(list(by_conv), user)
    return {pk for cid in hidden for pk in by_conv.get(cid, [])}


def _answers(patient, protocol=None):
    from .models import Answer

    qs = (Answer.objects.filter(meeting__patient=patient)
          .select_related("meeting", "question", "question__protocol")
          .order_by("meeting__scheduled_time", "pk"))
    if protocol is not None:
        qs = qs.filter(question__protocol=protocol)
    return list(qs)


# ----------------------------------------------------------------------------
# The questions staff ask
# ----------------------------------------------------------------------------

def find_clients(user, query: str = "", *, via: str) -> dict:
    """Clients by name, surname, full name or phone; everyone visible if blank."""
    from django.db.models import TextField, Value
    from django.db.models.functions import Concat

    from .models import RecordAccess

    qs = visible_patients(user).order_by("lastname", "name")
    q = " ".join((query or "").replace(",", " ").split())
    if q:
        tokens = q.split(" ")
        cond = Q(name__icontains=q) | Q(lastname__icontains=q) | Q(phone_number__icontains=q)
        if len(tokens) >= 2:
            first, last = tokens[0], tokens[-1]
            qs = qs.annotate(full=Concat("name", Value(" "), "lastname",
                                         output_field=TextField()))
            cond |= (Q(full__icontains=q)
                     | (Q(name__icontains=first) & Q(lastname__icontains=last))
                     | (Q(name__icontains=last) & Q(lastname__icontains=first)))
        qs = qs.filter(cond)

    total = qs.count()
    clients = [{
        **_client_ref(p),
        "phone": _phone(p.phone_number),
        "navigator": _person(p.navigator),
    } for p in qs[:MAX_CLIENTS]]
    # A name list is not a record: one row for the question, not one per name.
    _log(user, [], RecordAccess.Action.FIND, via, q or "(all)")
    return {"query": q, "total": total, "clients": clients,
            "truncated": total > len(clients)}


def overview(user, patient_id, *, via: str) -> dict:
    """Everything the platform holds about one client, in summary form."""
    from . import conversation_privacy, conversation_summary
    from .models import Alert, Conversation, Meeting, Note, RecordAccess

    p = _patient(user, patient_id)
    now = timezone.now()

    upcoming = (Meeting.objects.filter(patient=p, scheduled_time__gt=now,
                                       status=Meeting.Status.PENDING)
                .prefetch_related("scheduled_protocols").order_by("scheduled_time")[:3])
    past = (Meeting.objects.filter(patient=p)
            .filter(Q(scheduled_time__lte=now) | ~Q(status=Meeting.Status.PENDING))
            .prefetch_related("scheduled_protocols", "executed_protocols")
            .order_by("-scheduled_time")[:3])
    recent = []
    for m in past:
        item = _meeting(m, protocols=False)
        item["covered"] = [f"{x.number}. {x.title}" for x in m.executed_protocols.all()]
        if m.protocol_summary:
            item["summary"] = _clip(m.protocol_summary, 600)
        if m.cancel_reason:
            item["cancel_reason"] = m.cancel_reason
        recent.append(item)

    # Which protocols have answers, and how far through each one they are.
    progress = {}
    for a in _answers(p):
        proto = a.question.protocol
        entry = progress.setdefault(proto.pk, {
            "protocol": f"{proto.number}. {proto.title}",
            "questions": proto.questions.count(),
            "answered": set(), "last_answered": None,
        })
        entry["answered"].add(a.question_id)
        entry["last_answered"] = _when(a.meeting.happened_at)
    protocols = [{**e, "answered": len(e["answered"])} for e in progress.values()]
    for proto in p.protocols.all():
        if proto.pk not in progress:
            protocols.append({"protocol": f"{proto.number}. {proto.title}",
                              "questions": proto.questions.count(),
                              "answered": 0, "last_answered": None})

    alerts = [{
        "id": a.pk, "title": a.title, "priority": a.get_priority_display(),
        "status": a.get_status_display(), "raised": _when(a.created_at),
    } for a in (Alert.objects.filter(patient=p)
                .exclude(status=Alert.AlertStatus.RESOLVED).order_by("priority", "-created_at")[:MAX_RECENT])]

    convs = list(Conversation.objects.filter(patient=p).order_by("-last_message_at")[:MAX_RECENT])
    # withheld_ids answers in the spelling it was asked in, so ask in strings.
    withheld = conversation_privacy.withheld_ids([str(c.pk) for c in convs], user)
    conversations = []
    for c in convs:
        text, source, _at = conversation_summary.machine_summary(c)
        row = {"started": _when(c.started_at), "last_message": _when(c.last_message_at),
               "summary": _clip(text, 500) or None}
        if str(c.pk) in withheld:
            # As on the client's page: the fact of it and its summary, no more.
            row["hidden_by_client"] = True
        else:
            if c.topic:
                row["topic"] = c.topic
            human, author, _ = conversation_summary.human_summary(c)
            if human:
                row["link_worker_summary"] = _clip(human, 500)
        conversations.append(row)

    notes = [{
        "written": _when(n.created_at), "by": _person(n.author), "text": _clip(n.body, 400),
    } for n in (Note.objects.filter(Q(meeting__patient=p) | Q(alert__patient=p)
                                    | Q(conversation__patient=p))
                .select_related("author").order_by("-created_at")[:MAX_RECENT])]

    caregiver = None
    if p.caregiver_id:
        cg = p.caregiver
        caregiver = {"name": f"{cg.name} {cg.lastname}".strip(),
                     "relationship": cg.relationship or None,
                     "phone": _phone(cg.phone_number)}

    details = "\n\n".join(x for x in ((p.details or "").strip(),
                                      (p.details_editable or "").strip()) if x)
    out = {
        "client": {
            **_client_ref(p),
            "phone": _phone(p.phone_number),
            "email": p.email or None,
            "navigator": _person(p.navigator),
            "agent": p.agent.name if p.agent_id else None,
            "assistant_on": p.chatbot_enabled,
        },
        "caregiver": caregiver,
        "details": _clip(details, DETAILS_CHARS) or None,
        "care_plan_on_file": bool(p.care_plan),
        "next_meetings": [_meeting(m) for m in upcoming],
        "recent_meetings": recent,
        "protocols": protocols,
        "open_alerts": alerts,
        "recent_conversations": conversations,
        "recent_notes": notes,
    }
    enrolment = _enrolment(p)
    if enrolment:
        out["study"] = enrolment

    _log(user, [p], RecordAccess.Action.OVERVIEW, via)
    return out


def _enrolment(p):
    """The client's study, where study enrolment is on and they came through one."""
    from django.core.exceptions import ObjectDoesNotExist

    from . import enrolment as enrolment_service

    if not enrolment_service.enabled():
        return None
    try:
        e = p.enrolment
    except ObjectDoesNotExist:
        return None
    latest = e.latest_consent
    return {"study": e.study.display_name, "status": e.get_status_display(),
            "consent_version": latest.consent_version if latest else None,
            "consent_current": e.consent_is_current}


def upcoming_meetings(user, *, patient_id=None, days: int = 30, via: str) -> dict:
    """Pending meetings in the next ``days`` days, for one client or the caseload."""
    from .models import Meeting, RecordAccess

    days = max(1, min(int(days or 30), 365))
    now = timezone.now()
    qs = (Meeting.objects.filter(patient__in=visible_patients(user),
                                 status=Meeting.Status.PENDING,
                                 scheduled_time__gt=now,
                                 scheduled_time__lte=now + timedelta(days=days))
          .select_related("patient").prefetch_related("scheduled_protocols")
          .order_by("scheduled_time"))
    scope = "caseload"
    if patient_id not in (None, ""):
        p = _patient(user, patient_id)
        qs = qs.filter(patient=p)
        scope = _name(p)

    total = qs.count()
    rows = list(qs[:MAX_MEETINGS])
    meetings = [{**_meeting(m), "client": _client_ref(m.patient)} for m in rows]

    revealed = {m.patient_id: m.patient for m in rows}
    if scope != "caseload":
        revealed = {p.pk: p}
    _log(user, list(revealed.values()), RecordAccess.Action.MEETINGS, via,
         f"next {days} days")
    return {"scope": scope, "days": days, "total": total, "meetings": meetings,
            "truncated": total > len(meetings)}


def protocol_answers(user, patient_id, protocol=None, *, via: str) -> dict:
    """What the client has answered: the latest answer to each question.

    The rule the call panel uses: the most recent answer wins, because a later
    answer corrects an earlier one. Which call it came from travels with it.
    """
    from .models import RecordAccess

    p = _patient(user, patient_id)
    proto = resolve_protocol(protocol) if protocol not in (None, "") else None

    latest = {}
    for a in _answers(p, proto):
        latest[a.question_id] = a  # ordered oldest first, so the last one wins

    by_protocol = {}
    for a in latest.values():
        q = a.question
        entry = by_protocol.setdefault(q.protocol.pk, {
            "protocol": f"{q.protocol.number}. {q.protocol.title}",
            "repeatable": q.protocol.repeatable,
            "questions": q.protocol.questions.count(),
            "answers": [],
        })
        entry["answers"].append({
            "question": f"Q{q.order}. {_clip(_plain(q.prompt_md), 220)}",
            "answer": a.response,
            "answered": _when(a.meeting.happened_at),
            "by_text": a.by_text,
            "_order": q.order,
        })
    protocols = []
    for entry in sorted(by_protocol.values(), key=lambda e: e["protocol"]):
        entry["answers"].sort(key=lambda x: x.pop("_order"))
        protocols.append(entry)

    if proto is not None and not protocols:
        protocols = [{"protocol": f"{proto.number}. {proto.title}",
                      "repeatable": proto.repeatable,
                      "questions": proto.questions.count(), "answers": []}]

    _log(user, [p], RecordAccess.Action.ANSWERS, via,
         f"protocol {proto.number}" if proto else "all protocols")
    return {"client": _client_ref(p), "protocols": protocols}


def protocol_history(user, patient_id, protocol, *, via: str) -> dict:
    """How one protocol's answers changed, call by call, oldest first.

    Each round is one meeting's answers, beside that meeting's own summary of
    the protocol where one was written. Reading the rounds in order is how
    "is her sleep getting better?" gets answered from what was actually said.
    """
    from .models import RecordAccess

    p = _patient(user, patient_id)
    proto = resolve_protocol(protocol)

    rounds = {}
    for a in _answers(p, proto):
        m = a.meeting
        r = rounds.setdefault(m.pk, {
            "meeting": m.pk, "date": _when(m.happened_at),
            "type": m.get_type_display(), "status": m.get_status_display(),
            "answers": [],
        })
        if m.protocol_summary and "meeting_summary" not in r:
            r["meeting_summary"] = _clip(m.protocol_summary, 600)
        r["answers"].append({"question": f"Q{a.question.order}. "
                                         f"{_clip(_plain(a.question.prompt_md), 160)}",
                             "answer": a.response, "_order": a.question.order})
    ordered = sorted(rounds.values(), key=lambda r: r["date"] or "")
    for r in ordered:
        r["answers"].sort(key=lambda x: x.pop("_order"))

    _log(user, [p], RecordAccess.Action.HISTORY, via, f"protocol {proto.number}")
    return {
        "client": _client_ref(p),
        "protocol": f"{proto.number}. {proto.title}",
        "repeatable": proto.repeatable,
        "rounds": ordered,
        "note": (None if len(ordered) > 1 else
                 "Only one call has answers for this protocol, so there is no change to "
                 "describe yet." if ordered else "No answers recorded for this protocol."),
    }


def search_records(user, text: str, *, via: str) -> dict:
    """Where a word or phrase appears across the records this person may see.

    Searches protocol answers, client details, notes, meeting summaries,
    conversation summaries and alerts — the written record, never message
    bodies. "Which of my clients mentioned diabetes?" is this.
    """
    from . import conversation_summary
    from .models import Alert, Answer, Conversation, Meeting, Note, RecordAccess

    needle = " ".join((text or "").split())
    if len(needle) < 3:
        raise BadQuestion("Search for at least three characters.")
    clients = visible_patients(user)
    hits = []

    # At the start of a word, so "fall" finds "falls" and "falling" but not
    # "windfall", and "ill" does not find every "will". \W and re.escape mean
    # the same in Postgres's regex dialect and in Python's (which SQLite uses).
    word = r"(^|\W)" + re.escape(needle)

    def has(field):
        return Q(**{f"{field}__iregex": word})

    def hit(patient, source, when, body, extra=None):
        hits.append({"client": _client_ref(patient), "source": source, "date": _when(when),
                     "snippet": _snippet(body, needle), **(extra or {})})

    for a in (Answer.objects.filter(has("response"), meeting__patient__in=clients)
              .select_related("meeting__patient", "question__protocol")
              .order_by("-meeting__scheduled_time")[:MAX_HITS]):
        hit(a.meeting.patient, "protocol answer", a.meeting.happened_at, a.response,
            {"protocol": f"{a.question.protocol.number}. {a.question.protocol.title}",
             "question": f"Q{a.question.order}"})
    for p in clients.filter(has("details") | has("details_editable"))[:MAX_HITS]:
        body = p.details if _starts_word(p.details, needle) else p.details_editable
        hit(p, "client details", None, body)
    for n in (Note.objects.filter(Q(meeting__patient__in=clients) | Q(alert__patient__in=clients)
                                  | Q(conversation__patient__in=clients), has("body"))
              .select_related("meeting__patient", "alert__patient", "conversation__patient")
              .order_by("-created_at")[:MAX_HITS]):
        owner = n.patient
        if owner is not None:
            hit(owner, "note", n.created_at, n.body)
    for m in (Meeting.objects.filter(has("protocol_summary"), patient__in=clients)
              .select_related("patient").order_by("-scheduled_time")[:MAX_HITS]):
        hit(m.patient, "meeting summary", m.happened_at, m.protocol_summary)
    convs = list(Conversation.objects.filter(patient__in=clients)
                 .filter(has("agent_summary") | has("summary"))
                 .select_related("patient").order_by("-last_message_at")[:MAX_HITS])
    for c in convs:
        # Only the summary the screens show — the agent's where it wrote one,
        # the classifier's otherwise — which a hidden conversation keeps too.
        # A match in a superseded summary is a match in text nobody is shown.
        shown, _source, _at = conversation_summary.machine_summary(c)
        if _starts_word(shown, needle):
            hit(c.patient, "conversation summary", c.last_message_at, shown)

    alerts = list(Alert.objects.filter(patient__in=clients)
                  .filter(has("title") | has("description"))
                  .select_related("patient").order_by("-created_at")[:MAX_HITS])
    withheld = _alerts_withheld(alerts, user)
    for a in alerts:
        # An alert raised from a conversation the client hid keeps its title,
        # and loses what the classifier wrote out of the exchange — as the
        # alert panel does.
        body = a.title if a.pk in withheld else f"{a.title}. {a.description}"
        if _starts_word(body, needle):
            hit(a.patient, "alert", a.created_at, body)

    hits.sort(key=lambda h: h["date"] or "", reverse=True)
    total = len(hits)
    hits = hits[:MAX_HITS]
    matched = {}
    for h in hits:
        matched.setdefault(h["client"]["id"], None)
    patients = list(clients.filter(pk__in=matched))
    _log(user, patients, RecordAccess.Action.SEARCH, via, needle)
    return {"query": needle, "clients_matched": len(patients), "total": total,
            "hits": hits, "truncated": total > len(hits)}
