"""Client-requested conversation privacy.

Three additive columns and nothing else. ``Conversation.hidden`` defaults to
False, so every conversation that already exists stays exactly as readable as
it was, and ``conversation_privacy_enabled`` is blank — which the tri-state
resolver reads as "no override" — so an installation that does not set
CONVERSATION_PRIVACY_ENABLED sees no change at all.

See conversation_privacy.md.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ConvAI', '0086_conversation_download_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='hidden',
            field=models.BooleanField(
                db_index=True, default=False,
                help_text='Client asked that this conversation not be readable by their link worker.'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='hidden_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='When the client last asked for this conversation to be hidden.'),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='conversation_privacy_enabled',
            field=models.CharField(
                blank=True,
                choices=[('', 'Use .env default'), ('1', 'On'), ('0', 'Off')],
                default='', max_length=1),
        ),
    ]
