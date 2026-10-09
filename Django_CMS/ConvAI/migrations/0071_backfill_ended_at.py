"""Give a past-tense meeting a time that has actually arrived.

Before ``ended_at`` existed, recording an outcome left the meeting dated by
``scheduled_time``. For a call recorded ahead of its slot — from a diary entry,
or because the caregiver was reached early — that put a finished call into
Happened under a date still in the future.

Only those are touched. A completed call whose scheduled time is in the past is
left alone: ``scheduled_time`` is a fair answer for when it happened, and
inventing a different one would be worse than the approximation already there.

The ones repaired get the moment this runs, which is not when the call happened
either — but it is the only thing we know to be true of them: it was recorded
before now.
"""

from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    Meeting = apps.get_model("ConvAI", "Meeting")
    now = timezone.now()
    Meeting.objects.exclude(status=0).filter(
        ended_at__isnull=True,
        cancelled_at__isnull=True,
        scheduled_time__gt=now,
    ).update(ended_at=now)


def backwards(apps, schema_editor):
    """Nothing to undo: the column goes with the previous migration."""


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0070_meeting_ended_at"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
