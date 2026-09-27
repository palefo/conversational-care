"""Conversational Care tools for a LangGraph agent, in one import.

For **remote** agents: the ones that run on their own LangGraph server and are
connected to Conversational Care as a *Remote* agent with **Allow callbacks**
switched on. (Prompt-based agents inside Conversational Care get the same tools
by ticking a box on the agent form, and do not need this module.)

    from conversationalcare_api.langgraph_tools import build_cc_tools, CC_PROMPT

    graph = create_react_agent(
        model=model,
        tools=[get_current_date, *build_cc_tools()],
        prompt=lambda state, config: [{"role": "system",
                                       "content": BASE_PROMPT + CC_PROMPT}] + state["messages"],
    )

What the tools are careful about, so each agent does not have to be:

* **They take no conversation or client id.** The conversation is the one
  named by the ``cc_run_token`` in the run config, which Conversational Care
  issues for this run alone. A model cannot point them at anybody else.
* **They never raise into the graph.** With no token — ``langgraph dev``, tests,
  callbacks switched off — they tell the model the platform is not connected,
  and the conversation carries on.
* **They carry the platform's own wording** for the system prompt, so what the
  agent promises a client about privacy matches what the platform does. The
  Conversational Care test suite checks these copies against the platform's.
"""
# No ``from __future__ import annotations`` here: LangChain reads each tool's
# signature to build its schema and to inject ``config``, and string annotations
# naming ``RunnableConfig`` (imported inside build_cc_tools) cannot be resolved.
from .run_client import ConversationalCareError, RunClient

__all__ = ["build_cc_tools", "CC_PROMPT", "PRIVACY_PROMPT", "SUMMARY_PROMPT"]

# Mirrors ConvAI/native_agents/privacy_tool.py:PRIVACY_PROMPT_SUFFIX.
PRIVACY_PROMPT = """

## Keeping a conversation private

You can keep this conversation from the person's link worker, if they ask for
it, with `set_conversation_privacy`.

- Only on their say-so, and only after telling them what it means: their link
  worker will still see that this conversation happened, when it was and how
  many messages it had — they just won't be able to read what was said.
- Tell them their link worker will still read a **summary** of it, so that
  somebody who has to help them still knows roughly what they needed. What
  they lose is the conversation itself, not the fact of what it was about. If
  there is something in here they would not want summarised either, say so
  now rather than after.
- Tell them two things it does not cover. A supervising administrator can
  still read it. And if anything in it suggests the person may be at risk of
  harming themselves, their link worker will be able to read it, because
  somebody has to be able to help.
- It covers this conversation only. A later conversation starts visible again.
- They can change their mind either way at any point; use the same tool with
  `hidden` set to false.
"""

# Mirrors ConvAI/native_agents/summary_tool.py:SUMMARY_PROMPT_SUFFIX.
SUMMARY_PROMPT = """

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

CC_PROMPT = PRIVACY_PROMPT + SUMMARY_PROMPT

NOT_CONNECTED = ("Conversational Care is not connected for this conversation, so this "
                 "cannot be done here. Carry on without it.")


def build_cc_tools(*, privacy: bool = True, summary: bool = True, base_url=None):
    """The tools, ready for ``create_react_agent``.

    ``privacy`` adds ``get_conversation_privacy`` and ``set_conversation_privacy``;
    ``summary`` adds ``report_summary``. ``base_url`` overrides where the API is,
    for an agent server that cannot rely on ``cc_api_url`` or the
    ``CONVERSATIONAL_CARE_BASE_URL`` environment variable.
    """
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import tool

    def client(config):
        return RunClient.from_config(config, base_url=base_url)

    tools = []

    if summary:
        @tool("report_summary")
        def report_summary(summary: str, config: RunnableConfig) -> str:
            """Record what this conversation was about, for the person's link worker.

            Replaces any summary reported before, so it should stand on its own.
            Applies to this conversation alone.

            Args:
                summary: A few sentences for the link worker: what the person
                    wanted, what you told them, what is still open.
            """
            text = (summary or "").strip()
            if not text:
                return "SUMMARY_REJECTED — a summary cannot be empty."
            cc = client(config)
            if cc is None:
                return NOT_CONNECTED
            try:
                cc.report_summary(text)
            except ConversationalCareError as exc:
                return f"SUMMARY_REJECTED — {exc}"
            except Exception as exc:  # network: tell the model, keep the conversation
                return f"The summary could not be saved right now ({exc})."
            return ("SUMMARY_RECORDED — this is now the summary the link worker will see "
                    "for this conversation. Reporting again replaces it.")

        tools.append(report_summary)

    if privacy:
        @tool("get_conversation_privacy")
        def get_conversation_privacy(config: RunnableConfig) -> str:
            """Whether this conversation is currently hidden from the person's link worker."""
            cc = client(config)
            if cc is None:
                return NOT_CONNECTED
            try:
                state = cc.get()
            except Exception as exc:
                return f"Could not check right now ({exc})."
            if not state:
                return NOT_CONNECTED
            if not state.get("privacy_available"):
                return "Keeping a conversation private is not available on this service."
            return ("PRIVACY_HIDDEN — hidden from the link worker; they can see that it "
                    "happened and its summary, not what was said." if state.get("hidden")
                    else "PRIVACY_VISIBLE — the link worker can read this conversation.")

        @tool("set_conversation_privacy")
        def set_conversation_privacy(hidden: bool, config: RunnableConfig) -> str:
            """Hide this conversation from the person's link worker, or unhide it.

            Only ever because the person asked, and only after telling them what
            it means. Applies to this conversation alone.

            Args:
                hidden: True to hide it from their link worker, False to let
                    them read it again.
            """
            cc = client(config)
            if cc is None:
                return NOT_CONNECTED
            try:
                state = cc.set_visibility(hidden)
            except Exception as exc:
                return f"That setting could not be changed right now ({exc})."
            if state is None:
                return "Keeping a conversation private is not available on this service."
            return ("PRIVACY_HIDDEN — hidden from the link worker." if state.get("hidden")
                    else "PRIVACY_VISIBLE — the link worker can read it again.")

        tools.extend([get_conversation_privacy, set_conversation_privacy])

    return tools
