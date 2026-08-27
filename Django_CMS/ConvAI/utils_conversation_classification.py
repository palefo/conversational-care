# utils_conversation_classification.py

import json
import re
from typing import Iterable, Dict, Any, List, NamedTuple, Optional
from django.utils.translation import gettext_lazy as _
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

# Priority values, mirroring Alert.Priority. Kept as plain ints so this module
# stays importable without dragging in the model layer.
PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW = 1, 2, 3


class Detector(NamedTuple):
    """One thing an agent watches for, and what happens when it finds it.

    ``raises`` is the difference between a detector that only tints the review
    panel — which is all any of them did before — and one that puts a row in
    somebody's queue.
    """
    label: str
    instruction: str
    raises: bool
    priority: int


# The one detector an admin cannot switch off. Every other rule here is theirs
# to write, which is right for missed medication and wrong for this: a platform
# talking to caregivers in distress cannot let "nobody filled in that JSON
# field" be the thing standing between a disclosure and a navigator seeing it.
SAFETY_LABEL = "Self-harm"
SAFETY_DETECTOR = Detector(
    label=SAFETY_LABEL,
    instruction=(
        "The person expresses wanting to die, wanting to hurt themselves, not "
        "wanting to go on, or a plan to end their life — stated plainly or "
        "hinted at. Set true on any such expression, including a single line "
        "with nothing after it. When in doubt, set true."
    ),
    raises=True,
    priority=PRIORITY_HIGH,
)


# Built-in labels are stored in English and read back in English: the stored
# string is the key that dedupe, the agent's own config and the audit trail all
# match on, and a key that moved with whoever was logged in would quietly stop
# matching itself. Only the reading of it is translated.
#
# Detectors an admin wrote are their own words in their own language, so they
# pass through untouched — there is nothing here to translate them against.
BUILTIN_LABELS = {SAFETY_LABEL: _("Self-harm")}


def display_label(label: str) -> str:
    """How a detector label should read to whoever is looking at it."""
    return BUILTIN_LABELS.get(label, label)


def normalize_detectors(raw: Any) -> Dict[str, Detector]:
    """Read ``Agent.detectors`` in either shape it has ever been stored in.

    The original shape was ``{label: "instruction"}`` and drove nothing but a
    checkbox in the review panel, so those rows keep that meaning: detect, do
    not raise. The current shape is
    ``{label: {"instruction": ..., "raises": bool, "priority": int}}``.
    """
    out: Dict[str, Detector] = {}
    if isinstance(raw, str) and raw.strip():
        # Tolerate the legacy newline-separated list of bare labels.
        raw = {ln.strip(): "" for ln in raw.splitlines() if ln.strip()}
    if not isinstance(raw, dict):
        return out

    for label, spec in raw.items():
        label = str(label).strip()
        if not label:
            continue
        if isinstance(spec, dict):
            instruction = str(spec.get("instruction") or "").strip()
            raises = bool(spec.get("raises"))
            try:
                priority = int(spec.get("priority") or PRIORITY_MEDIUM)
            except (TypeError, ValueError):
                priority = PRIORITY_MEDIUM
            if priority not in (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW):
                priority = PRIORITY_MEDIUM
        else:
            instruction, raises, priority = str(spec or "").strip(), False, PRIORITY_MEDIUM
        out[label] = Detector(label, instruction, raises, priority)
    return out


def detectors_for(agent: Optional[Any]) -> Dict[str, Detector]:
    """Every detector that applies to ``agent``, safety floor included.

    The agent's own rows win on the label, so an admin who writes their own
    self-harm wording gets it — what they cannot do is take the label away.
    """
    dets = {SAFETY_LABEL: SAFETY_DETECTOR}
    dets.update(normalize_detectors(getattr(agent, "detectors", None) if agent else None))
    # Whatever an admin wrote for the safety label, it still raises, and it
    # still raises high.
    if SAFETY_LABEL in dets:
        own = dets[SAFETY_LABEL]
        dets[SAFETY_LABEL] = own._replace(
            instruction=own.instruction or SAFETY_DETECTOR.instruction,
            raises=True,
            priority=PRIORITY_HIGH,
        )
    return dets


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
    dets = detectors_for(agent)

    system_base = (
        f"You are {role}. "
        "Given a chat transcript between a patient/caregiver and a care agent, produce compact JSON with keys:\n"
        f"- abstract: {abstract_line}\n"
        f"- classification: pick ONE label (in conversation language) from {CATEGORIES}; if none fits, use Other.\n"
        "- important: true if a human must review following your previous instructions.\n"
        # "The conversation language" is asked for in three places above and
        # below, and left to inference it reads whatever looks most like a
        # language — including the "Patient:"/"Agent:" labels the transcript
        # builder puts on every line, which are ours and not anyone's speech.
        # Pinning it to what the two of them actually wrote is what stops a
        # formatting choice from deciding what language a navigator is answered
        # in. See _format_transcript.
        "\nLanguage: wherever an instruction above or below says \"the conversation "
        "language\", that means the language the patient/caregiver and the agent "
        "actually wrote in. Judge it only from the words they said. The speaker "
        "labels and timestamps in the transcript are formatting added by this "
        "system — ignore them entirely when deciding the language.\n"
    )

    if dets:
        lines = []
        for d in dets.values():
            lines.append(f'- "{d.label}": {d.instruction}' if d.instruction
                         else f'- "{d.label}": (binary detector; set true only if clearly present)')
        system_base += (
            '\nAdditionally, include a key "detectors" as an object with EXACTLY these keys, '
            "each boolean (true/false). If unsure, default to false.\n"
            + "\n".join(lines)
            + "\n"
            # The abstract describes the exchange; this describes why somebody
            # is being interrupted. A navigator opening the alert reads it
            # first, so it has to name the moment rather than the theme.
            + '\nAlso include a key "triggers": an object keyed by ONLY the detector '
            "labels you set to true. Each value is ONE sentence, in the conversation "
            "language, saying what in this exchange set that detector off — quote the "
            "words the person used where you can. Omit labels you set to false.\n"
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
        # English, and deliberately so. The prompt asks for the abstract and the
        # triggers "in the conversation language", and these labels are on every
        # single line of what it reads — so a Spanish "Paciente:"/"Agente:" was
        # the loudest language signal in the input and got answered as one. An
        # all-English exchange came back summarised in Spanish with the person's
        # own words quoted inside it. These are scaffolding the code adds, not
        # anything either party said, so they must not carry a language of their
        # own; the transcript's language should be whatever was actually spoken.
        who = "Patient" if m.get("from") == "user" else "Agent"
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
        "detectors": { <label>: bool, ... },
        "triggers":  { <label>: str, ... }   # only for detectors that fired
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

    # One sentence per fired detector, saying what set it off. Kept only for
    # labels actually set true, so a model that answers for every label cannot
    # put an explanation on an alert that was never raised.
    trigs: Dict[str, str] = {}
    trig_obj = data.get("triggers")
    if isinstance(trig_obj, dict):
        trigs = {str(k): str(v).strip() for k, v in trig_obj.items()
                 if dets.get(str(k)) and str(v or "").strip()}

    return {
        "abstract": abstract,
        "classification": classification,
        "important": important,
        "detectors": dets,
        "triggers": trigs,
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