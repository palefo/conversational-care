"""Give clients a programme based on what they have already been through.

Migration 0077 runs this once, at the moment the feature ships. This command is
for afterwards: clients imported from elsewhere, or a deployment restored from a
backup taken before the migration, arrive with an empty programme and therefore
an empty call panel. It is additive — it never unticks a protocol somebody chose
by hand — so it is safe to run more than once.
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from ConvAI.models import Answer, Meeting, Patient


class Command(BaseCommand):
    help = "Add protocols to clients' programmes based on their answers and pending calls."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change and write nothing.",
        )
        parser.add_argument(
            "--client",
            type=int,
            default=None,
            metavar="ID",
            help="Only this client, by id.",
        )

    def handle(self, *args, **options):
        dry = options["dry_run"]
        only = options["client"]

        patients = Patient.objects.all()
        if only is not None:
            patients = patients.filter(pk=only)
            if not patients.exists():
                self.stderr.write(self.style.ERROR(f"No client with id {only}."))
                return

        # Answered means the protocol applies to them; booked on a call still to
        # happen means somebody has already decided it does. Anything wider hands
        # every client the full list again, which is the state this replaces.
        wanted = {}

        answers = (Answer.objects
                   .filter(meeting__patient__in=patients)
                   .values_list("meeting__patient_id", "question__protocol_id")
                   .distinct())
        for patient_id, protocol_id in answers:
            if patient_id and protocol_id:
                wanted.setdefault(patient_id, set()).add(protocol_id)

        booked = (Meeting.objects
                  .filter(patient__in=patients, status=Meeting.Status.PENDING)
                  .values_list("patient_id", "scheduled_protocols__id")
                  .distinct())
        for patient_id, protocol_id in booked:
            if patient_id and protocol_id:
                wanted.setdefault(patient_id, set()).add(protocol_id)

        added_total = 0
        touched = 0
        for patient in (patients
                        .filter(pk__in=wanted)
                        .prefetch_related("protocols")
                        .order_by("name", "lastname")):
            have = {p.pk for p in patient.protocols.all()}
            new = wanted[patient.pk] - have
            if not new:
                continue

            touched += 1
            added_total += len(new)
            self.stdout.write(
                f"{patient.name} {patient.lastname} (#{patient.pk}): +{len(new)} protocol(s)"
            )
            if not dry:
                patient.protocols.add(*new)

        empty = (patients
                 .annotate(n=Count("protocols"))
                 .filter(n=0)
                 .exclude(pk__in=wanted)
                 .count())

        verb = "would add" if dry else "added"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {added_total} protocol(s) across {touched} client(s)."
        ))
        if empty:
            # Not a failure. A client with no answers and nothing booked has no
            # evidence behind them, and guessing a programme is exactly what
            # this change exists to stop.
            self.stdout.write(
                f"{empty} client(s) left with no programme — nothing recorded to infer one from. "
                "Choose their protocols on their profile."
            )
