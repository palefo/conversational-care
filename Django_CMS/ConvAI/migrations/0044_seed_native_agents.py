from django.db import migrations


NATIVE_AGENTS = [
    {"name": "Loopback", "native_key": "loopback"},
    {"name": "Link Worker", "native_key": "link_worker"},
]


def seed_native_agents(apps, schema_editor):
    Agent = apps.get_model("ConvAI", "Agent")
    for spec in NATIVE_AGENTS:
        Agent.objects.update_or_create(
            native_key=spec["native_key"],
            kind="native",
            defaults={"name": spec["name"], "host": "", "langgraph_name": "", "port": None},
        )


def unseed_native_agents(apps, schema_editor):
    Agent = apps.get_model("ConvAI", "Agent")
    Agent.objects.filter(kind="native", native_key__in=[s["native_key"] for s in NATIVE_AGENTS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ConvAI", "0043_agent_kind_agent_native_key_alter_agent_host_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_native_agents, unseed_native_agents),
    ]
