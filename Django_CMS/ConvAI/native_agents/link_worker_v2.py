"""Link Worker v2 (beta): the staff assistant that can read a client's record.

v1 (``link_worker``) finds clients and books meetings. v2 does that and answers
questions about them: when is the next meeting, what did she report in the
wellbeing protocol, how have his IQCODE answers changed, which of my clients
mentioned falls, everything we hold about this person. A navigator asks about
their own clients; an admin about everyone.

Every read goes through ``ConvAI.client_records`` — the same functions the
client-record REST endpoints call — so the assistant and the API share one
permission rule, one privacy rule and one access log, and describe a record
identically (the tools hand the model the same JSON the endpoints return).

**Who is asking** comes from ``staff_user_id`` in the run config, which only
staff surfaces set: the chat bubble (``views.chat.send_chat_message``) and the
admin-only Test chat on the Agents page (``views.agents._test_chat_context``).
Never from ``user_id``: for a client's own conversation that is the *client's*
id, and a client whose id happened to match a navigator's would otherwise be
answered as that navigator. And never from a personal API token, which v1 puts
in the run config — LangGraph copies string config values into run metadata,
where tracing reads it.

Selected for the bubble by ``SiteConfiguration.link_worker_v2_enabled``; with
that off the bubble runs v1, unchanged. See link_worker_v2.md.
"""
from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

from . import register
from .link_worker import _schedule_meeting

logger = logging.getLogger(__name__)

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "link_worker_v2.prompt")
RECENT_MSG_LIMIT = 16
# A tool answer is read by the model, not a person. Past this it stops helping
# and starts crowding out the conversation; the model is told it was cut.
MAX_TOOL_CHARS = 12000

NOT_SIGNED_IN = ("This assistant only answers staff, and could not tell who is asking. "
                 "Ask them to open it from the chat bubble while signed in.")


def _load_system_prompt() -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def _configurable(config) -> dict:
    if isinstance(config, dict):
        return config.get("configurable", {}) or {}
    return getattr(config, "configurable", {}) or {}


def actor_from(cfg: dict):
    """The member of staff this run speaks for, or None.

    Only ``staff_user_id``, which the model cannot write (it lives in the run
    config, set by the server), and only an active navigator or admin.
    """
    from django.contrib.auth import get_user_model

    from ..roles import is_navigator

    pk = (cfg or {}).get("staff_user_id")
    if not pk:
        return None
    user = get_user_model().objects.filter(pk=pk).first()
    if user is None or not user.is_active or not is_navigator(user):
        return None
    return user


def as_tool_output(data) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + ' …[cut: ask a narrower question to see the rest]'
    return text


def _protocol_list() -> str:
    """The platform's protocols, for the system prompt.

    The prompt is built inside the event loop the native agents run in, where
    Django refuses database access outright (SynchronousOnlyOperation). So the
    one query runs in a worker thread, which closes its own connection — a
    thread that opened one and walked away would leak it.
    """
    from concurrent.futures import ThreadPoolExecutor

    def query():
        from django.db import connection

        from ..models import Protocol
        try:
            rows = [f"{p.number}. {p.title}" + (" (repeatable)" if p.repeatable else "")
                    for p in Protocol.objects.all()]
            return "; ".join(rows) or "none defined yet"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(query).result()


