"""Link Worker native agent.

This is the agent behind the navigator chatbot *bubble*. It is a LangGraph
``create_react_agent`` that can search patients and schedule meetings for them.

It acts **as the signed-in user**: the bubble passes that user's Conversational
Care API token (``user_token``) in the run config when — and only when — the user
is a navigator or above. The tools authenticate the acting user from that token
(falling back to ``user_id``) and then apply the platform's usual per-object
permissions directly against the Django ORM:

    * navigators may list and schedule for **their own** clients only;
    * admins may list and schedule for **everybody**.

Tools run inside the async LangGraph graph, so all ORM access is marshalled
through ``sync_to_async``.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Annotated, List, Optional
from zoneinfo import ZoneInfo

from . import register

# ----------------------------
# Config
# ----------------------------
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "link_worker_agent.prompt")
RECENT_MSG_LIMIT = 10
DEFAULT_TZ = os.getenv("DEFAULT_TIMEZONE", "UTC")


def _load_system_prompt() -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def set_once(left, right):
    """Keep the first non-empty/non-None value across merges."""
    if left is not None and left != "":
        return left
    return right


# ----------------------------
# Acting user + permissioned ORM helpers (sync; wrapped with sync_to_async)
# ----------------------------
def _configurable(config) -> dict:
    """Pull the ``configurable`` dict out of a RunnableConfig (dict or object)."""
    if isinstance(config, dict):
        return config.get("configurable", {}) or {}
    return getattr(config, "configurable", {}) or {}


def _authenticate_actor(cfg: dict):
    """Resolve the acting user from their API token (preferred) or user_id.

    Returns the user only if they are a navigator or above; otherwise ``None``.
    """
    from rest_framework.authtoken.models import Token
    from django.contrib.auth import get_user_model
    from ..roles import is_navigator

    user = None
    token = (cfg or {}).get("user_token")
    if token:
        row = Token.objects.select_related("user").filter(key=token).first()
        user = row.user if row else None
    if user is None and (cfg or {}).get("user_id"):
        user = get_user_model().objects.filter(pk=(cfg or {}).get("user_id")).first()

    if not user or not user.is_active or not is_navigator(user):
        return None
    return user


def _search_patients(user, q: str = "") -> str:
    from ..models import Patient
    from ..roles import is_admin
    from django.db.models import Q, Value, TextField
    from django.db.models.functions import Concat

    qs = Patient.objects.all().order_by("lastname", "name")
    if not is_admin(user):  # navigators see only their own clients
        qs = qs.filter(navigator=user)

    # Normalise: collapse whitespace, treat commas as separators.
    q_norm = " ".join((q or "").replace(",", " ").split())
    if q_norm:
        tokens = q_norm.split(" ")
        base_q = (
            Q(name__icontains=q_norm)
            | Q(lastname__icontains=q_norm)
            | Q(phone_number__icontains=q_norm)
        )
        if len(tokens) >= 2:
            # Full-name search: match "First Last" (and "Last First") even though
            # name and lastname are stored separately. Mirrors the REST API.
            first, last = tokens[0], tokens[-1]
            qs = qs.annotate(
                full_name=Concat("name", Value(" "), "lastname", output_field=TextField())
            ).filter(
                base_q
                | Q(full_name__icontains=q_norm)
                | (Q(name__icontains=first) & Q(lastname__icontains=last))
                | (Q(name__icontains=last) & Q(lastname__icontains=first))
            )
        else:
            qs = qs.filter(base_q)

    items = list(qs[:20])
    if not items:
        return "No clients found." if q_norm else "You have no clients assigned yet."
    lines = [
        f"ID {p.id}: {p.name} {p.lastname} (phone: {p.phone_number or '—'})"
        for p in items
    ]
    header = (f"Found {len(items)} client(s):" if q_norm
              else f"Your clients ({len(items)}):")
    return header + "\n" + "\n".join(lines)


def _schedule_meeting(user, patient_id: int, scheduled_dt: datetime,
                      type: int | None, protocol_numbers: list[int] | None) -> str:
    from ..models import Patient, Meeting, Protocol
    from ..roles import is_admin
    from django.utils import timezone

    patient = Patient.objects.select_related("navigator").filter(pk=patient_id).first()
    if patient is None:
        return f"No patient found with ID {patient_id}."

    # Permission: admins may act on anyone; navigators only on their own clients.
    if not is_admin(user) and patient.navigator_id != user.id:
        return "You don't have permission to schedule for this client."

    if timezone.is_naive(scheduled_dt):
        scheduled_dt = timezone.make_aware(scheduled_dt, timezone.get_current_timezone())

    # Conflict window: ±59 minutes across the acting user's own clients.
    window_start = scheduled_dt - timedelta(minutes=59)
    window_end = scheduled_dt + timedelta(minutes=59)
    if Meeting.objects.filter(
        patient__navigator=user,
        scheduled_time__gte=window_start,
        scheduled_time__lt=window_end,
    ).exists():
        return ("There's already a meeting within ±59 minutes of that time. "
                "Please pick a slot at least an hour apart.")

    meeting = Meeting(patient=patient, scheduled_time=scheduled_dt)
    if type is not None:
        meeting.type = type
    meeting.save()

    # Only protocols that exist. The tool used to take a bare integer against a
    # hard-coded list of ten, so the agent could book a call against a protocol
    # nobody had created — and the call would then open on a panel with nothing
    # in it. Anything unrecognised is reported back rather than stored.
    booked, unknown = [], []
    for n in (protocol_numbers or []):
        protocol = Protocol.objects.filter(number=n).first()
        (booked if protocol else unknown).append(protocol or n)
    if booked:
        meeting.scheduled_protocols.set(booked)

    lines = [
        "MEETING_OK",
        f"id={meeting.id}",
        f"time={timezone.localtime(meeting.scheduled_time):%Y-%m-%d %H:%M}",
        f"patient={patient.name} {patient.lastname}",
        f"type={meeting.get_type_display()}",
        "protocols=" + (", ".join(f"{p.number}. {p.title}" for p in booked) or "—"),
    ]
    if unknown:
        lines.append("ignored_unknown_protocols=" + ", ".join(str(n) for n in unknown))
    return "\n".join(lines)


@register("link_worker")
def build(checkpointer, model_name=None):
    """Build the Link Worker react-agent graph."""
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
            "Link Worker agent is not available (LangGraph/LLM dependencies missing)."
        ) from exc

    class LinkWorkerState(AgentState):
        user_name: Annotated[Optional[str], set_once] = None
        user_id: Annotated[Optional[int], set_once] = None

    class SearchPatientsInput(BaseModel):
        q: str = Field(
            default="",
            description=("Name, last name, full name, or phone substring to search clients. "
                         "Leave EMPTY to list all clients visible to the user."),
        )

    # NOTE: these tools are intentionally *synchronous*. In this langgraph
    # version the run config only reaches SYNC tools (via ensure_config()) — an
    # async tool sees neither an injected `config` arg nor the ambient config.
    # langgraph runs sync tools in a worker thread during ainvoke(), so Django
    # ORM access here is safe (no running event loop in that thread).
    @tool("search_patients", args_schema=SearchPatientsInput)
    def search_patients_tool(q: str = "") -> str:
        """List or search clients visible to the signed-in user.

        Call with an empty query to list every client the user can see; pass a
        name/last name/full name/phone substring to filter. Returns id, name,
        lastname, phone.
        """
        cfg = ensure_config().get("configurable", {}) or {}
        user = _authenticate_actor(cfg)
        if user is None:
            return "You need to be signed in as a navigator or admin to look up clients."
        try:
            return _search_patients(user, q)
        except Exception as e:  # pragma: no cover - defensive
            return f"Search failed: {e}"

    class ScheduleMeetingInput(BaseModel):
        patient_id: int = Field(..., description="The selected client's ID.")
        when: str = Field(..., description="Date/time string. ISO 8601 preferred; natural text accepted.")
        type: int | None = Field(None, description="Meeting type (0 Onboarding, 1 Regular [default], 2 Final, 3 Initial).")
        scheduled_protocols: list[int] | None = Field(
            None,
            description=("Numbers of the protocols this call should cover. Use the numbers "
                         "of protocols that exist in the platform; unknown ones are ignored."),
        )

    @tool("schedule_meeting", args_schema=ScheduleMeetingInput)
    def schedule_meeting_tool(patient_id: int, when: str, type: int | None = None,
                              scheduled_protocols: list[int] | None = None) -> str:
        """Schedule a meeting for a client at the requested time (permissions apply)."""
        cfg = ensure_config().get("configurable", {}) or {}
        user = _authenticate_actor(cfg)
        if user is None:
            return "You need to be signed in as a navigator or admin to schedule meetings."
        from dateutil import parser as dtparser
        try:
            scheduled_dt = dtparser.parse(when)
        except Exception:
            return ("I couldn't parse that date/time. Please give a precise time, "
                    "e.g. 2026-07-15 15:30.")
        try:
            return _schedule_meeting(user, patient_id, scheduled_dt, type, scheduled_protocols)
        except Exception as e:  # pragma: no cover - defensive
            return f"Scheduling failed: {e}"

    tools = [search_patients_tool, schedule_meeting_tool]
    system_prompt = _load_system_prompt()

    def prompt(state: "LinkWorkerState", config: "RunnableConfig") -> List["AnyMessage"]:
        sys = system_prompt
        tz = ZoneInfo(DEFAULT_TZ)
        sys += f"\nCurrent datetime is {datetime.now(tz).isoformat()} (timezone: {DEFAULT_TZ})."

        # Prefer the passed config; fall back to the ambient run config.
        cfg = _configurable(config) or (ensure_config().get("configurable", {}) or {})
        user_name = state.get("user_name") or cfg.get("user_name")
        user_id = state.get("user_id") or cfg.get("user_id")
        if user_name:
            sys += f"\nUser's name is {user_name}. Address them politely by name."
        if user_id is not None:
            sys += f"\nThe viewer's user_id is {user_id}."
        if cfg.get("is_admin"):
            sys += "\nThis user is an admin and may act on any client."
        else:
            sys += "\nThis user is a navigator and may only act on their own clients."

        msgs = state["messages"][-RECENT_MSG_LIMIT:] if state.get("messages") else []
        return [{"role": "system", "content": sys}] + msgs

    # Model is admin-selectable via Agent.model (blank → platform default), and
    # routed to Azure/OpenAI/etc. by the shared factory.
    model = make_llm(model_name, temperature=1.0)

    # NOTE: this LangGraph (0.2.x) exposes the system-prompt hook as
    # ``state_modifier`` (the newer ``prompt`` kwarg doesn't exist yet). The
    # callable receives (state, config); config carries the user context.
    return create_react_agent(
        model=model,
        tools=tools,
        state_modifier=prompt,
        state_schema=LinkWorkerState,
        checkpointer=checkpointer,
    )
