from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('ConvAI', '0050_callrecording_transcribed_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='patient',
            name='automation_prev_agent',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+', to='ConvAI.agent',
                help_text='Agent to restore when the running automation ends.',
            ),
        ),
        migrations.AddField(
            model_name='patient',
            name='automation_meeting',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+', to='ConvAI.meeting',
                help_text='Meeting the running automation is collecting answers for.',
            ),
        ),
        migrations.AddField(
            model_name='patient',
            name='automation_protocol',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True,
                help_text='Protocol number the running automation is working through.',
            ),
        ),
        migrations.AddField(
            model_name='patient',
            name='automation_expires_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='Sliding idle deadline for the automation; empty means no automation is active.',
            ),
        ),
    ]
