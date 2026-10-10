"""Whose a message is — decided once, when it arrives, and never re-derived.

A phone number is an *address*, not an identity. It is the right thing to use
at the moment a message arrives, because it is all the platform has: Twilio
says "this came from +44…", and somebody has to be looked up. It is the wrong
thing to use at any later moment, because numbers change hands. A client who
gets a new number should keep their history; a stranger who is given a client's
old number should not inherit it; and a caregiver who stops looking after
somebody should not take that person's conversations with them.

So every message is stamped when it is written — ``Message.patient`` (whose
client file), ``Message.account`` (which login it came through, if any) and
``Message.sender_role`` (who wrote in) — and every read goes through those
fields. Numbers are matched in exactly two places: :func:`resolve_inbound`, on
receipt, and the fallback in :func:`patient_messages_q`, for rows written
before attribution existed. The same move was made for call recordings in
migration 0080; see call_recording_attribution.md.

**One resolver for every inbound path.** WhatsApp text, WhatsApp audio (sync
and async) and SMS used to each run their own
``Patient.objects.filter(phone | caregiver phone).first()`` — unordered, so a
number shared by two clients went to whichever the database returned first,
and could go to a different one next time. The rule is now:

1. A client's own number beats a caregiver's number.
2. Among several clients still, the one this number most recently wrote about.
3. Failing that, the oldest client record.

A shared number is not an error — a household shares a phone, and a caregiver
can look after two people — but it is something an admin should know about, so
every ambiguous match is logged and :func:`shared_numbers` lists them on the
Settings page.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Q

logger = logging.getLogger(__name__)

__all__ = [
    "Inbound", "normalise", "resolve_inbound", "create_message",
    "patient_messages_q", "navigator_messages_q", "legacy_q", "shared_numbers",
    "role_for_number",
]


@dataclass
class Inbound:
    """What a number resolved to on receipt."""

    patient: object = None
    role: str = ""
    candidates: list = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


def normalise(number) -> str:
    """The number as the Patient and Caregiver columns store it (E.164)."""
    return str(number or "").replace("whatsapp:", "").replace(" ", "").strip()


def resolve_inbound(number) -> Inbound:
    """The client a message from ``number`` belongs to, and who wrote it.

    Call this where a message arrives, and nowhere else. The answer is written
    onto the message by :func:`create_message` so that nothing downstream has
    to ask again — see the module docstring.
    """
    from .models import Message, Patient

    number = normalise(number)
    if not number:
        return Inbound()

    own = list(Patient.objects.filter(phone_number=number)
               .select_related("agent", "caregiver", "navigator").order_by("pk"))
    if own:
        candidates, role = own, Message.SenderRole.CLIENT
    else:
        candidates = list(Patient.objects.filter(caregiver__phone_number=number)
                          .select_related("agent", "caregiver", "navigator")
                          .order_by("pk"))
        role = Message.SenderRole.CAREGIVER
    if not candidates:
        return Inbound()
    if len(candidates) == 1:
        return Inbound(candidates[0], role, candidates)

    # Several clients share this number at the same priority. The one this
    # number most recently wrote about is almost always the conversation still
    # going on; with no history on this number at all, the oldest record.
    recent = (Message.objects
              .filter(patient__in=candidates, user=number)
              .order_by("-timestamp")
              .values_list("patient_id", flat=True)
              .first())
    chosen = next((p for p in candidates if p.pk == recent), candidates[0])
    logger.warning(
        "Inbound number matches %d clients (%s, as %s); attributed to client %s. "
        "See Settings -> shared numbers.",
        len(candidates), ", ".join(str(p.pk) for p in candidates), role, chosen.pk)
    return Inbound(chosen, role, candidates)


def role_for_number(number, patient) -> str:
    """Client or caregiver, for a number already known to belong to ``patient``.

    For write sites that resolved the client some other way — an outbound
    reminder addressed to the caregiver, a tester chat — and only need to know
    which side of the pair a number is.
    """
    from .models import Message

    number = normalise(number)
    if patient is not None and number:
        if normalise(patient.phone_number) == number:
            return Message.SenderRole.CLIENT
        caregiver = getattr(patient, "caregiver", None)
        if caregiver is not None and normalise(caregiver.phone_number) == number:
            return Message.SenderRole.CAREGIVER
    return Message.SenderRole.CLIENT


def create_message(*, conversation_id, user, sender_role, user_message="",
                   response_message="", patient=None, account=None, **extra):
    """Write a Message with its owner fixed. The only way messages are written.

    ``sender_role`` has no default on purpose: every write site has to say who
    wrote in, rather than leaving a blank that reads as "written before
    attribution existed" and so falls back to number matching forever.
    """
    from .models import Message

    return Message.objects.create(
        conversation_id=conversation_id,
        user=user or "",
        user_message=user_message,
        response_message=response_message,
        patient=patient,
        account=account,
        sender_role=sender_role,
        **extra,
    )


def legacy_q() -> Q:
    """Rows written before attribution existed and the backfill could not place.

    Exactly these, and no others, may still be matched by number. A row with an
    owner, or with a sender_role — self-registration, which has neither owner —
    was stamped when it was written and is never re-derived.
    """
    return Q(patient__isnull=True, account__isnull=True, sender_role="")


def _numbers(patient) -> list[str]:
    # Kept on the instance: formatting a PhoneNumber is slow enough to show on
    # a page that asks for every client's numbers three times over. A fresh
    # instance (every request loads its own) always formats anew.
    cached = getattr(patient, "_attribution_numbers", None)
    if cached is not None:
        return list(cached)
    nums = []
    if patient.phone_number:
        nums.append(str(patient.phone_number))
    caregiver = getattr(patient, "caregiver", None)
    if caregiver is not None and caregiver.phone_number:
        nums.append(str(caregiver.phone_number))
    patient._attribution_numbers = tuple(nums)
    return nums


def patient_messages_q(patient) -> Q:
    """Every message on ``patient``'s file.

    By ``Message.patient``, plus — for legacy rows only — the two ways they
    used to be matched: the client's or caregiver's *current* number, or a
    conversation already tied to this client. The fallback can be deleted once
    ``legacy_q`` matches nothing on a given installation.
    """
    from .models import Conversation

    q = Q(patient=patient)
    fallback = Q(pk__in=[])
    nums = _numbers(patient)
    if nums:
        fallback |= Q(user__in=nums)
    conv_ids = [str(c) for c in
                Conversation.objects.filter(patient=patient).values_list("id", flat=True)]
    if conv_ids:
        fallback |= Q(conversation_id__in=conv_ids)
    return q | (legacy_q() & fallback)


def per_patient_aggregates(patients, queryset, fields=(), aggregates=None, *, owned=True, legacy=True) -> dict:
    """``queryset`` aggregated over each client's file — many clients at once.

    The same rule as :func:`patient_messages_q`, without asking it once per
    client: lists that cover every client (Communications, Clients) used to,
    and paid five queries and a scan of the message table for each one. Here
    it is two queries however many clients there are — the rows that name
    their client (``owned``), and the legacy rows (``legacy``), matched in
    Python to whoever holds their number or their conversation.

    Returns ``{patient_pk: [row, …]}``, one row per value of ``fields`` with
    the ``aggregates`` (Max, Min, Count or Sum) folded together across both
    halves. A legacy row matching two clients (a shared number) counts for
    both, exactly as each client's own query would have counted it.
    """
    from django.db.models import Count, Max, Min, Sum
    from .models import Conversation

    patients = list(patients)
    aggregates = dict(aggregates or {})
    fields = tuple(fields)
    merge = {}
    for name, expr in aggregates.items():
        if isinstance(expr, Max):
            merge[name] = lambda a, b: b if a is None else a if b is None else max(a, b)
        elif isinstance(expr, Min):
            merge[name] = lambda a, b: b if a is None else a if b is None else min(a, b)
        elif isinstance(expr, (Count, Sum)):
            merge[name] = lambda a, b: (a or 0) + (b or 0)
        else:
            raise ValueError(f"cannot merge aggregate {name!r} across halves")

    out = {p.pk: {} for p in patients}

    def fold(pk, row):
        key = tuple(row[f] for f in fields)
        have = out[pk].get(key)
        if have is None:
            out[pk][key] = {f: row[f] for f in fields} | {n: row[n] for n in aggregates}
        else:
            for n in aggregates:
                have[n] = merge[n](have[n], row[n])

    # order_by() clears Message's default ordering, which would otherwise be
    # added to the GROUP BY and split every group into single rows.
    if owned:
        rows = (queryset.filter(patient_id__in=list(out))
                .order_by().values("patient_id", *fields).annotate(**aggregates))
        for row in rows:
            fold(row["patient_id"], row)

    if legacy:
        by_number, by_conv = defaultdict(set), defaultdict(set)
        for p in patients:
            for n in _numbers(p):
                by_number[n].add(p.pk)
        for conv_id, pk in Conversation.objects.filter(patient_id__in=list(out)).values_list("id", "patient_id"):
            by_conv[str(conv_id)].add(pk)
        if by_number or by_conv:
            fallback = Q(pk__in=[])
            if by_number:
                fallback |= Q(user__in=list(by_number))
            if by_conv:
                fallback |= Q(conversation_id__in=list(by_conv))
            rows = (queryset.filter(legacy_q() & fallback)
                    .order_by().values("user", "conversation_id", *fields).annotate(**aggregates))
            for row in rows:
                for pk in by_number.get(row["user"], set()) | by_conv.get(row["conversation_id"], set()):
                    fold(pk, row)

    return {pk: list(rows.values()) for pk, rows in out.items()}


def navigator_messages_q(user) -> Q:
    """Every message on the files of ``user``'s clients."""
    from .models import Conversation, Patient

    q = Q(patient__navigator=user)
    fallback = Q(pk__in=[])
    nums = []
    for p in Patient.objects.filter(navigator=user).select_related("caregiver"):
        nums.extend(_numbers(p))
    if nums:
        fallback |= Q(user__in=nums)
    conv_ids = [str(c) for c in Conversation.objects.filter(patient__navigator=user)
                .values_list("id", flat=True)]
    if conv_ids:
        fallback |= Q(conversation_id__in=conv_ids)
    return q | (legacy_q() & fallback)


def shared_numbers() -> dict:
    """Numbers held by more than one client, as ``{number: [(role, patient), …]}``.

    A caregiver of two clients appears here once per client, which is the point:
    it is a number :func:`resolve_inbound` has to choose between.
    """
    from .models import Patient

    holders = defaultdict(list)
    for p in Patient.objects.select_related("caregiver").order_by("pk"):
        if p.phone_number:
            holders[str(p.phone_number)].append(("client", p))
        if p.caregiver is not None and p.caregiver.phone_number:
            holders[str(p.caregiver.phone_number)].append(("caregiver", p))
    return {n: hs for n, hs in holders.items() if len({p.pk for _r, p in hs}) > 1}
