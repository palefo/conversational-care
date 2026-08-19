from django.db import migrations


def create_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ("Navigator", "PatientTester"):
        Group.objects.get_or_create(name=name)


def noop(apps, schema_editor):
    # Keep the groups on reverse (they may own memberships); nothing to undo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0041_alter_siteconfiguration_options"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_groups, noop),
    ]
