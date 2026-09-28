"""Self-registration native agent.

Greets people who message the service from an *unknown* WhatsApp or SMS number, collects
their first name, last name and phone number, and files a ``SelfRegistration`` row
(state ``REGISTERED``) for an admin to approve later.

Unlike the other native agents this one runs **without a Patient**: it is invoked
by ``utils._handle_self_registration_flow`` for inbound messages whose sender does
not match any patient/caregiver. The caller's phone number is passed through
``config["configurable"]["phone_number"]`` on every turn and surfaced to the model
as system context, so the number rarely has to be typed by hand. So is the
``channel`` it arrived on (``"whatsapp"`` or ``"sms"``), which is also kept on the
registration so whoever approves it knows how the person wrote in.

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
CHANNEL_NAMES = {"whatsapp": "WhatsApp", "sms": "SMS"}


def _create_self_registration(name: str, lastname: str, phone: str, channel: str | None = None) -> dict:
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
        details={"source": "self-registration-agent",
                 **({"channel": channel} if channel in CHANNEL_NAMES else {})},
    )
    return {"ok": True, "already_registered": False, "id": sr.id}


def _enrolment_enabled() -> bool:
    """Whether this installation runs studies (see participant_management.md)."""
    from ..site_config import get_bool
    return get_bool("STUDY_ENROLMENT_ENABLED")


def _link_access_code(code: str, phone: str) -> dict:
    """Match a study access code and remember the number it came from.

    A match means a clinician already enrolled this person, so there is nothing
    to file for approval. It stops short of admitting them: a code sent over
    WhatsApp is not consent, and the join page is where consent is taken.
    """
    from ..enrolment import link_phone_to_enrolment

    return link_phone_to_enrolment(code, phone)


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
            return _create_self_registration(name, lastname, phone_number,
                                             channel=_ctx().get("channel"))
        except Exception as e:  # pragma: no cover - defensive
            return {"ok": False, "error": f"Could not submit registration: {e}"}

    @tool("check_access_code")
    def check_access_code(code: str) -> dict:
        """Check an access code the person says they were given for a study.

        Only useful where the service runs a study. Returns {ok, name, study,
        needs_consent} when the code matches somebody who has not claimed it yet,
        and {ok: False} otherwise. A match means they are already enrolled, so do
        NOT also submit a registration request for them. {ok: False, reason:
        "locked"} means too many wrong codes from this number: stop asking for
        one and tell them to contact the person who gave it to them."""
        try:
            return _link_access_code(code, _ctx().get("phone_number") or "")
        except Exception as e:  # pragma: no cover - defensive
            return {"ok": False, "error": f"Could not check that code: {e}"}

    # The code tool is only offered where a study is actually being run. An agent
    # that cannot do anything useful with a code should not be asking for one.
    tools = [submit_self_registration]
    if _enrolment_enabled():
        tools.append(check_access_code)
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
        # Where a study is running, an access code short-circuits the whole
        # registration: the person is already enrolled and only needs to consent.
        join_url = cfg.get("join_url")
        if join_url:
            sys += (
                "\nThis service also runs a research study. Some people writing in "
                "have already been enrolled by a clinician and given a three-word "
                "access code (like 'maple-crane-frost'). Early on, ask once whether "
                "they were given such a code. If they give you one, call "
                "`check_access_code`. If it matches, greet them by the name it "
                "returns, tell them they are already enrolled, and send them to "
                f"{join_url} to read the information sheet and give consent — then "
                "stop; do NOT collect their details or submit a registration. "
                "If it does not match, or they have no code, carry on with the "
                "normal registration below without dwelling on it."
            )

        channel = CHANNEL_NAMES.get(cfg.get("channel"), "WhatsApp")
        sys += f"\nThe person is writing to the service by {channel}."
        if channel == "SMS":
            sys += (" Keep every reply to one or two short sentences: each text message "
                    "costs them, and long ones arrive split into pieces.")
        if phone_number:
            sys += (f"\nThey are writing from {channel} number {phone_number}. "
                    "Use this as the default phone number to register — just confirm "
                    "it with them; do not ask them to type it unless they want a different one.")
        else:
            sys += f"\nTheir {channel} number is not available, so you must ask for it."

        msgs = state["messages"][-RECENT_MSG_LIMIT:] if state.get("messages") else []
        return [{"role": "system", "content": sys}] + msgs

    model = make_llm(model_name, temperature=0.3)

    return create_react_agent(
        model=model,
        tools=tools,
        state_modifier=prompt,
        checkpointer=checkpointer,
    )
