from django.db import migrations

# Meeting.Status.PENDING. Spelled out rather than imported: a migration has to
# keep meaning what it meant the day it ran, and the enum can move.
PENDING = 0


def backfill(apps, schema_editor):
    """Move the two integer columns into relations, then give every existing
    client a programme.

    Both halves are idempotent — re-running adds nothing new — so this is safe
    to replay if a deploy is repeated.
    """
    Meeting = apps.get_model("ConvAI", "Meeting")
    Patient = apps.get_model("ConvAI", "Patient")
    Protocol = apps.get_model("ConvAI", "Protocol")
    Answer = apps.get_model("ConvAI", "Answer")

    by_number = {p.number: p for p in Protocol.objects.all()}
    if not by_number:
        return

    # ── calls ─────────────────────────────────────────────────────────────
    # A number with no protocol behind it is dropped rather than invented: the
    # old list offered ten options and only some of them were ever real, so a
    # meeting can be pointing at a protocol that does not exist. There is
    # nothing to carry over in that case, and the column stays as it is.
    for meeting in Meeting.objects.exclude(
        scheduled_protocol=None, executed_protocol=None
    ).iterator():
        sched = by_number.get(meeting.scheduled_protocol)
        done = by_number.get(meeting.executed_protocol)
        if sched is not None:
            meeting.scheduled_protocols.add(sched)
        if done is not None:
            meeting.executed_protocols.add(done)

    # ── clients ───────────────────────────────────────────────────────────
    # What each client has actually answered, plus whatever is booked on a call
    # still to happen. Answered means it applies to them; booked means someone
    # has already decided it does. Anything else would either hide a protocol a
    # navigator is midway through or hand every client the full list again,
    # which is the state this whole change exists to end.
    programmes = {}

    for patient_id, protocol_id in (
        Answer.objects
        .values_list("meeting__patient_id", "question__protocol_id")
        .distinct()
    ):
        if patient_id and protocol_id:
            programmes.setdefault(patient_id, set()).add(protocol_id)

    for patient_id, protocol_id in (
        Meeting.objects
        .filter(status=PENDING)
        .values_list("patient_id", "scheduled_protocols__id")
        .distinct()
    ):
        if patient_id and protocol_id:
            programmes.setdefault(patient_id, set()).add(protocol_id)

    for patient in Patient.objects.filter(pk__in=programmes).iterator():
        patient.protocols.add(*programmes[patient.pk])


def unbackfill(apps, schema_editor):
    """Clear the relations. The integer columns were never emptied, so the
    calls keep the protocol they were carrying before this ran.

    Client programmes are dropped entirely: they did not exist before this
    migration, and there is no earlier state to restore them to.
    """
    Meeting = apps.get_model("ConvAI", "Meeting")
    Patient = apps.get_model("ConvAI", "Patient")

    for meeting in Meeting.objects.iterator():
        meeting.scheduled_protocols.clear()
        meeting.executed_protocols.clear()
    for patient in Patient.objects.iterator():
        patient.protocols.clear()


class Migration(migrations.Migration):
    dependencies = [("ConvAI", "0076_meeting_executed_protocols_and_more")]
    operations = [migrations.RunPython(backfill, unbackfill)]
