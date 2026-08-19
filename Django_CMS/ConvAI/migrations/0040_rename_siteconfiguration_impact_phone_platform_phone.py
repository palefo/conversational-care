from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0039_alter_convaiuser_preferred_language"),
    ]

    operations = [
        migrations.RenameField(
            model_name="siteconfiguration",
            old_name="impact_phone",
            new_name="platform_phone",
        ),
    ]
