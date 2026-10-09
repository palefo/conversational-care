from django.db import migrations


# IQCODE — Informant Questionnaire on Cognitive Decline in the Elderly.
# Answered by the informant (the caregiver), not the client: every item is
# "compared with 10 years ago", so the answer is a direction of travel rather
# than a score on the day. The 1-5 scale is the same on all of them, which is
# why it is repeated under each question instead of being stated once at the
# top — in the panel a question is a field you fill in on its own, and the
# scale is unreadable if it lives three screens up.
TITLE = "IQCODE — Cognitive decline (informant report)"

DESCRIPTION = (
    "Ask the caregiver to compare the person now with **how they were 10 years "
    "ago**, and record a number from 1 to 5 for each item.\n\n"
    "1 Much improved · 2 A bit improved · 3 Not much change · 4 A bit worse · "
    "5 Much worse"
)

SCALE = (
    "\n\n*1 Much improved · 2 A bit improved · 3 Not much change · "
    "4 A bit worse · 5 Much worse*"
)

ITEMS = [
    "Remembering things about family and friends e.g. occupations, birthdays, addresses",
    "Remembering things that have happened recently",
    "Recalling conversations a few days later",
    "Remembering his/her address and telephone number",
    "Remembering what day and month it is",
    "Remembering where things are usually kept",
    "Remembering where to find things which have been put in a different place from usual",
    "Knowing how to work familiar machines around the house",
]


def seed(apps, schema_editor):
    Protocol = apps.get_model("ConvAI", "Protocol")
    Question = apps.get_model("ConvAI", "Question")

    # Idempotent on the title: re-running must not leave two IQCODEs behind,
    # and the number is whatever was free when it first ran.
    protocol = Protocol.objects.filter(title=TITLE).first()
    if protocol is None:
        # Same rule the New protocol button uses (views/protocols.py), so a
        # protocol seeded here and one made by hand cannot collide on number.
        next_number = (
            Protocol.objects.order_by("-number")
            .values_list("number", flat=True)
            .first() or 0
        ) + 1
        protocol = Protocol.objects.create(
            number=next_number, title=TITLE, description=DESCRIPTION
        )
    else:
        protocol.description = DESCRIPTION
        protocol.save(update_fields=["description"])

    # Only seed the questions into an empty protocol. Answers lock a protocol
    # everywhere else in the app; wiping questions here would orphan them.
    if protocol.questions.exists():
        return

    for order, text in enumerate(ITEMS, start=1):
        Question.objects.create(
            protocol=protocol, order=order, prompt_md=f"{text}{SCALE}"
        )


def unseed(apps, schema_editor):
    Protocol = apps.get_model("ConvAI", "Protocol")
    Answer = apps.get_model("ConvAI", "Answer")

    protocol = Protocol.objects.filter(title=TITLE).first()
    if protocol is None:
        return
    # Never take answers down with it — reversing a migration is not consent
    # to delete what caregivers said.
    if Answer.objects.filter(question__protocol=protocol).exists():
        return
    protocol.delete()


class Migration(migrations.Migration):
    dependencies = [("ConvAI", "0074_email_support")]
    operations = [migrations.RunPython(seed, unseed)]
