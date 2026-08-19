"""Self-registration native agent.

Greets people who message the service from an *unknown* WhatsApp number, collects
their first name, last name and phone number, and files a ``SelfRegistration`` row
(state ``REGISTERED``) for an admin to approve later.

Unlike the other native agents this one runs **without a Patient**: it is invoked
by ``utils._handle_self_registration_flow`` for inbound messages whose sender does
not match any patient/caregiver. The caller's phone number is passed through
``config["configurable"]["phone_number"]`` on every turn and surfaced to the model
as system context, so the number rarely has to be typed by hand.

Like ``protocol_qa`` it is an in-process ``create_react_agent`` with a single
**synchronous** tool (safe for Django ORM: LangGraph runs sync tools in a worker
thread during ``ainvoke``). Conversation state persists through the shared Postgres
checkpointer keyed by the self-registration ``thread_id``.
"""
from __future__ import annotations

import os
import re

from . import register

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "self_registration_agent.prompt")
RECENT_MSG_LIMIT = 20

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def _load_system_prompt() -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def _configurable(config) -> dict:
    """Pull the ``configurable`` dict out of a RunnableConfig (dict or object)."""
    if isinstance(config, dict):
        return config.get("configurable", {}) or {}
    return getattr(config, "configurable", {}) or {}


def normalize_phone(s: str) -> str:
    """Best-effort E.164 normalisation: keep a leading ``+`` and digits only."""
    s = (s or "").strip()
    if not s:
        return s
    if s[0] == "+":
        return "+" + re.sub(r"[^\d]", "", s[1:])
    return re.sub(r"[^\d]", "", s)


def valid_e164(phone: str) -> bool:
    return bool(_E164.match(phone or ""))


# ----------------------------
# ORM helper (sync; runs in LangGraph's worker thread)
# ----------------------------
def _create_self_registration(name: str, lastname: str, phone: str) -> dict:
    """Create (or reuse) a pending self-registration for this phone number."""
    from ..models import SelfRegistration

    name = (name or "").strip()
    lastname = (lastname or "").strip()
    phone = normalize_phone(phone)

    if not name or not lastname:
        return {"ok": False, "error": "Both a first name and a last name are required."}
    if not valid_e164(phone):
        return {"ok": False, "error": "The phone number must be valid E.164, e.g. +447700900123."}

    # Idempotency: if there's already a pending request for this number, don't
    # duplicate it — the person is likely re-confirming or messaging again.
    existing = SelfRegistration.objects.filter(
        phone_number=phone, state=SelfRegistration.State.REGISTERED
    ).first()
    if existing is not None:
        return {"ok": True, "already_registered": True, "id": existing.id}

    sr = SelfRegistration.objects.create(
        name=name,
        lastname=lastname,
        phone_number=phone,
        state=SelfRegistration.State.REGISTERED,
        details={"source": "self-registration-agent"},
    )
    return {"ok": True, "already_registered": False, "id": sr.id}


@register("self_registration")
def build(checkpointer, model_name=None):
    """Build the self-registration react-agent graph."""
    try:
        from langchain_core.messages import AnyMessage
        from langchain_core.runnables import RunnableConfig
        from langchain_core.runnables.config import ensure_config
        from langchain_core.tools import tool
        from langgraph.prebuilt import create_react_agent
        from langgraph.prebuilt.chat_agent_executor import AgentState
        from ..llm_factory import make_llm
    except Exception as exc:  # missing LLM deps
        raise RuntimeError(
            "Self-registration agent is not available (LangGraph/LLM dependencies missing)."
        ) from exc

    def _ctx() -> dict:
        return ensure_config().get("configurable", {}) or {}

    @tool("submit_self_registration")
    def submit_self_registration(name: str, lastname: str, phone_number: str) -> dict:
        """File the person's registration request for admin approval. Call this once,
        only after you have their first name, last name, and a valid E.164 phone
        number. If the phone number is already known from the system context, pass
        that value. Returns {ok, already_registered}."""
        try:
            return _create_self_registration(name, lastname, phone_number)
        except Exception as e:  # pragma: no cover - defensive
            return {"ok": False, "error": f"Could not submit registration: {e}"}

    tools = [submit_self_registration]
    system_prompt = _load_system_prompt()

    def prompt(state: "AgentState", config: "RunnableConfig") -> list["AnyMessage"]:
        cfg = _configurable(config) or _ctx()
        sys = system_prompt

        sys += "\n==========System info to use as required================"
        phone_number = cfg.get("phone_number")
        language = cfg.get("platform_language")
        brand = cfg.get("brand_name")
        if brand:
            sys += f"\nYou are registering people for {brand}."
        if language:
            sys += f"\nThe platform language is '{language}'. Start in this language."
        if phone_number:
            sys += (f"\nThe person is writing from WhatsApp number {phone_number}. "
                    "Use this as the default phone number to register — just confirm "
                    "it with them; do not ask them to type it unless they want a different one.")
        else:
            sys += "\nTheir WhatsApp number is not available, so you must ask for it."

        msgs = state["messages"][-RECENT_MSG_LIMIT:] if state.get("messages") else []
        return [{"role": "system", "content": sys}] + msgs

    model = make_llm(model_name, temperature=0.3)

    return create_react_agent(
        model=model,
        tools=tools,
        state_modifier=prompt,
        checkpointer=checkpointer,
    )
