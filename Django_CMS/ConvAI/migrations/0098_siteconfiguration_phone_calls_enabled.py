from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ConvAI', '0097_jobs_and_online_seams'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfiguration',
            name='phone_calls_enabled',
            field=models.CharField(blank=True, choices=[('', 'Use .env default'), ('1', 'On'), ('0', 'Off')], default='', max_length=1),
        ),
    ]
