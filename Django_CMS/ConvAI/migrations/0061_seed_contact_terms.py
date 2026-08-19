from django.db import migrations

# The five the platform ships with. Their slugs match what was already stored
# on Patient.contact_terms, so existing preferences keep resolving.
STANDARD = [
    ("mornings", "Mornings only"),
    ("afternoons", "Afternoons only"),
    ("voice", "Voice, not text"),
    ("weekdays", "No weekends"),
    ("caregiver_first", "Caregiver first"),
]


def seed(apps, schema_editor):
    ContactTerm = apps.get_model("ConvAI", "ContactTerm")
    for slug, label in STANDARD:
        ContactTerm.objects.update_or_create(
            slug=slug, defaults={"label": label, "is_standard": True},
        )


def unseed(apps, schema_editor):
    ContactTerm = apps.get_model("ConvAI", "ContactTerm")
    ContactTerm.objects.filter(slug__in=[s for s, _ in STANDARD]).delete()


class Migration(migrations.Migration):
    dependencies = [("ConvAI", "0060_contactterm")]
    operations = [migrations.RunPython(seed, unseed)]
