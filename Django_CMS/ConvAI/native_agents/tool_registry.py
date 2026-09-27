"""The platform tools a prompt-based agent can be given, in one table.

``Agent.tools`` stores slugs from here. Everything else about a tool — what the
checkbox says, what the tab is called, the wording appended to the system
prompt, and how the LangGraph tools are built — lives in this one dict, so
adding a third tool is one entry plus a builder and touches no form, template,
migration or graph code.

Two things are deliberately *not* stored on the agent:

* **The default prompt.** An agent that has not overridden a tool's wording has
  no ``prompt`` key at all, and reads the default from here at build time. Had
  the default been copied into the row on save, improving the shipped wording
  would reach no existing agent, and "Reset to default" would write a copy that
  is right only until the next release. Reset is a key deletion.

A tool's entry is ``{}`` (on, shipped wording), ``{"prompt": …}`` (on, edited)
or ``{"enabled": false, "prompt": …}`` (off, but its edited wording kept, so
unticking a box by accident and saving does not throw away somebody's work).
A tool that is off with nothing edited has no entry at all.
* **Whether the tool works.** ``available()`` is asked at render and at build
  time, because a tool can be switched off by an installation-wide setting
  after an agent was configured with it. Conversation privacy is the live
  example: it is off by default, and an agent that lists the tool while the
  feature is off would tell a client it can promise something it cannot.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

__all__ = ["TOOLS", "slugs", "spec", "enabled_slugs", "prompt_for",
           "default_prompt", "build_tools", "prompt_suffix", "is_available"]


def _privacy_available() -> bool:
    from .. import conversation_privacy
    return conversation_privacy.enabled()


def _always() -> bool:
    return True


# slug -> spec. Order is the order the tabs appear *and* the order the prompts
# are concatenated in, which is why it is a dict literal and not sorted at use
# time: the assembled system prompt has to read the same way twice, and a model
# reads what comes last most keenly.
TOOLS = {
    "conversation_privacy": {
        "label": _("Conversation privacy"),
        "tab": _("Privacy"),
        # One checkbox, two tools: an agent that can see whether a conversation
        # is hidden but not change it is not a configuration anybody has asked
        # for, and splitting them would put a choice on the form that has one
        # sensible answer.
        "tools": ("get_conversation_privacy", "set_conversation_privacy"),
        "description": _("Lets the agent hide this conversation from the client's link "
                         "worker when the client asks, and check whether it is hidden. "
                         "Requires Conversation privacy to be switched on in "
                         "Settings → Privacy."),
        "unavailable": _("Conversation privacy is switched off for this installation "
                         "(Settings → Privacy). The agent will not be given this tool "
                         "until it is switched on."),
        "available": _privacy_available,
        "builder": "ConvAI.native_agents.privacy_tool:build_privacy_tools",
        "prompt": "ConvAI.native_agents.privacy_tool:PRIVACY_PROMPT_SUFFIX",
    },
    "report_summary": {
        "label": _("Report summary"),
        "tab": _("Summary"),
        "tools": ("report_summary",),
        "description": _("Lets the agent write the summary of the conversation that the "
                         "client's link worker reads. Preferred over the automatic "
                         "summary, and shown even when the conversation is hidden."),
        "unavailable": "",
        "available": _always,
        "builder": "ConvAI.native_agents.summary_tool:build_summary_tools",
        "prompt": "ConvAI.native_agents.summary_tool:SUMMARY_PROMPT_SUFFIX",
    },
}


def _resolve(path: str):
    """``'pkg.module:name'`` → the object, imported lazily.

    Lazily because a builder pulls in the LangChain tool machinery, and the
    agent form renders the prompt text on every page load without needing it.
    """
    from importlib import import_module

    module_path, _sep, attr = path.partition(":")
    return getattr(import_module(module_path), attr)


def slugs() -> list[str]:
    return list(TOOLS.keys())


def spec(slug: str) -> dict | None:
    return TOOLS.get(slug)


def is_available(slug: str) -> bool:
    """Whether this installation's settings let this tool actually run."""
    s = TOOLS.get(slug)
    if not s:
        return False
    try:
        return bool(s["available"]())
    except Exception:  # pragma: no cover - a settings read must not break the form
        return False


def default_prompt(slug: str) -> str:
    """The shipped wording for a tool, as the tool's own module defines it."""
    s = TOOLS.get(slug)
    if not s:
        return ""
    try:
        return str(_resolve(s["prompt"])).strip()
    except Exception:  # pragma: no cover - defensive
        return ""


def enabled_slugs(agent) -> list[str]:
    """The tools this agent has on, in registry order, ignoring unknown slugs.

    Registry order rather than the stored key order, and not as a precaution:
    ``Agent.tools`` is a ``jsonb`` column, and Postgres does not keep the key
    order it was given — it sorts by key length, then bytes. So a row saved with
    privacy first reads back with ``report_summary`` first, and reading the
    stored order would silently reorder the assembled system prompt. Unknown
    slugs are dropped rather than raising: a row written by a newer release, or
    a tool since removed, should not stop an agent answering.
    """
    stored = getattr(agent, "tools", None) or {}
    if not isinstance(stored, dict):
        return []
    return [s for s in TOOLS if s in stored and is_enabled_entry(stored[s])]


def is_enabled_entry(entry) -> bool:
    """Whether a stored tool entry means "on". Absent ``enabled`` means on."""
    if not isinstance(entry, dict):
        return bool(entry)
    return entry.get("enabled", True) is not False


def prompt_for(agent, slug: str) -> str:
    """This agent's wording for a tool: its override, or the shipped default."""
    stored = (getattr(agent, "tools", None) or {}).get(slug) or {}
    if isinstance(stored, dict):
        override = (stored.get("prompt") or "").strip()
        if override:
            return override
    return default_prompt(slug)


def prompt_suffix(agent) -> str:
    """Every enabled, available tool's wording, concatenated in registry order.

    Only *available* tools contribute. A prompt describing a tool the graph was
    not given is how you get an agent telling a client their conversation is
    hidden when nothing hid it.
    """
    parts = [prompt_for(agent, slug) for slug in enabled_slugs(agent)
             if is_available(slug)]
    return "".join("\n\n" + p for p in parts if p)


def build_tools(agent) -> list:
    """The LangGraph tool objects for everything this agent has enabled.

    A builder that raises is logged and skipped: one misconfigured tool must not
    cost the agent the other one, nor the turn.
    """
    import logging

    logger = logging.getLogger(__name__)
    out = []
    for slug in enabled_slugs(agent):
        if not is_available(slug):
            logger.info("Tool %r is enabled on agent %s but switched off for this "
                        "installation; not building it.", slug, getattr(agent, "pk", "?"))
            continue
        try:
            out.extend(_resolve(TOOLS[slug]["builder"])())
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not build tool %r for agent %s",
                             slug, getattr(agent, "pk", "?"))
    return out