@register("link_worker_v2")
def build(checkpointer, model_name=None):
    """Build the Link Worker v2 react-agent graph."""
    try:
        from pydantic import BaseModel, Field
        from langchain_core.messages import AnyMessage
        from langchain_core.runnables import RunnableConfig
        from langchain_core.runnables.config import ensure_config
        from langchain_core.tools import tool
        from langgraph.prebuilt import create_react_agent
        from langgraph.prebuilt.chat_agent_executor import AgentState
        from ..llm_factory import make_llm
    except Exception as exc:  # missing LLM deps
        raise RuntimeError(
            "Link Worker v2 is not available (LangGraph/LLM dependencies missing)."
        ) from exc

    from .. import client_records as records

    def answer(fn, *args, **kwargs) -> str:
        """Run one records question as the signed-in member of staff."""
        user = actor_from(ensure_config().get("configurable", {}) or {})
        if user is None:
            return NOT_SIGNED_IN
        try:
            return as_tool_output(fn(user, *args, via="agent", **kwargs))
        except records.NotVisible:
            return ("NOT_VISIBLE — there is no client with that ID among the clients this "
                    "person can see. Use find_clients to get the right ID.")
        except records.BadQuestion as exc:  # a question to put back to the person
            return f"CANNOT_ANSWER — {exc}"
        except Exception:
            # Anything else is ours, not the question's: log it, and let the
            # model say so rather than failing the whole turn.
            logger.exception("Link Worker v2 tool %s failed", getattr(fn, "__name__", fn))
            return ("CANNOT_ANSWER — the record could not be read just now. Say so, and "
                    "suggest trying again or opening the client's page.")

    # Tools are synchronous on purpose: in this LangGraph version the run config
    # only reaches sync tools (ensure_config), and LangGraph runs them in a
    # worker thread, so the Django ORM is safe inside them. Same as v1.

    class FindInput(BaseModel):
        query: str = Field("", description="Name, surname, full name or phone. Empty lists "
                                            "every client this person can see.")

    @tool("find_clients", args_schema=FindInput)
    def find_clients(query: str = "") -> str:
        """Find clients by name or phone, or list them all. Gives each client's ID."""
        return answer(records.find_clients, query)

    class ClientInput(BaseModel):
        client_id: int = Field(..., description="The client's ID, from find_clients.")

    @tool("client_overview", args_schema=ClientInput)
    def client_overview(client_id: int) -> str:
        """Everything held about one client: contact, caregiver, details, next and recent
        meetings, protocol progress, open alerts, recent conversation summaries, notes."""
        return answer(records.overview, client_id)

    class MeetingsInput(BaseModel):
        client_id: Optional[int] = Field(None, description="One client's ID, or empty for "
                                                           "the whole caseload.")
        days: int = Field(30, description="How many days ahead to look (1-365).")

    @tool("upcoming_meetings", args_schema=MeetingsInput)
    def upcoming_meetings(client_id: Optional[int] = None, days: int = 30) -> str:
        """Meetings still to happen, soonest first, for one client or the whole caseload."""
        return answer(records.upcoming_meetings, patient_id=client_id, days=days)

    class AnswersInput(BaseModel):
        client_id: int = Field(..., description="The client's ID.")
        protocol: Optional[str] = Field(None, description="A protocol number or words from its "
                                                          "title. Empty for every protocol.")

    @tool("protocol_answers", args_schema=AnswersInput)
    def protocol_answers(client_id: int, protocol: Optional[str] = None) -> str:
        """What a client reported in their protocols: the latest answer to each question,
        with when it was given. Use for conditions, needs and anything else they said."""
        return answer(records.protocol_answers, client_id, protocol)

    class HistoryInput(BaseModel):
        client_id: int = Field(..., description="The client's ID.")
        protocol: str = Field(..., description="A protocol number or words from its title.")

    @tool("protocol_history", args_schema=HistoryInput)
    def protocol_history(client_id: int, protocol: str) -> str:
        """How one protocol's answers changed, call by call, oldest first. Use for
        "how is she doing", "has it improved", or any change over time."""
        return answer(records.protocol_history, client_id, protocol)

    class SearchInput(BaseModel):
        text: str = Field(..., description="A word or phrase, at least three characters.")

    @tool("search_records", args_schema=SearchInput)
    def search_records(text: str) -> str:
        """Find a word or phrase across every client this person can see: protocol answers,
        details, notes, meeting and conversation summaries, alerts. Use for "which of my
        clients mentioned …"."""
        return answer(records.search_records, text)

    class ScheduleInput(BaseModel):
        client_id: int = Field(..., description="The client's ID.")
        when: str = Field(..., description="Date and time, ISO 8601 preferred.")
        type: Optional[int] = Field(None, description="0 Onboarding, 1 Protocol (default), "
                                                      "2 Final, 3 Initial, 4 Follow-up.")
        protocols: Optional[List[int]] = Field(None, description="Protocol numbers to cover.")

    @tool("schedule_meeting", args_schema=ScheduleInput)
    def schedule_meeting(client_id: int, when: str, type: Optional[int] = None,
                         protocols: Optional[List[int]] = None) -> str:
        """Book a meeting for a client. Only after the person has confirmed the client and
        the time. The same checks as the calendar: own clients only, one hour apart."""
        user = actor_from(ensure_config().get("configurable", {}) or {})
        if user is None:
            return NOT_SIGNED_IN
        from dateutil import parser as dtparser
        try:
            scheduled = dtparser.parse(when)
        except Exception:
            return "CANNOT_ANSWER — that date/time could not be read; ask for e.g. 2026-10-15 15:30."
        return _schedule_meeting(user, client_id, scheduled, type, protocols)

    tools = [find_clients, client_overview, upcoming_meetings, protocol_answers,
             protocol_history, search_records, schedule_meeting]
    base_prompt = _load_system_prompt()

    def prompt(state: "AgentState", config: "RunnableConfig") -> List["AnyMessage"]:
        from django.conf import settings
        from django.utils import timezone

        cfg = _configurable(config) or (ensure_config().get("configurable", {}) or {})
        sys = base_prompt
        # Django's own time zone, which is what every time the tools return is
        # written in. (v1 reads DEFAULT_TIMEZONE, which can disagree with it.)
        sys += f"\n\nNow: {timezone.localtime():%A %Y-%m-%d %H:%M} ({settings.TIME_ZONE})."
        sys += f"\nProtocols on this platform: {_protocol_list()}."
        if cfg.get("user_name"):
            sys += f"\nYou are talking to {cfg['user_name']}."
        sys += ("\nThey are an admin: every client is visible to them." if cfg.get("is_admin")
                else "\nThey are a navigator: only their own clients are visible to them.")
        msgs = state["messages"][-RECENT_MSG_LIMIT:] if state.get("messages") else []
        return [{"role": "system", "content": sys}] + msgs

    # Lower than v1's 1.0: this agent reports what a record says, and a model
    # that paraphrases creatively is a model that misquotes a client.
    model = make_llm(model_name, temperature=0.2)

    return create_react_agent(
        model=model,
        tools=tools,
        state_modifier=prompt,
        checkpointer=checkpointer,
    )
