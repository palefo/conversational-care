"""The tool an agent uses to say what the conversation was about.

One tool, ``report_summary``, over the same ``conversation_summary`` functions
the REST endpoint calls. Built here rather than inside any one agent because
more than one kind of agent wants it, and because the *wording* of what a
summary is for — a link worker who has to pick the conversation up — should not
be re-invented per agent.

The summary an agent writes is preferred over the classifier's, so this is not a
second opinion the panel might show: it is the one a navigator will read. That
is the whole reason the prompt below is as specific as it is about audience,
length and what not to put in it.

The tool takes **no conversation id**. The thread comes from the run config, as
it does in ``privacy_tool``, for the same reason: an id the model could pass is
an id the model could get wrong, and writing a summary onto somebody else's
conversation because a digit was hallucinated carries no information the runtime
did not already have.

Deliberately **synchronous**, like the RAG, Link Worker and privacy tools:
LangGraph runs sync tools in a worker thread during ``ainvoke()``, where Django
ORM access is safe. An async tool would run inside the event loop and raise
``SynchronousOnlyOperation``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["build_summary_tools", "SUMMARY_PROMPT_SUFFIX", "MAX_SUMMARY_CHARS"]


# Long enough for the ~80 words the classifier's own abstract instruction asks
# for, with room for an agent that has more to hand over, and short enough that
# the panel block it lands in is still a block rather than a second transcript.
MAX_SUMMARY_CHARS = 2000


# What the agent needs to know to write a summary worth reading, rather than
# what this module can enforce. The audience line is the important one: a model
# asked for "a summary" writes one for whoever it was just talking to, and this
# one is read by somebody who was not there.
SUMMARY_PROMPT_SUFFIX = """

## Summarising the conversation

Use `report_summary` to record what this conversation was about.

- Write it for the person's **link worker**, who was not here and will read it
  to pick up where you left off. Not for the person you are talking to.
- Say what they wanted, what you told them, and anything left open or worth
  following up. A few sentences; under 150 words.
- Write it in the language the rest of the record is in, and name people the
  way the conversation did.
- Report it once you know what the conversation was about, and again at the end
  if it moved on. Each report replaces the last, so the newest one should stand
  on its own — do not write "as mentioned above".
- Do not put anything in it that the person asked you to keep private beyond
  the summary itself, and nothing you are not confident they said. If this
  conversation is hidden from the link worker, the summary is the one thing
  they *will* see, so it must be something the person would expect them to
  read.
"""


def _thread_id(cfg) -> str:
    """The conversation the tool is being called inside.

    Taken from the run config — never from a tool argument. See the module
    docstring, and ``privacy_tool._thread_id``, which this mirrors on purpose:
    the two tools must not disagree about what "this conversation" means.
    """
    return str((cfg or {}).get("thread_id") or (cfg or {}).get("conversation_id") or "")


def _resolve(thread_id: str):
    from ..models import Conversation
    import uuid

    try:
        conv_uuid = uuid.UUID(thread_id)
    except (TypeError, ValueError):
        return None
    return Conversation.objects.filter(id=conv_uuid).first()


def build_summary_tools():
    """The ``@tool``-decorated ``[report_summary]``, ready for a react agent.

    Imports are inside the function so a deployment without the LangChain tool
    machinery can still import this module (and its prompt text) — the agent
    form reads ``SUMMARY_PROMPT_SUFFIX`` from here on every page render.
    """
    from pydantic import BaseModel, Field
    from langchain_core.runnables.config import ensure_config
    from langchain_core.tools import tool

    from .. import conversation_summary

    class ReportSummaryInput(BaseModel):
        summary: str = Field(
            ...,
            description=("What this conversation was about, written for the "
                         "person's link worker to read. A few sentences: what "
                         "they wanted, what you told them, what is still open."),
        )

    @tool("report_summary", args_schema=ReportSummaryInput)
    def report_summary(summary: str) -> str:
        """Record what this conversation was about, for the person's link worker.

        Replaces any summary previously reported for this conversation, so the
        text given here should stand on its own. Applies to this conversation
        alone.
        """
        text = (summary or "").strip()
        if not text:
            # Refused rather than written: erasing a summary a navigator may
            # already have read is a different act from writing one.
            return ("SUMMARY_REJECTED — a summary cannot be empty. Say what the "
                    "conversation was about, or don't call this tool.")
        if len(text) > MAX_SUMMARY_CHARS:
            return (f"SUMMARY_REJECTED — too long ({len(text)} characters, "
                    f"limit {MAX_SUMMARY_CHARS}). This is a summary, not a "
                    f"transcript; shorten it and try again.")

        conv = _resolve(_thread_id(ensure_config().get("configurable", {})))
        if conv is None:
            return ("This conversation has not been recorded yet, so there is "
                    "nothing to summarise. Try again once you have exchanged a "
                    "few messages.")
        try:
            conversation_summary.set_agent_summary(conv, text)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("report_summary failed")
            return f"I couldn't save that summary: {exc}"
        logger.info("Agent summary recorded for conversation %s (%d chars)",
                    conv.id, len(text))
        return ("SUMMARY_RECORDED — this is now the summary the link worker will "
                "see for this conversation. Reporting again replaces it.")

    return [report_summary]
