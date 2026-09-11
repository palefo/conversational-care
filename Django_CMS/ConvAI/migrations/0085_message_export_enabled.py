"""Message export: the switch behind Settings -> Export.

Additive and off by default — ``message_export_enabled`` is blank, which the
tri-state resolver reads as "no override", so an installation that does not set
MESSAGE_EXPORT_ENABLED sees nothing change. See message_export.md.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ConvAI', '0084_sensei_agents'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfiguration',
            name='message_export_enabled',
            field=models.CharField(blank=True, choices=[('', 'Use .env default'), ('1', 'On'), ('0', 'Off')], default='', max_length=1),
        ),
    ]
