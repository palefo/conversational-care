"""Carry the old single-blob meeting notes into Note rows.

``Meeting.notes`` held one overwritten blob per meeting. Those are real things
people wrote, so they become Note rows rather than being left behind when the
panel starts reading the new model.

The old field is deliberately left in place. Dropping it in the same release as
the move would make a rollback lossy; it can go once this has been running for
a while.

Author is left null: the old field never recorded who wrote it, and inventing an
author would be worse than admitting we do not know.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Meeting = apps.get_model("ConvAI", "Meeting")
    Note = apps.get_model("ConvAI", "Note")

    rows = []
    for meeting in Meeting.objects.exclude(notes="").exclude(notes=None).iterator():
        body = (meeting.notes or "").strip()
        if not body:
            continue
        rows.append(Note(meeting_id=meeting.pk, body=body))
    if rows:
        Note.objects.bulk_create(rows, batch_size=200)
        # created_at is auto_now_add, so the copies land with today's date. The
        # meeting's own timestamp is the closest honest answer we have.
        for meeting in Meeting.objects.exclude(notes="").exclude(notes=None).iterator():
            stamp = meeting.notes_updated_at or meeting.scheduled_time
            if stamp:
                Note.objects.filter(meeting_id=meeting.pk).update(
                    created_at=stamp, updated_at=stamp
                )


def backwards(apps, schema_editor):
    """The blob is still on Meeting, so undoing this only drops the copies."""
    Note = apps.get_model("ConvAI", "Note")
    Note.objects.filter(meeting__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0066_callrecording_transcript_moments_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
