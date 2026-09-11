"""Conversation downloads: the second switch behind Settings -> Export.

Additive and off by default — ``conversation_download_enabled`` is blank, which
the tri-state resolver reads as "no override", so an installation that does not
set CONVERSATION_DOWNLOAD_ENABLED sees nothing change. See message_export.md.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ConvAI', '0085_message_export_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfiguration',
            name='conversation_download_enabled',
            field=models.CharField(blank=True, choices=[('', 'Use .env default'), ('1', 'On'), ('0', 'Off')], default='', max_length=1),
        ),
    ]
