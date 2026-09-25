"""The tool a client uses to keep a conversation from their link worker.

Two tools, over the same rule the REST endpoint enforces
(``ConvAI.conversation_privacy``): one to read the current answer, one to set
it. Built here rather than inside any one agent because more than one kind of
agent will want them, and because the wording of what the client is being
promised must not be re-invented per agent.

**Not attached to any agent yet.** ``build_privacy_tools`` exists, is tested,
and is registered by nothing: wiring it to a prompt-based agent turns that
agent into a react agent (a plain prompt agent has no tool loop at all), and
the conversation that *asks* the client the question — when to offer it, how
to word it, what to do with a "maybe later" — is the next piece of work. Until
then the switch is reachable through the API and the Django admin only. See
conversation_privacy.md.

The tools are deliberately **synchronous**, for the same reason the RAG and
Link Worker tools are: LangGraph runs sync tools in a worker thread during
``ainvoke()``, where Django ORM access is safe. An async tool would run inside
the event loop and raise ``SynchronousOnlyOperation``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["build_privacy_tools", "PRIVACY_PROMPT_SUFFIX"]


# What the agent must tell the client, in the agent's own turn, rather than
# what this module can enforce. Two of these are limits on the promise, and a
# client agreeing to something they have been half-told is worse than not
# being offered it at all.
PRIVACY_PROMPT_SUFFIX = """

## Keeping a conversation private

You can keep this conversation from the person's link worker, if they ask for
it, with `set_conversation_privacy`.

- Only on their say-so, and only after telling them what it means: their link
  worker will still see that this conversation happened, when it was and how
  many messages it had — they just won't be able to read what was said.
- Tell them two things it does not cover. A supervising administrator can
  still read it. And if anything in it suggests the person may be at risk of
  harming themselves, their link worker will be able to read it, because
  somebody has to be able to help.
- It covers this conversation only. A later conversation starts visible again.
- They can change their mind either way at any point; use the same tool with
  `hidden` set to false.
"""


def _thread_id(cfg) -> str:
    """The conversation the tool is being called inside.

    Taken from the run config — never from a tool argument. A conversation id
    the model could pass is a conversation id the model could get wrong, and
    hiding somebody else's exchange because a digit was hallucinated is not a
    failure worth risking for an argument that carries no information the
    runtime did not already have.
    """
    return str((cfg or {}).get("thread_id") or "")


def _resolve(thread_id: str):
    from ..models import Conversation
    import uuid

    try:
        conv_uuid = uuid.UUID(thread_id)
    except (TypeError, ValueError):
        return None
    return Conversation.objects.filter(id=conv_uuid).first()


def _describe(conv) -> str:
    from ..models import Message

    count = Message.objects.filter(conversation_id=str(conv.id)).count()
    if conv.hidden:
        return (f"PRIVACY_HIDDEN messages={count} — this conversation's content is "
                f"hidden from the link worker. They can see that it happened and "
                f"that it has {count} messages, but not what was said.")
    return (f"PRIVACY_VISIBLE messages={count} — this conversation can be read by "
            f"the link worker.")


def build_privacy_tools():
    """The ``@tool``-decorated pair, ready to hand to a react agent.

    Returns ``[get_conversation_privacy, set_conversation_privacy]``. Imports
    are inside the function so a deployment without the LangChain tool
    machinery can still import this module (and its prompt text).
    """
    from pydantic import BaseModel, Field
    from langchain_core.runnables.config import ensure_config
    from langchain_core.tools import tool

    from .. import conversation_privacy

    @tool("get_conversation_privacy")
    def get_conversation_privacy() -> str:
        """Whether this conversation is currently hidden from the person's link worker."""
        if not conversation_privacy.enabled():
            return "Conversation privacy is not available on this service."
        conv = _resolve(_thread_id(ensure_config().get("configurable", {})))
        if conv is None:
            return "This conversation has not been recorded yet, so there is nothing to hide."
        return _describe(conv)

    class SetPrivacyInput(BaseModel):
        hidden: bool = Field(
            ...,
            description=("True to hide this conversation's content from the person's "
                         "link worker, False to let them read it again. Only ever set "
                         "this because the person asked you to."),
        )

    @tool("set_conversation_privacy", args_schema=SetPrivacyInput)
    def set_conversation_privacy(hidden: bool) -> str:
        """Hide this conversation from the person's link worker, or unhide it.

        The link worker still sees that the conversation happened and how many
        messages it holds; only the content, the summary and the topic are
        withheld. Applies to this conversation alone.
        """
        if not conversation_privacy.enabled():
            return "Conversation privacy is not available on this service."
        conv = _resolve(_thread_id(ensure_config().get("configurable", {})))
        if conv is None:
            # Nothing to write on, and nothing lost: a conversation with no row
            # yet has no messages for anyone to read either.
            return ("This conversation has not been recorded yet, so there is nothing "
                    "to hide. Ask again once you have exchanged a few messages.")
        try:
            conversation_privacy.set_hidden(conv, hidden)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("set_conversation_privacy failed")
            return f"I couldn't change that setting: {exc}"
        return _describe(conv)

    return [get_conversation_privacy, set_conversation_privacy]
