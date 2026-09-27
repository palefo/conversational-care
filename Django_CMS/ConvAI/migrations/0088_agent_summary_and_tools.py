"""The agent's own summary, a person's own summary, and per-agent tools.

Additive, except for one backfill that has to move text rather than add it.

Until now a navigator correcting a conversation summary **overwrote
Conversation.summary in place** and recorded a SummaryEdit alongside it to say
whose words they were. The generated original was gone at that point. From here
the generated summary is read-only and a person's goes in SummaryEdit.body as a
block of its own — so every conversation with a SummaryEdit is holding a
person's words in the machine's field, and would print them under a "Generated
automatically" byline, which is exactly the false claim SummaryEdit exists to
prevent.

Those rows are therefore **moved**, not copied: the text goes to ``body`` and
``summary`` is cleared. Copying would print the same paragraph twice under two
different bylines; leaving it would keep attributing a person's writing to a
model. Clearing loses nothing that still existed — the model's original was
overwritten the day they edited it.

Reversible, and the reverse is the same move backwards.
"""
from django.db import migrations, models


def human_summaries_to_body(apps, schema_editor):
    SummaryEdit = apps.get_model("ConvAI", "SummaryEdit")
    edits = (SummaryEdit.objects
             .filter(conversation__isnull=False)
             .exclude(body__gt="")
             .select_related("conversation"))
    for edit in edits:
        conv = edit.conversation
        text = (conv.summary or "").strip()
        if not text:
            continue
        edit.body = text
        edit.save(update_fields=["body"])
        conv.summary = ""
        conv.save(update_fields=["summary"])


def body_to_human_summaries(apps, schema_editor):
    SummaryEdit = apps.get_model("ConvAI", "SummaryEdit")
    edits = (SummaryEdit.objects
             .filter(conversation__isnull=False, body__gt="")
             .select_related("conversation"))
    for edit in edits:
        conv = edit.conversation
        # Only back into an empty field: a summary written since this migration
        # ran belongs to whoever wrote it, and is not ours to overwrite.
        if (conv.summary or "").strip():
            continue
        conv.summary = edit.body
        conv.save(update_fields=["summary"])
        edit.body = ""
        edit.save(update_fields=["body"])


class Migration(migrations.Migration):

    dependencies = [
        ('ConvAI', '0087_conversation_privacy'),
    ]

    operations = [
        migrations.AddField(
            model_name='agent',
            name='tools',
            field=models.JSONField(blank=True, default=dict, help_text='Prompt-based agents only: platform tools this agent may call, keyed by tool slug, with an optional prompt override.'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='agent_summary',
            field=models.TextField(blank=True, default='', help_text='Summary reported by the agent that held this conversation.'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='agent_summary_at',
            field=models.DateTimeField(blank=True, help_text='When the agent last reported a summary for this conversation.', null=True),
        ),
        migrations.AddField(
            model_name='summaryedit',
            name='body',
            field=models.TextField(blank=True, default='', help_text="The person's own summary, kept alongside the generated one. Used for conversations; blank for the kinds whose overview is edited in place."),
        ),
        migrations.RunPython(human_summaries_to_body, body_to_human_summaries),
    ]
