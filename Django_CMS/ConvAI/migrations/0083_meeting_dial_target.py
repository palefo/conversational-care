"""Who a call rings, and whether anyone booked it.

Both default to what the platform did before them, so every existing row keeps
its meaning: dial_target 0 is the caregiver, which is the only person any call
has ever reached, and unscheduled False is a call that was booked — which all
of them were, there having been no other way to place one.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ConvAI", "0082_agent_description")]

    operations = [
        migrations.AddField(
            model_name="meeting",
            name="dial_target",
            field=models.IntegerField(
                choices=[(0, "Caregiver"), (1, "Client")],
                default=0,
                help_text="Which of the client's two numbers the bridge rings",
            ),
        ),
        migrations.AddField(
            model_name="meeting",
            name="unscheduled",
            field=models.BooleanField(
                default=False,
                help_text="Placed on the spot rather than booked in advance",
            ),
        ),
    ]
