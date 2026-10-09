"""Carry an alert's internal note out of its JSON blob and into a Note row.

``alert.data['internal_note']`` was a single string with no author and no time,
and it surfaced as a line in the alert's summary rather than as a note. Now that
notes are records, it becomes one, and the alert's Notes tab shows it beside
anything written since.

The key is left in ``data`` for one release, for the same reason the old
``Meeting.notes`` field is: a rollback should not lose what someone wrote.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Alert = apps.get_model("ConvAI", "Alert")
    Note = apps.get_model("ConvAI", "Note")

    for alert in Alert.objects.iterator():
        body = ((alert.data or {}).get("internal_note") or "").strip()
        if not body:
            continue
        if Note.objects.filter(alert_id=alert.pk).exists():
            continue
        note = Note.objects.create(alert_id=alert.pk, body=body)
        # created_at is auto_now_add; the alert's own time is the closest
        # honest answer, since the blob never recorded when it was written.
        Note.objects.filter(pk=note.pk).update(
            created_at=alert.created_at, updated_at=alert.created_at
        )


def backwards(apps, schema_editor):
    """The blob is still in data, so undoing only drops the copies."""
    Note = apps.get_model("ConvAI", "Note")
    Note.objects.filter(alert__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0067_meeting_notes_to_note_rows"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
