# utils_conversation_classification.py

import json
import re
from typing import Iterable, Dict, Any, List, Tuple, Optional
from langchain.prompts import ChatPromptTemplate

DEFAULT_MODEL = "gpt-4.1"  # change if you prefer
TEMPERATURE = 0.2

# Defaults used when the Agent doesn't specify them
DEFAULT_ROLE = (
    "a clinical (textual) support triage assistant with healthcare/mental-health background"
)
DEFAULT_ABSTRACT = "≤80 words summary, in the conversation language."
CATEGORIES = (
    "[Informative - dementia, Informative - services, Wellbeing support, "
    "Burden of care, Requires medical attention, Emergency]"
)


def _detector_items(agent: Optional[Any]) -> List[Tuple[str, str]]:
    """
    Return list of (label, instruction) pairs from Agent.detectors.
    Agent.detectors is expected to be a JSON object: {label: instruction}.
    """
    if not agent:
        return []
    det = getattr(agent, "detectors", None)
    if isinstance(det, dict):
        # preserve insertion order
        return [(str(k), str(v or "").strip()) for k, v in det.items()]
    # tolerate legacy string lists if ever present
    if isinstance(det, str) and det.strip():
        return [(ln.strip(), "") for ln in det.splitlines() if ln.strip()]
    return []


def _agent_role(agent: Optional[Any]) -> str:
    """
    Prefer Agent.classification_role; fallback to DEFAULT_ROLE.
    """
    txt = (getattr(agent, "classification_role", "") or "").strip()
    return txt if txt else DEFAULT_ROLE


def _abstract_instruction(agent: Optional[Any]) -> str:
    """
    Prefer Agent.abstract_instruction; fallback to DEFAULT_ABSTRACT.
    """
    txt = (getattr(agent, "abstract_instruction", "") or "").strip()
    return txt if txt else DEFAULT_ABSTRACT


def _build_prompt(agent: Optional[Any], transcript: str):
    """
    Build a dynamic prompt using the Agent's role, abstract instruction, and detectors.
    """
    role = _agent_role(agent)
    abstract_line = _abstract_instruction(agent)
    det_pairs = _detector_items(agent)

    system_base = (
        f"You are {role}. "
        "Given a chat transcript between a patient/caregiver and a care agent, produce compact JSON with keys:\n"
        f"- abstract: {abstract_line}\n"
        f"- classification: pick ONE label (in conversation language) from {CATEGORIES}; if none fits, use Other.\n"
        "- important: true if a human must review following your previous instructions.\n"
    )

    if det_pairs:
        # Add detector schema and per-label guidance
        lines = []
        for label, instr in det_pairs:
            if instr:
                lines.append(f'- "{label}": {instr}')
            else:
                lines.append(f'- "{label}": (binary detector; set true only if clearly present)')
        system_base += (
            '\nAdditionally, include a key "detectors" as an object with EXACTLY these keys, '
            "each boolean (true/false). If unsure, default to false.\n"
            + "\n".join(lines)
            + "\n"
        )

    prompt_tmpl = ChatPromptTemplate.from_messages([
        ("system", system_base),
        ("user", "Transcript:\n{transcript}\n\nRespond with JSON only.")
    ])
    return prompt_tmpl.format_messages(transcript=transcript)


def _format_transcript(messages: Iterable[Dict[str, Any]]) -> str:
    """
    messages: iterable of dicts like:
      {"timestamp": dt, "from": "user"|"assistant", "text": "..."}
    """
    lines: List[str] = []
    for m in messages:
        who = "Paciente" if m.get("from") == "user" else "Agente"
        ts = m.get("timestamp")
        ts_s = ts.strftime("%Y-%m-%d %H:%M") if ts else ""
        txt = (m.get("text") or "").strip().replace("\n", " ").strip()
        if txt:
            lines.append(f"[{ts_s}] {who}: {txt}")
    # hard cap for safety
    return "\n".join(lines[:2000])


def classify_conversation_with_llm(
    messages: Iterable[Dict[str, Any]],
    agent: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Classify a conversation.
    Returns:
      {
        "abstract": str,
        "classification": str,
        "important": bool,
        "detectors": { <label>: bool, ... }  # only if Agent defines detectors
      }
    """
    transcript = _format_transcript(messages)
    # Route through the shared LLM factory so classification honours USE_AZURE and
    # the agent's configured model (blank → platform default), instead of always
    # calling OpenAI directly. Falls back to this module's DEFAULT_MODEL only when
    # neither the agent nor the platform default is set.
    from .llm_factory import make_llm, default_model
    model_name = (getattr(agent, "model", "") or "").strip() or default_model() or DEFAULT_MODEL
    llm = make_llm(model_name, temperature=TEMPERATURE)

    prompt = _build_prompt(agent, transcript)
    resp = llm.invoke(prompt)
    content = resp.content if hasattr(resp, "content") else str(resp)

    # Extract JSON payload
    m = re.search(r"\{.*\}", content, re.S)
    raw = m.group(0) if m else content
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    abstract = (data.get("abstract") or "")
    classification = (data.get("classification") or "")
    important = bool(data.get("important"))

    dets: Dict[str, bool] = {}
    det_obj = data.get("detectors")
    if isinstance(det_obj, dict):
        # normalize to bool
        dets = {str(k): bool(v) for k, v in det_obj.items()}

    return {
        "abstract": abstract,
        "classification": classification,
        "important": important,
        "detectors": dets,
    }


def build_message_rows_for_conv(conv_id: str, MessageModel) -> List[Dict[str, Any]]:
    """
    Utility: collect a normalized list of message rows for a conversation id.
    Each row is {"timestamp": dt, "from": "user"|"assistant", "text": "..."}.
    """
    qs = (
        MessageModel.objects
        .filter(conversation_id=conv_id)
        .order_by("timestamp")
        .values("timestamp", "user_message", "response_message")
    )
    rows: List[Dict[str, Any]] = []
    for r in qs:
        if r["user_message"]:
            rows.append({"timestamp": r["timestamp"], "from": "user", "text": r["user_message"]})
        if r["response_message"]:
            rows.append({"timestamp": r["timestamp"], "from": "assistant", "text": r["response_message"]})
    return rows