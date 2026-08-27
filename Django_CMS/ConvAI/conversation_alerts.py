"""Classifying a conversation, and raising an alert when a detector fires.

Until now these were two halves that never met. ``classify_conversation_with_llm``
could tell that a caregiver had said something that needed a human, and
``Alert`` was how a human got told — but the only thing that ever wrote the
first result anywhere was a button on the Settings page, and all it wrote was
``Conversation.is_important``, which appears on no queue and in no count. A
disclosure could sit correctly classified in the database and still reach
nobody.

This module is the join. One entry point, :func:`review_conversation`, is used
by both callers: the hook on the inbound message path, and the batch button in
Settings. Whatever the two disagree about, it will not be this.

Two rules worth stating plainly, because they are the ones a reader will want
to check:

* ``important`` still only sets ``is_important``. It is the model's opinion
  that a conversation is worth reading, which is a review flag, not a page.
  Alerts come from detectors an admin explicitly marked as raising, so what
  interrupts somebody is always something a person chose in advance — plus
  the compiled-in self-harm floor, which nobody has to remember to choose.
* One open alert per conversation per detector. A caregiver in a long crisis
  chat is one thing that happened, not fourteen.
"""
import logging

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from .models import Alert, Conversation, Message, Patient
from .utils_conversation_classification import (
    build_message_rows_for_conv,
    classify_conversation_with_llm,
    detectors_for,
)

logger = logging.getLogger(__name__)


def _resolve_agent(conv):
    """The agent whose detectors and role apply to this conversation.

    Prefer the one recorded on the Conversation. Older rows predate that field
    being filled in reliably, so fall back through the patient — first the one
    linked to the conversation, then the one owning the first message's phone
    number, which is how the Settings batch has always found it.
    """
    if getattr(conv, "agent", None):
        return conv.agent
    if getattr(conv, "patient", None) and conv.patient.agent_id:
        return conv.patient.agent

    first_msg = (Message.objects
                 .filter(conversation_id=str(conv.id))
                 .order_by("timestamp")
                 .first())
    if not first_msg:
        return None
    sender = (first_msg.user or "").strip()
    if not sender:
        return None
    patient = (Patient.objects
               .filter(Q(phone_number=sender) | Q(caregiver__phone_number=sender))
               .select_related("agent")
               .first())
    return getattr(patient, "agent", None) if patient else None


def _patient_for(conv):
    """The client an alert about this conversation would be about."""
    if getattr(conv, "patient", None):
        return conv.patient
    first_msg = (Message.objects
                 .filter(conversation_id=str(conv.id))
                 .order_by("timestamp")
                 .first())
    sender = (first_msg.user or "").strip() if first_msg else ""
    if not sender:
        return None
    return (Patient.objects
            .filter(Q(phone_number=sender) | Q(caregiver__phone_number=sender))
            .select_related("navigator")
            .first())


def _already_open(conv_id: str, label: str) -> bool:
    """Is there an unresolved alert for this conversation and this detector?

    Matched on the detector label rather than the title so that renaming what
    an alert is called does not start a second one about the same thing.
    """
    return (Alert.objects
            .exclude(status=Alert.AlertStatus.RESOLVED)
            .filter(data__conversation_id=conv_id, data__detector=label)
            .exists())


def _raise(conv, patient, detector, trigger: str, abstract: str, topic: str) -> Alert:
    """Create the alert a fired detector earns.

    ``data['conversation_id']`` is the important part: the detail panel resolves
    the exchange behind an alert from exactly that key, so the Conversation tab
    opens on the real thread rather than on the day's messages guessed by date.
    """
    return Alert.objects.create(
        patient=patient,
        user=patient.navigator,
        # No created_by: nobody created this. The audit trail says what it was,
        # and CONVERSATION is the type that has always meant "the classifier
        # produced this" — it just never had anything producing it.
        alert_type=Alert.AlertType.CONVERSATION,
        priority=detector.priority,
        # The detector label, not free text. A queue of these is meant to be
        # scannable, and two navigators describing the same thing differently
        # is precisely what the label exists to prevent.
        title=detector.label[:200],
        # What the panel's summary block shows. The trigger sentence sits above
        # it in its own block rather than being glued on here, so a navigator
        # editing the summary cannot accidentally erase why they were called.
        description=abstract,
        data={
            "conversation_id": str(conv.id),
            "detector": detector.label,
            "trigger": trigger,
            "trigger_at": conv.last_message_at.isoformat() if conv.last_message_at else "",
            "classification": topic,
            "raised_by_human": False,
        },
    )


