"""A protocol's questions and a meeting's answers, in the shape both the
navigator's live checklist and the voice interviewer read."""
from __future__ import annotations

import re

from django.utils.html import strip_tags


def plain(md: str) -> str:
    """Markdown to something that can be read aloud or shown on one line."""
    text = strip_tags(md or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # links
    text = re.sub(r"[*_`#>]+", "", text)                       # emphasis, headings, quotes
    text = re.sub(r"^\s*[-+]\s+", "• ", text, flags=re.M)      # bullets
    return re.sub(r"[ \t]+", " ", text).strip()


def questions_for(meeting, number: int):
    from ConvAI.models import Answer, Protocol

    protocol = Protocol.objects.filter(number=number).first()
    if protocol is None:
        return None
    answers = {a.question_id: a for a in Answer.objects.filter(
        meeting=meeting, question__protocol=protocol)}

    # Answers given on an earlier meeting carry forward on a one-off protocol,
    # exactly as the panel shows them: the interviewer confirms them rather
    # than asking again. A repeatable protocol asks afresh every time.
    carried = {}
    if not protocol.repeatable:
        for a in (Answer.objects
                  .filter(meeting__patient=meeting.patient, question__protocol=protocol)
                  .exclude(meeting=meeting)
                  .select_related("meeting")
                  .order_by("meeting__scheduled_time", "pk")):
            carried[a.question_id] = {"text": a.response,
                                      "when": a.meeting.happened_at.date().isoformat()}

    items = []
    for q in protocol.questions.all():
        ans = answers.get(q.id)
        items.append({
            "id": q.id,
            "order": q.order,
            "prompt_md": q.prompt_md,
            "prompt": plain(q.prompt_md),
            "answer": ans.response if ans else "",
            "source": ans.source if ans else "",
            "carried": None if ans else carried.get(q.id),
        })
    return {
        "meeting_id": meeting.pk,
        "protocol": {"number": protocol.number, "title": protocol.title,
                     "description": plain(protocol.description or ""),
                     "repeatable": protocol.repeatable},
        "questions": items,
        "answered": sum(1 for i in items if i["answer"]),
        "total": len(items),
    }
