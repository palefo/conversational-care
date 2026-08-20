"""Protocol QA native agent.

Collects a meeting's protocol answers conversationally (over WhatsApp) and saves
them to ``Answer`` rows. It is assigned to a patient by the *automation* flow
([`start_protocol_automation`](../views/protocols.py)); when it finishes — or the
conversation goes idle for 3h — the patient is reverted to their previous agent.

Like ``link_worker`` this is an in-process ``create_react_agent`` whose tools are
**synchronous** (so Django ORM access is safe: LangGraph runs sync tools in a
worker thread during ``ainvoke``). Run context (meeting_id, protocol_id,
patient_id, names, phone) is passed through ``config["configurable"]`` on every
turn by the automation lifecycle in ``utils.process_message_for_patient`` and read
here via ``ensure_config()``.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone as dt_timezone
from typing import Annotated, List, Optional

from . import register

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "protocol_qa_agent.prompt")
RECENT_MSG_LIMIT = 20
DEFAULT_TZ = os.getenv("DEFAULT_TIMEZONE", "Europe/London")


def _load_system_prompt() -> str:
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def set_once(left, right):
    """Keep the first non-empty/non-None value across merges."""
    if left is not None and left != "":
        return left
    return right


def _configurable(config) -> dict:
    """Pull the ``configurable`` dict out of a RunnableConfig (dict or object)."""
    if isinstance(config, dict):
        return config.get("configurable", {}) or {}
    return getattr(config, "configurable", {}) or {}


# ----------------------------
# ORM helpers (sync; run in LangGraph's worker thread)
# ----------------------------
def _fetch_questions(protocol_num: int, meeting_id: int) -> dict:
    from ..models import Protocol, Answer

    protocol = Protocol.objects.filter(number=protocol_num).first()
    if protocol is None:
        return {"error": f"No protocol found with number {protocol_num}."}

    answers = {
        a.question_id: a.response
        for a in Answer.objects.filter(
            meeting_id=meeting_id, question__protocol=protocol
        )
    }
    questions = [
        {
            "id": q.id,
            "order": q.order,
            "prompt_md": q.prompt_md,
            "response": answers.get(q.id, ""),
        }
        for q in protocol.questions.all()
    ]
    return {
        "meeting_id": meeting_id,
        "protocol": {"number": protocol.number, "title": protocol.title,
                     "description": protocol.description},
        "questions": questions,
    }


def _save_answer(protocol_num: int, meeting_id: int, question_id: int, response: str) -> dict:
    """Upsert a single answer; an empty response deletes it. Mirrors
    ``ProtocolAnswerForm.save`` semantics."""
    from ..models import Answer, Question

    question = Question.objects.filter(
        id=question_id, protocol__number=protocol_num
    ).first()
    if question is None:
        valid_ids = list(
            Question.objects.filter(protocol__number=protocol_num)
            .order_by("order").values_list("id", flat=True)
        )
        return {
            "ok": False,
            "error": (
                f"question_id {question_id} does not exist in protocol {protocol_num}. "
                f"This protocol has exactly these question_ids: {valid_ids}. "
                "Do not invent questions — only save answers against these ids, "
                "and fold any extra information the caregiver volunteers into the "
                "notes of the most relevant existing question."
            ),
            "valid_question_ids": valid_ids,
        }

    response = (response or "").strip()
    ans = Answer.objects.filter(meeting_id=meeting_id, question_id=question_id).first()

    if response:
        # Written by the automation, so it is marked as having come back by text.
        # A navigator editing it afterwards clears the mark — see protocol_view.
        if ans:
            ans.response = response
            ans.by_text = True
            ans.save(update_fields=["response", "by_text"])
        else:
            Answer.objects.create(
                meeting_id=meeting_id, question_id=question_id,
                response=response, by_text=True,
            )
        return {"ok": True, "question_id": question_id, "saved": True}

    # Empty response clears any stored answer.
    if ans:
        ans.delete()
    return {"ok": True, "question_id": question_id, "saved": False, "cleared": True}


def _finish_automation(patient_id: int) -> dict:
    from ..models import Patient
    from ..utils import end_automation

    patient = Patient.objects.filter(pk=patient_id).first()
    if patient is None:
        return {"ok": False, "error": "Patient not found."}
    end_automation(patient)
    return {"ok": True, "finished": True}


@register("protocol_qa")
def build(checkpointer, model_name=None):
    """Build the Protocol QA react-agent graph."""
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
            "Protocol QA agent is not available (LangGraph/LLM dependencies missing)."
        ) from exc

    class QAState(AgentState):
        meeting_id: Annotated[Optional[int], set_once] = None
        protocol_id: Annotated[Optional[int], set_once] = None
        patient_id: Annotated[Optional[int], set_once] = None

    def _ctx() -> dict:
        return ensure_config().get("configurable", {}) or {}

    @tool("get_protocol_questions")
    def get_protocol_questions() -> dict:
        """Fetch the active protocol's questions and any answers already saved for
        this meeting. Returns {meeting_id, protocol, questions:[{id, order,
        prompt_md, response}]}. Call this before asking anything."""
        cfg = _ctx()
        protocol_num, meeting_id = cfg.get("protocol_id"), cfg.get("meeting_id")
        if protocol_num is None or meeting_id is None:
            return {"error": "No active protocol/meeting in this conversation."}
        try:
            return _fetch_questions(protocol_num, meeting_id)
        except Exception as e:  # pragma: no cover - defensive
            return {"error": f"Could not load questions: {e}"}

    @tool("save_protocol_answer")
    def save_protocol_answer(question_id: int, response: str) -> dict:
        """Save (upsert) the answer to one question for the active meeting. Write
        the response as concise third-person notes for a later specialist. An
        empty response clears a previously saved answer."""
        cfg = _ctx()
        protocol_num, meeting_id = cfg.get("protocol_id"), cfg.get("meeting_id")
        if protocol_num is None or meeting_id is None:
            return {"ok": False, "error": "No active protocol/meeting in this conversation."}
        try:
            return _save_answer(protocol_num, meeting_id, question_id, response)
        except Exception as e:  # pragma: no cover - defensive
            return {"ok": False, "error": f"Could not save answer: {e}"}

    @tool("get_current_date")
    def get_current_date() -> str:
        """Get the current date, time, day of week, and timezone in a readable format."""
        now = datetime.now(dt_timezone.utc).astimezone()
        return now.strftime("%A, %Y-%m-%d %H:%M:%S %Z%z")

    @tool("finish_protocol_automation")
    def finish_protocol_automation() -> dict:
        """End this automation once the protocol is complete or the caregiver wants
        to stop. Reverts the client to their previous agent. Call it last, after
        thanking them."""
        cfg = _ctx()
        patient_id = cfg.get("patient_id")
        if patient_id is None:
            return {"ok": False, "error": "No patient in this conversation."}
        try:
            return _finish_automation(patient_id)
        except Exception as e:  # pragma: no cover - defensive
            return {"ok": False, "error": f"Could not finish automation: {e}"}

    tools = [get_protocol_questions, save_protocol_answer,
             get_current_date, finish_protocol_automation]
    system_prompt = _load_system_prompt()

    def prompt(state: "QAState", config: "RunnableConfig") -> List["AnyMessage"]:
        cfg = _configurable(config) or _ctx()
        sys = system_prompt

        sys += "\n==========System info to use as required================"
        protocol_id = state.get("protocol_id") or cfg.get("protocol_id")
        meeting_id = state.get("meeting_id") or cfg.get("meeting_id")
        patient_id = state.get("patient_id") or cfg.get("patient_id")
        user_name = cfg.get("user_name")
        caregiver_name = cfg.get("caregiver_name")
        phone_number = cfg.get("phone_number")

        if protocol_id is not None:
            sys += f"\nActive protocol_id: {protocol_id}."
        if meeting_id is not None:
            sys += f"\nActive meeting_id (main identifier for this process): {meeting_id}."
        if patient_id is not None:
            sys += f"\npatient_id: {patient_id}."
        if user_name:
            sys += f"\nThe client/patient name is {user_name}."
        if caregiver_name:
            sys += (f"\nThe study partner/carer/caregiver is {caregiver_name}. "
                    "They are the person you are speaking to on WhatsApp.")
        if phone_number:
            sys += f"\nThe caregiver phone number is {phone_number}."
        sys += (f"\nThe timezone is {DEFAULT_TZ}. Use get_current_date for the current date/time.")

        msgs = state["messages"][-RECENT_MSG_LIMIT:] if state.get("messages") else []
        return [{"role": "system", "content": sys}] + msgs

    model = make_llm(model_name, temperature=1.0)

    # LangGraph 0.2.x: the system-prompt hook is ``state_modifier`` (receives
    # (state, config); config carries the run context).
    return create_react_agent(
        model=model,
        tools=tools,
        state_modifier=prompt,
        state_schema=QAState,
        checkpointer=checkpointer,
    )