def review_conversation(conversation_id, *, raise_alerts: bool = True) -> dict:
    """Classify one conversation, store the result, and raise what it earns.

    Returns ``{"analyzed": bool, "alerts": [Alert, ...]}``. Never raises: this
    runs on the ingest pool behind a caregiver's reply, and a classifier that
    is down must not turn into a message that failed to send.
    """
    result = {"analyzed": False, "alerts": []}

    try:
        conv = (Conversation.objects
                .select_related("agent", "patient", "patient__navigator")
                .filter(id=conversation_id)
                .first())
    except (ValueError, TypeError, ValidationError):
        # An id that is not a UUID cannot name a conversation. Returning is
        # right; raising would only be logged as a generic pool failure.
        logger.warning("Cannot review %r: not a conversation id.", conversation_id)
        return result
    if not conv:
        return result

    conv_id = str(conv.id)
    rows = build_message_rows_for_conv(conv_id, Message)
    if not rows:
        # Nothing said yet. Mark it seen so the batch does not keep picking it
        # up, and leave everything else alone.
        conv.analyzed = True
        conv.analyzed_at = timezone.now()
        conv.save(update_fields=["analyzed", "analyzed_at"])
        return result

    agent = _resolve_agent(conv)

    try:
        verdict = classify_conversation_with_llm(rows, agent=agent)
    except Exception:
        logger.exception("Classification failed for conversation %s", conv_id)
        return result

    fired = verdict.get("detectors") or {}
    triggers = verdict.get("triggers") or {}
    abstract = (verdict.get("abstract") or "")[:2000]
    topic = (verdict.get("classification") or "")[:120]

    conv.summary = abstract
    conv.topic = topic
    conv.is_important = bool(verdict.get("important"))
    conv.auto_flags = fired if isinstance(fired, dict) else {}
    conv.analyzed = True
    conv.analyzed_at = timezone.now()
    # Re-analysing means there is something new to look at, so it goes back in
    # the unread pile — the same thing the Settings batch has always done.
    conv.visited = False
    conv.save(update_fields=["summary", "topic", "is_important", "auto_flags",
                             "analyzed", "analyzed_at", "visited"])
    result["analyzed"] = True

    if not raise_alerts:
        return result

    patient = _patient_for(conv)
    if not patient:
        # An Alert has to be about somebody — the model enforces it. An
        # unmatched phone number is a real case (self-registration in
        # progress), and it is not one an alert can describe.
        if any(fired.values()):
            logger.warning("Conversation %s fired detectors but has no client; "
                           "no alert raised.", conv_id)
        return result

    for label, detector in detectors_for(agent).items():
        if not detector.raises or not fired.get(label):
            continue
        if _already_open(conv_id, label):
            continue
        try:
            result["alerts"].append(
                _raise(conv, patient, detector,
                       trigger=triggers.get(label, ""), abstract=abstract, topic=topic))
        except Exception:
            logger.exception("Could not raise '%s' alert for conversation %s",
                             label, conv_id)

    return result


def review_conversation_async(conversation_id) -> None:
    """Queue :func:`review_conversation` on the ingestion pool.

    The caregiver's reply has already been sent by the time this runs. The
    classifier is a second model call and would otherwise be added to the wait
    for every single message.
    """
    from .async_reply import submit_ingest
    submit_ingest(review_conversation, conversation_id)
