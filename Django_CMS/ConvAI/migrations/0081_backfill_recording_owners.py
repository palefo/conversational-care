"""Give the recordings already on file an owner, where one can be established.

Everything placed from here on is written down as it happens — see CallLeg —
but the rows that predate that have only two phone numbers on them, and this is
the one chance to work out what they were before the answer gets any colder.

The rule is deliberately narrow. Where a number identifies exactly one client,
that client is written on the recording. Where it identifies two — a caregiver
who looks after one client and is themself another, which is the case that
started all of this — nothing is written, and the surfaces fall back to the
same number matching they always did. A guess recorded as a fact is worse than
no fact: the fallback is at least visibly ambiguous, whereas a wrong FK looks
settled.

The navigator's own leg is marked where it can be told apart with certainty —
a number that belongs to a member of staff and to no client at all. Those are
the recordings that were being drawn on a client's timeline while being a
recording of staff, and marking them is what takes them off it.
"""

from django.db import migrations

# Deliberately a literal rather than an import of views._panel.RECORDING_MATCH_WINDOW.
# A migration has to keep meaning what it meant on the day it ran; if that
# window is ever retuned, rows this touched must not silently re-describe
# themselves. 90 minutes either side, matching the fold at the time of writing.
MATCH_WINDOW_SECONDS = 5400


def _numbers_for(patient):
    nums = set()
    if patient.phone_number:
        nums.add(str(patient.phone_number))
    if patient.caregiver_id and patient.caregiver and patient.caregiver.phone_number:
        nums.add(str(patient.caregiver.phone_number))
    return nums


def backfill(apps, schema_editor):
    CallRecording = apps.get_model("ConvAI", "CallRecording")
    Patient = apps.get_model("ConvAI", "Patient")
    Meeting = apps.get_model("ConvAI", "Meeting")
    ConvAIUser = apps.get_model("ConvAI", "ConvAIUser")

    CANCELLED = 4        # Meeting.Status.CANCELLED
    LEG_CTN = 1          # CallRecording.Leg.CTN

    pending = list(
        CallRecording.objects
        .filter(patient__isnull=True)
        .order_by("start_time", "pk")
    )
    if not pending:
        return

    # Which clients answer to which number. A number with two clients behind it
    # is exactly the ambiguity this cannot resolve, so it is kept as a set and
    # only acted on when it holds one.
    clients_by_number = {}
    for patient in Patient.objects.select_related("caregiver"):
        for num in _numbers_for(patient):
            clients_by_number.setdefault(num, set()).add(patient.pk)

    staff_numbers = {
        str(num) for num in
        ConvAIUser.objects.exclude(phone_number__isnull=True)
                          .exclude(phone_number="")
                          .values_list("phone_number", flat=True)
        if num
    }

    # Meetings per client, with the anchor the fold uses. happened_at is a
    # property on the real model and properties do not survive into the
    # historical one, so it is spelled out here — and it must stay spelled the
    # same way: ended_at, else cancelled_at, else the booking.
    meetings_by_patient = {}
    for m in Meeting.objects.all().only(
        "id", "patient_id", "status", "ended_at", "cancelled_at", "scheduled_time"
    ):
        if m.status == CANCELLED:
            # Not somewhere a recording is guessed onto; see build_patient_events.
            continue
        anchor = m.ended_at or m.cancelled_at or m.scheduled_time
        if anchor is None:
            continue
        meetings_by_patient.setdefault(m.patient_id, []).append((m.id, anchor))

    # One recording per meeting. Three (to_number, start_time) groups on the
    # database this was written against hold more than one row — both legs of
    # one conference, or a repeated sync — and attaching two recordings to one
    # meeting would make the timeline claim two calls happened.
    taken = set(
        CallRecording.objects
        .filter(meeting__isnull=False)
        .values_list("meeting_id", flat=True)
    )

    filed = orphaned = staff = 0
    for rec in pending:
        to_num = str(rec.to_number or "")
        candidates = clients_by_number.get(to_num, set())

        if not candidates and to_num and to_num in staff_numbers:
            # A staff number that belongs to no client: the navigator's own leg
            # of a conference. Which conference is not knowable from here, so
            # only the side is written — enough to stop it being drawn as
            # somebody's call.
            rec.leg = LEG_CTN
            rec.save(update_fields=["leg"])
            staff += 1
            continue

        if len(candidates) != 1:
            orphaned += 1
            continue

        patient_id = next(iter(candidates))
        rec.patient_id = patient_id
        fields = ["patient"]

        near = sorted(
            (
                (abs((anchor - rec.start_time).total_seconds()), meeting_id)
                for meeting_id, anchor in meetings_by_patient.get(patient_id, ())
            ),
            key=lambda pair: (pair[0], pair[1]),
        )
        for gap, meeting_id in near:
            if gap > MATCH_WINDOW_SECONDS:
                break
            if meeting_id not in taken:
                rec.meeting_id = meeting_id
                taken.add(meeting_id)
                fields.append("meeting")
                break

        rec.save(update_fields=fields)
        filed += 1

    print(
        f"  call recordings: {filed} given a client, {staff} marked as the "
        f"navigator's own leg, {orphaned} left to number matching"
    )


def unbackfill(apps, schema_editor):
    """Deliberately does nothing.

    There is no record of which rows this filled in, so clearing them all would
    also clear anything written since by a call actually being placed — real
    facts destroyed to undo inferred ones. Reversing the migration before it
    drops the columns anyway.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0080_call_recording_attribution"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
