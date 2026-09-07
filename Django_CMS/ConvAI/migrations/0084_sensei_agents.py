"""Sensei agents: the new Agent kind and the settings behind it.

Additive and off by default — ``sensei_enabled`` is blank, which the tri-state
resolver reads as "no override", so an installation that does not use Sensei
sees nothing change. See agents.md -> "Sensei agents".
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ConvAI', '0083_meeting_dial_target'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfiguration',
            name='sensei_api_url',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='sensei_enabled',
            field=models.CharField(blank=True, choices=[('', 'Use .env default'), ('1', 'On'), ('0', 'Off')], default='', max_length=1),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='sensei_function_key',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='sensei_user_id_secret',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AlterField(
            model_name='agent',
            name='kind',
            field=models.CharField(choices=[('remote', 'Remote'), ('native', 'Native'), ('prompt', 'Prompt-based'), ('sensei', 'Sensei')], db_index=True, default='remote', help_text='Remote = runs on a LangGraph server (host:port). Native = ships with the platform. Prompt-based = in-process agent driven by a stored prompt. Sensei = forwards the turn to the external Sensei service.', max_length=16),
        ),
    ]
