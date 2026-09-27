"""Which summary of a conversation gets shown, and who may write one.

A conversation can end up with two machine-written summaries and one written by
a person, from three different places:

* ``Conversation.summary`` — the classifier's, written by ``review_conversation``
  reading the transcript after the fact.
* ``Conversation.agent_summary`` — the agent's own, reported through the
  ``report_summary`` tool or the summary endpoint while it still had the
  conversation in front of it.
* ``SummaryEdit.body`` — the navigator's, written by hand in the panel.

**One machine summary is shown, and the agent's is preferred.** The agent was in
the conversation; the classifier was reading it from outside, from a transcript,
after it ended. Where the agent has said what the exchange was about, that is
the better answer, and showing both would print two paragraphs that usually say
the same thing twice.

**The machine summary is read-only, and the person's sits beside it.** It used
to be editable in place, which meant a navigator's correction and the model's
original could not both exist — and then an agent reporting later would shadow
the correction with no explanation. Two blocks, neither overwriting the other,
answers both: the record keeps what the model claimed *and* what the person who
knows better says. (Meetings, recordings and alerts keep their in-place edit;
this changed for conversations only.)

**Hidden conversations still show their summary.** This reverses the original
conversation-privacy decision, deliberately. The argument for withholding it was
that an abstract of a conversation *is* the conversation — but a link worker
with no idea what their client needed cannot do the job the client is there for,
and the client asking not to be transcribed is not usually asking to be left
without care. So the line moved: the *words* stay withheld — the messages, the
topic, the detector answers, the review, the CSV — and a summary is shown. An
agent reporting a summary on a hidden conversation is writing for a navigator
who will read it, and ``agent_tools.md`` says so in the tool's own prompt.
"""
from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

__all__ = [
    "AGENT", "CLASSIFIER",
    "machine_summary", "human_summary", "set_agent_summary", "set_human_summary",
]

# What wrote the summary on screen. Returned rather than inferred by the caller,
# because the byline and the tint differ and four surfaces must not each work
# it out from "is agent_summary non-empty" and drift.
AGENT = "agent"
CLASSIFIER = "classifier"


def machine_summary(conversation):
    """The one model-written summary to show, and which model wrote it.

    Returns ``(text, source, written_at)`` with ``source`` one of ``AGENT`` /
    ``CLASSIFIER``, or ``("", "", None)`` when nothing has summarised it yet.
    """
    if conversation is None:
        return "", "", None
    agent_text = (getattr(conversation, "agent_summary", "") or "").strip()
    if agent_text:
        return agent_text, AGENT, getattr(conversation, "agent_summary_at", None)
    classifier_text = (getattr(conversation, "summary", "") or "").strip()
    if classifier_text:
        return classifier_text, CLASSIFIER, getattr(conversation, "analyzed_at", None)
    return "", "", None


def human_summary(conversation):
    """The navigator's own summary of this conversation, if one was written.

    Returns ``(text, author_name, edited_at)``, or ``("", "", None)``. A
    ``SummaryEdit`` row with a blank ``body`` is not a person's summary — on the
    in-place kinds that is what every row looks like — so it reads as absent.
    """
    if conversation is None:
        return "", "", None
    try:
        edit = conversation.summary_edit
    except (ObjectDoesNotExist, AttributeError):
        return "", "", None
    body = (getattr(edit, "body", "") or "").strip()
    if not body:
        return "", "", None
    author = edit.author
    name = ((author.get_full_name() or author.username) if author else "")
    return body, name, edit.edited_at


def set_agent_summary(conversation, text: str):
    """Record the agent's summary of this conversation. Returns the conversation.

    Overwrites rather than appends, and re-stamps the time. A conversation grows
    while it is happening, so the agent's latest reading of it is the one worth
    keeping — and an agent that reports twice should not have to care, exactly
    as the visibility endpoint is idempotent for the same reason.

    A blank report is refused by the callers rather than swallowed here: erasing
    a summary a navigator may already have read is a different act from writing
    one, and nothing has asked for it.
    """
    conversation.agent_summary = (text or "").strip()
    conversation.agent_summary_at = timezone.now()
    conversation.save(update_fields=["agent_summary", "agent_summary_at"])
    return conversation


def set_human_summary(conversation, body: str, author):
    """Record a person's own summary beside the machine one.

    ``update_or_create`` rather than a row per edit: this records whose words
    the summary is now, not every hand that has passed over it — the same
    reasoning ``SummaryEdit`` was built with.
    """
    from .models import SummaryEdit

    edit, _created = SummaryEdit.objects.update_or_create(
        conversation=conversation,
        defaults={"author": author, "body": (body or "").strip()},
    )
    return edit
