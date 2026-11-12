# Shared Twilio numbers: the routing hub. See shared_numbers.md.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ConvAI', '0103_conversation_idle_hours'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfiguration',
            name='self_reg_message',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='routing_priority',
            field=models.PositiveIntegerField(default=100),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='routing_claimed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='LinkedInstallation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('webhook_url', models.URLField(max_length=500)),
                ('api_token', models.CharField(blank=True, default='', max_length=255)),
                ('priority', models.PositiveIntegerField(default=10)),
                ('enabled', models.BooleanField(default=True)),
                ('last_contact', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('forward_failures', models.PositiveIntegerField(default=0)),
                ('peer_async', models.BooleanField(blank=True, null=True)),
                ('peer_registration_message', models.CharField(blank=True, default='', max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['priority', 'name'],
            },
        ),
    ]
