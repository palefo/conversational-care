"""Give every agent a one-line description, and write one for the natives.

The Agents page listed four native cards that said little beyond a name and a
model id: the only way to learn what `link_worker` was *for* was to read the
code. The field is editable for all kinds, but native agents ship with the
platform, so their descriptions ship with it too.

Seeding only fills a blank description. An admin who has reworded one keeps
their wording if this migration is ever re-run against the same database.
"""

from django.db import migrations, models


# Keyed by native_key, which is what identifies a native agent (the display
# name is admin-editable). Wording is deliberately about what the agent *does*
# for the service, not how it is built.
#
# Each one is kept inside CARD_BUDGET: the card gives a description two lines
# and trims what will not fit, and a sentence cut off at "hands the client
# bac…" is worse than the shorter sentence that fits. Anything a card cannot
# hold belongs on the agent's own page, not in this dict.
CARD_BUDGET = 90

NATIVE_DESCRIPTIONS = {
    "link_worker":
        "Finds clients and schedules meetings for them, from the navigator chat bubble.",
    "loopback":
        "Echoes back whatever it is sent — a plumbing test for a chat channel.",
    "protocol_qa":
        "Asks a protocol's questions over WhatsApp and saves the answers.",
    "self_registration":
        "Takes the name and number of an unknown sender, for an admin to approve.",
}


def seed_descriptions(apps, schema_editor):
    Agent = apps.get_model("ConvAI", "Agent")
    for native_key, description in NATIVE_DESCRIPTIONS.items():
        Agent.objects.filter(
            kind="native", native_key=native_key, description=""
        ).update(description=description)


def unseed_descriptions(apps, schema_editor):
    # Only take back what was written here, and only if it still reads that way.
    Agent = apps.get_model("ConvAI", "Agent")
    for native_key, description in NATIVE_DESCRIPTIONS.items():
        Agent.objects.filter(
            kind="native", native_key=native_key, description=description
        ).update(description="")


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0081_backfill_recording_owners"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="description",
            field=models.CharField(
                blank=True, default="", max_length=200,
                help_text="One line on what this agent does. Shown on its card "
                          "on the Agents page, where about 90 characters fit.",
            ),
        ),
        migrations.RunPython(seed_descriptions, unseed_descriptions),
    ]
