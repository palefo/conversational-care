from django.db import migrations


NATIVE_AGENTS = [
    {"name": "Protocol QA Agent", "native_key": "protocol_qa"},
]


def seed_native_agents(apps, schema_editor):
    Agent = apps.get_model("ConvAI", "Agent")
    for spec in NATIVE_AGENTS:
        # Agent.name is unique. A legacy *remote* prototype may already hold this
        # display name — rename it out of the way so the new native agent can use it.
        legacy = (
            Agent.objects.filter(name=spec["name"])
            .exclude(kind="native", native_key=spec["native_key"])
            .first()
        )
        if legacy is not None:
            legacy.name = f"{spec['name']} (legacy remote)"
            legacy.save(update_fields=["name"])

        Agent.objects.update_or_create(
            native_key=spec["native_key"],
            kind="native",
            defaults={"name": spec["name"], "host": "", "langgraph_name": "", "port": None},
        )


def unseed_native_agents(apps, schema_editor):
    Agent = apps.get_model("ConvAI", "Agent")
    Agent.objects.filter(
        kind="native", native_key__in=[s["native_key"] for s in NATIVE_AGENTS]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0051_patient_automation_fields"),
    ]

    operations = [
        migrations.RunPython(seed_native_agents, unseed_native_agents),
    ]
