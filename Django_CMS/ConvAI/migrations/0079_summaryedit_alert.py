from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0078_summary_edit"),
    ]

    operations = [
        migrations.AddField(
            model_name="summaryedit",
            name="alert",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="summary_edit",
                to="ConvAI.alert",
            ),
        ),
    ]
