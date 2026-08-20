"""Retire the two places a note used to live before there was a Note table.

``0067`` and ``0068`` copied the old ``Meeting.notes`` blob and an alert's
``data['internal_note']`` into Note rows and deliberately left the originals in
place, so that release could be rolled back without losing what people had
written. Nothing reads or writes either of them any more — the panel and the
alert page both go through Note — so they come out here.

Two things happen first, because ``0068`` did not cover them:

* ``data['note_log']`` was a rolling list of the last 25 notes written on an
  alert. Nothing ever displayed it, and nothing copied it, so it is the one
  place on an alert holding notes that are not yet rows. Those are carried over
  before the key goes.
* ``internal_note_updated_at`` / ``internal_note_updated_by`` recorded when and
  by whom the blob was last written. They describe a field that no longer
  exists, and they were being shown verbatim in the panel's Technical tab.

**Reverting past this migration is lossy.** The columns come back empty, and
``0067``'s own reverse deletes the Note rows that replaced them. Everything
written as a note survives as a Note row on the way forward; there is no way
back to the old shape with the content intact.
"""

from django.conf import settings
from django.db import migrations

LEGACY_ALERT_KEYS = (
    "internal_note",
    "internal_note_updated_at",
    "internal_note_updated_by",
    "note_log",
)


def forwards(apps, schema_editor):
    Alert = apps.get_model("ConvAI", "Alert")
    Note = apps.get_model("ConvAI", "Note")
    User = apps.get_model(settings.AUTH_USER_MODEL)

    for alert in Alert.objects.iterator():
        data = alert.data
        if not isinstance(data, dict):
            continue
        if not any(k in data for k in LEGACY_ALERT_KEYS):
            continue

        # Whatever is already a row, so re-running or a partly-migrated alert
        # cannot produce the same note twice.
        existing = set(
            Note.objects.filter(alert_id=alert.pk).values_list("body", flat=True)
        )

        entries = data.get("note_log")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            body = (entry.get("note") or "").strip()
            if not body or body in existing:
                continue
            # The log recorded a username rather than a user. Matching it back
            # is best-effort: an author we cannot identify is left null, which
            # reads as "unknown" — better than attributing it to the wrong
            # person.
            author = User.objects.filter(username=entry.get("by") or "").first()
            note = Note.objects.create(alert_id=alert.pk, body=body, author=author)
            existing.add(body)
            when = entry.get("at")
            if when:
                # created_at is auto_now_add, so the copy lands with today's
                # date; the log's own timestamp is the honest one.
                Note.objects.filter(pk=note.pk).update(created_at=when, updated_at=when)

        for key in LEGACY_ALERT_KEYS:
            data.pop(key, None)
        alert.data = data
        alert.save(update_fields=["data"])


def backwards(apps, schema_editor):
    """Nothing to restore: the notes live on as rows, in one place only."""


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0071_backfill_ended_at"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="meeting", name="notes"),
        migrations.RemoveField(model_name="meeting", name="notes_updated_at"),
    ]
