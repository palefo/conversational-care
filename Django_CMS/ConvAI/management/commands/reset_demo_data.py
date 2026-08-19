"""
Wipe the demo dataset and rebuild it with 20 clients on a realistic schedule.

Unlike the ``seed_*`` commands (which add to whatever is already there), this
one starts from a clean slate so the numbers on Home stay believable. The
previous dataset had 37 protocol calls still sitting in Pending in the past,
which is not something a real caseload looks like — a call that never happened
gets marked not-answered, or it gets done late, it does not stay open forever.

What it produces:

- 20 clients, each with a caregiver, an agent and a weekly/biweekly cadence.
- ~10 weeks of past meetings, all resolved: mostly complete, some not answered,
  a few interrupted. Completed ones get protocol answers and a playable
  recording.
- Today: a realistic day for the logged-in navigator — earlier slots already
  closed, later slots still pending.
- Exactly two genuinely overdue calls, so the overdue state is visible on Home
  without drowning it.
- Three weeks of future scheduled calls.
- Open alerts across all three priorities plus a resolved/false-alarm archive.
- Chatbot conversations with backdated messages; a few flagged important.

Preserved: users, agents, protocols and questions, site configuration.
Destroyed: patients, caregivers, meetings, answers, alerts, conversations,
messages, call recordings, self-registrations.

    docker compose exec web python manage.py reset_demo_data
    docker compose exec web python manage.py reset_demo_data --clients 20
"""

import math
import os
import random
import struct
import wave

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ConvAI.models import (
    Agent,
    Alert,
    Answer,
    CallRecording,
    Caregiver,
    Conversation,
    Meeting,
    Message,
    Patient,
    Protocol,
    SelfRegistration,
)

PLATFORM_PHONE = "+14155550100"

# Deterministic output so re-running gives the same demo, and so a screenshot
# taken today still matches the data tomorrow.
SEED = 20260804


def _make_sine_wav(path, freq=220.0, seconds=6, framerate=22050, volume=0.35):
    """Write a short mono sine-tone WAV (playable in any browser <audio>)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = int(seconds * framerate)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        frames = bytearray()
        for i in range(n):
            env = min(1.0, i / (framerate * 0.1), (n - i) / (framerate * 0.1))
            sample = int(volume * env * 32767 * math.sin(2 * math.pi * freq * i / framerate))
            frames += struct.pack("<h", sample)
        w.writeframes(bytes(frames))


CLIENTS = [
    ("María", "Quispe", "Lucía", "Ramos"),
    ("José", "Mamani", "Elena", "Quispe"),
    ("Rosa", "Huamán", "Elena", "Ríos"),
    ("Carlos", "Flores", "Sofía", "Núñez"),
    ("Ana", "Vargas", "Miguel", "Vargas"),
    ("Luis", "Condori", "Teresa", "Condori"),
    ("Carmen", "Apaza", "Julio", "Apaza"),
    ("Jorge", "Ramírez", "Marta", "Ramírez"),
    ("Beatriz", "Salazar", "Andrés", "Salazar"),
    ("Fernando", "Rojas", "Pilar", "Rojas"),
    ("Isabel", "Cárdenas", "Rubén", "Cárdenas"),
    ("Manuel", "Paredes", "Gloria", "Paredes"),
    ("Silvia", "Guerrero", "Diego", "Guerrero"),
    ("Ricardo", "Ávila", "Nuria", "Ávila"),
    ("Patricia", "León", "Tomás", "León"),
    ("Óscar", "Benavides", "Rocío", "Benavides"),
    ("Julia", "Montoya", "Esteban", "Montoya"),
    ("Héctor", "Salcedo", "Amelia", "Salcedo"),
    ("Norma", "Vega", "Ignacio", "Vega"),
    ("Gustavo", "Ibáñez", "Clara", "Ibáñez"),
]

DETAILS_MD = [
    "**Lives with** her daughter. Mild cognitive impairment, diagnosed 2024.\n\n"
    "- Mobility: walks indoors unaided\n- Hearing: good\n- Prefers morning calls",
    "**Lives alone**, son visits twice a week. Early-stage dementia.\n\n"
    "- Mobility: uses a stick outdoors\n- Hearing: reduced, speak slowly\n- Prefers afternoon calls",
    "**Lives with** his wife, who is the main caregiver.\n\n"
    "- Mobility: limited, stairs difficult\n- Recent hospital discharge\n- Prefers late morning",
    "**Lives in** sheltered housing. Moderate dementia.\n\n"
    "- Mobility: wheelchair for distances\n- Caregiver present most days\n- Short calls work best",
]

CARE_PLANS = [
    "Weekly protocol call. Review medication adherence and sleep. Escalate any fall.",
    "Biweekly protocol call. Focus on caregiver strain and respite options.",
    "Weekly protocol call. Monitor appetite and weight. Dietitian referral pending.",
    "Weekly protocol call. Post-discharge follow-up, wound check for the first month.",
]

# Alert titles and descriptions kept identical to the ones the platform already
# raises, so the UI never shows vocabulary that does not exist in production.
CONVERSATION_ALERTS = [
    ("Missed medication reported", "Caregiver reported two missed evening doses this week."),
    ("Wandering episode flagged", "Patient attempted to leave home unaccompanied; home safety review advised."),
    ("Caregiver requested respite", "Caregiver feeling overwhelmed; respite options being reviewed."),
    ("Reduced appetite noted", "Appetite improved after smaller, more frequent meals."),
    ("Sleep disturbance reported", "Night-time restlessness for several nights; evening routine discussed."),
    ("Fall risk mentioned", "Unsteady on the stairs; grab rail and home assessment suggested."),
]

SYSTEM_ALERTS = [
    ("New self-registration to approve", "A caregiver self-registered and is awaiting approval."),
    ("Weekly caseload review due", "Several patients have upcoming protocol calls this week — review the schedule."),
    ("Follow up on missed call", "A scheduled call was not answered; reschedule and confirm with the caregiver."),
]

CONVERSATIONS = [
    ("Daily check-in", "Brief daily check-in from the caregiver.",
     [("How is she today?", "Good morning. How has the night been?"),
      ("She slept well, woke once", "That sounds settled. Any confusion on waking?"),
      ("No, she was calm", "Good to hear. I'll note that down.")]),
    ("Sleep & routine", "Advised on evening routine to ease restlessness and improve sleep.",
     [("He's up half the night again", "That must be exhausting. What time does he usually settle?"),
      ("Around 2am, sometimes later", "A consistent wind-down hour before bed often helps. Shall we try that this week?"),
      ("Worth a try", "I'll flag it for your next call too.")]),
    ("Caregiver support", "Caregiver felt tired; offered reassurance and respite guidance.",
     [("I'm shattered, honestly", "That's completely understandable. Have you had any time off this month?"),
      ("Not really", "Let me note that — respite options are worth discussing on your next call.")]),
    ("Daily care", "Bathing resistance; suggested gentle, dignity-preserving approaches.",
     [("She refuses to shower", "That's very common and not defiance. Does she prefer a particular time of day?"),
      ("Mornings are worse", "Then an evening wash may go better. Warm the room first.")]),
    ("Reassurance & caregiver support", "Caregiver feeling exhausted; provided support and respite guidance.",
     [("Is this normal?", "Yes — what you're describing is a very common stage. You're doing the right things."),
      ("Thank you", "I'm here whenever you need.")]),
    ("Medication", "Reviewed the evening dose schedule after a missed dose.",
     [("I think I gave the evening one twice", "Thank you for telling me. Any dizziness or bleeding?"),
      ("No, he seems fine", "I'll raise this with your care navigator to be safe.")]),
]


class Command(BaseCommand):
    help = "Wipe demo data and rebuild 20 clients with a realistic schedule."

    def add_arguments(self, parser):
        parser.add_argument("--clients", type=int, default=20)
        parser.add_argument(
            "--noinput", action="store_true",
            help="Skip the confirmation prompt (for scripted runs).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(SEED)
        User = get_user_model()
        now = timezone.localtime()
        today = now.date()

        marco = (User.objects.filter(is_superuser=True).order_by("id").first()
                 or User.objects.order_by("id").first())
        if marco is None:
            self.stderr.write(self.style.ERROR("No users found — create one first."))
            return
        nav2 = User.objects.filter(username="navigator2").first()
        agent = Agent.objects.order_by("id").first()
        rec_dir = settings.CALL_RECORDINGS_DIR

        # ── wipe ────────────────────────────────────────────────
        doomed = {
            "answers": Answer.objects.count(),
            "meetings": Meeting.objects.count(),
            "alerts": Alert.objects.count(),
            "conversations": Conversation.objects.count(),
            "messages": Message.objects.count(),
            "recordings": CallRecording.objects.count(),
            "patients": Patient.objects.count(),
            "caregivers": Caregiver.objects.count(),
        }
        self.stdout.write("Deleting: " + ", ".join(f"{v} {k}" for k, v in doomed.items()))

        Answer.objects.all().delete()
        Meeting.objects.all().delete()
        Alert.objects.all().delete()
        Conversation.objects.all().delete()
        Message.objects.all().delete()
        CallRecording.objects.all().delete()
        SelfRegistration.objects.all().delete()
        Patient.objects.all().delete()
        Caregiver.objects.all().delete()

        # Recording rows are gone, so their audio files are orphans. Leaving
        # them behind means the media dir grows on every reset.
        try:
            removed = 0
            for fname in os.listdir(rec_dir):
                if fname.endswith(".wav"):
                    os.remove(os.path.join(rec_dir, fname))
                    removed += 1
            self.stdout.write(f"Removed {removed} orphaned recording files")
        except OSError:
            pass

        questions_by_proto = {
            p.number: list(p.questions.all())
            for p in Protocol.objects.prefetch_related("questions")
        }
        protocol_numbers = sorted(questions_by_proto) or [1]

        want = max(1, min(options["clients"], len(CLIENTS)))
        counts = dict.fromkeys(
            ["patients", "meetings", "answers", "recordings",
             "conversations", "messages", "alerts"], 0)

        # Two clients carry a genuinely overdue call. Any more and the overdue
        # state stops reading as an exception.
        overdue_idx = {2, 11}
        # Clients whose calls land on today's schedule for the main navigator.
        # Slots are relative to the current time rather than fixed clock hours,
        # so the day always shows a few calls already closed and a few still to
        # come — otherwise a reset run at 17:00 leaves Home looking empty.
        today_idx = [0, 1, 3, 5, 7, 9, 13]
        today_offsets_min = [-330, -210, -90, 45, 120, 210, 330]

        for idx in range(want):
            fn, ln, cg_fn, cg_ln = CLIENTS[idx]
            navigator = nav2 if (nav2 and idx % 5 == 4) else marco

            caregiver = Caregiver.objects.create(
                name=cg_fn, lastname=cg_ln, phone_number=f"+51992{idx:06d}"[:13],
            )
            patient = Patient.objects.create(
                name=fn, lastname=ln,
                phone_number=f"+51991{idx:06d}"[:13],
                caregiver=caregiver,
                navigator=navigator,
                agent=agent,
                # care_plan is a FileField (an uploaded document), so it stays
                # empty here — the plan text belongs in details.
                details=(
                    DETAILS_MD[idx % len(DETAILS_MD)]
                    + "\n\n**Care plan.** "
                    + CARE_PLANS[idx % len(CARE_PLANS)]
                ),
            )
            counts["patients"] += 1

            phones = [str(patient.phone_number), str(caregiver.phone_number)]

            # ── past meetings: every one of them resolved ───────
            # Cadence: weekly for most, biweekly for a third of the caseload.
            step_days = 7 if idx % 3 else 14
            weeks_back = 10
            call_hour = 9 + (idx % 8)
            occurrence = 0

            for days_ago in range(step_days, weeks_back * 7 + 1, step_days):
                day = today - timezone.timedelta(days=days_ago)
                when = timezone.make_aware(
                    timezone.datetime.combine(
                        day, timezone.datetime.min.time().replace(hour=call_hour, minute=0)
                    )
                )
                roll = rng.random()
                if roll < 0.80:
                    status = Meeting.Status.COMPLETED
                elif roll < 0.92:
                    status = Meeting.Status.NOT_ANSWERED
                else:
                    status = Meeting.Status.INTERRUPTED

                proto = protocol_numbers[occurrence % len(protocol_numbers)]
                occurrence += 1

                m = Meeting.objects.create(
                    patient=patient,
                    scheduled_time=when,
                    status=status,
                    type=Meeting.MeetingType.ONBOARDING if days_ago >= weeks_back * 7 else Meeting.MeetingType.REGULAR,
                    scheduled_protocol=proto,
                    executed_protocol=proto if status == Meeting.Status.COMPLETED else None,
                    retries=0 if status == Meeting.Status.COMPLETED else rng.randint(1, 3),
                )
                counts["meetings"] += 1

                if status != Meeting.Status.COMPLETED:
                    continue

                for step in range(9):
                    setattr(m, f"step_{step}", True)
                m.save(update_fields=[f"step_{i}" for i in range(9)])

                for q in questions_by_proto.get(proto, [])[:4]:
                    Answer.objects.create(
                        meeting=m, question=q,
                        response=rng.choice([
                            "No change since the last call.",
                            "Slight improvement this week.",
                            "Caregiver reports some difficulty, monitoring.",
                            "Discussed and agreed a plan for next week.",
                        ]),
                    )
                    counts["answers"] += 1

                dur = rng.randint(180, 620)
                sid = f"REdemo{patient.pk:03d}{days_ago:03d}"
                CallRecording.objects.create(
                    recording_sid=sid,
                    from_number=PLATFORM_PHONE,
                    to_number=str(patient.phone_number),
                    start_time=when,
                    end_time=when + timezone.timedelta(seconds=dur),
                    duration=dur,
                    filename=f"{sid}.wav",
                )
                counts["recordings"] += 1
                try:
                    _make_sine_wav(
                        os.path.join(rec_dir, f"{sid}.wav"),
                        freq=170.0 + (patient.pk % 14) * 22.0, seconds=6,
                    )
                except OSError:
                    pass  # recordings dir unavailable; the row is still useful

            # ── overdue: a call that was never made ────────────
            if idx in overdue_idx:
                when = timezone.make_aware(
                    timezone.datetime.combine(
                        today - timezone.timedelta(days=2 if idx == 2 else 4),
                        timezone.datetime.min.time().replace(hour=10, minute=0),
                    )
                )
                Meeting.objects.create(
                    patient=patient, scheduled_time=when,
                    status=Meeting.Status.PENDING,
                    type=Meeting.MeetingType.REGULAR,
                    scheduled_protocol=protocol_numbers[idx % len(protocol_numbers)],
                    retries=2,
                )
                counts["meetings"] += 1

            # ── today ──────────────────────────────────────────
            if idx in today_idx:
                offset = today_offsets_min[today_idx.index(idx)]
                when = (now + timezone.timedelta(minutes=offset)).replace(
                    second=0, microsecond=0
                )
                # Keep it inside today, so it lands in the right bucket on Home.
                if when.date() != today:
                    when = now.replace(
                        hour=(9 if offset < 0 else 20), minute=0, second=0, microsecond=0
                    )
                # Slots already past are closed out; upcoming ones stay pending.
                if when < now:
                    status = Meeting.Status.COMPLETED if rng.random() < 0.75 else Meeting.Status.NOT_ANSWERED
                else:
                    status = Meeting.Status.PENDING
                proto = protocol_numbers[occurrence % len(protocol_numbers)]
                m = Meeting.objects.create(
                    patient=patient, scheduled_time=when, status=status,
                    type=Meeting.MeetingType.REGULAR,
                    scheduled_protocol=proto,
                    executed_protocol=proto if status == Meeting.Status.COMPLETED else None,
                )
                counts["meetings"] += 1
                if status == Meeting.Status.COMPLETED:
                    dur = rng.randint(200, 500)
                    sid = f"REdemo{patient.pk:03d}today"
                    CallRecording.objects.create(
                        recording_sid=sid, from_number=PLATFORM_PHONE,
                        to_number=str(patient.phone_number),
                        start_time=when, end_time=when + timezone.timedelta(seconds=dur),
                        duration=dur, filename=f"{sid}.wav",
                    )
                    counts["recordings"] += 1
                    try:
                        _make_sine_wav(os.path.join(rec_dir, f"{sid}.wav"), seconds=6)
                    except OSError:
                        pass

            # ── future ─────────────────────────────────────────
            for k in range(1, 4):
                day = today + timezone.timedelta(days=step_days * k)
                when = timezone.make_aware(
                    timezone.datetime.combine(
                        day, timezone.datetime.min.time().replace(hour=call_hour, minute=0)
                    )
                )
                Meeting.objects.create(
                    patient=patient, scheduled_time=when,
                    status=Meeting.Status.PENDING,
                    type=Meeting.MeetingType.REGULAR,
                    scheduled_protocol=protocol_numbers[(occurrence + k) % len(protocol_numbers)],
                )
                counts["meetings"] += 1

            # ── conversations ──────────────────────────────────
            for c in range(rng.randint(2, 4)):
                topic, summary, turns = CONVERSATIONS[(idx + c) % len(CONVERSATIONS)]
                days_ago = rng.randint(0, 21)
                last_ts = now - timezone.timedelta(days=days_ago, hours=rng.randint(0, 8))
                # A few recent ones are flagged for review and not yet opened.
                important = days_ago <= 3 and c == 0 and idx % 4 == 0
                conv = Conversation.objects.create(
                    patient=patient, agent=agent,
                    started_at=last_ts - timezone.timedelta(minutes=12),
                    last_message_at=last_ts,
                    topic=topic, summary=summary,
                    is_important=important,
                    visited=not important,
                    analyzed=True, analyzed_at=last_ts,
                )
                counts["conversations"] += 1
                for j, (user_msg, bot_msg) in enumerate(turns):
                    msg = Message.objects.create(
                        conversation_id=str(conv.id),
                        user=phones[1],
                        user_message=user_msg,
                        response_message=bot_msg,
                    )
                    # timestamp is auto_now_add, so it has to be backdated after
                    # the insert or every message lands on "now".
                    Message.objects.filter(pk=msg.pk).update(
                        timestamp=last_ts - timezone.timedelta(minutes=(len(turns) - j) * 3)
                    )
                    counts["messages"] += 1

            # ── alerts ─────────────────────────────────────────
            # Open alerts on roughly half the caseload, resolved ones on most,
            # so the archive has something in it without Home being flooded.
            if idx % 2 == 0:
                title, descr = CONVERSATION_ALERTS[idx % len(CONVERSATION_ALERTS)]
                priority = [Alert.Priority.HIGH, Alert.Priority.MEDIUM, Alert.Priority.LOW][idx % 3]
                a = Alert.objects.create(
                    patient=patient, user=navigator, created_by=navigator,
                    title=title, description=descr,
                    priority=priority,
                    status=Alert.AlertStatus.IN_PROGRESS if idx % 4 == 0 else Alert.AlertStatus.CREATED,
                    alert_type=Alert.AlertType.CONVERSATION,
                )
                Alert.objects.filter(pk=a.pk).update(
                    created_at=now - timezone.timedelta(hours=rng.randint(1, 40))
                )
                counts["alerts"] += 1

            for r in range(rng.randint(1, 3)):
                title, descr = CONVERSATION_ALERTS[(idx + r + 2) % len(CONVERSATION_ALERTS)]
                false_alarm = rng.random() < 0.3
                a = Alert.objects.create(
                    patient=patient, user=navigator, created_by=navigator,
                    title=title, description=descr,
                    priority=[Alert.Priority.HIGH, Alert.Priority.MEDIUM, Alert.Priority.LOW][(idx + r) % 3],
                    status=Alert.AlertStatus.RESOLVED,
                    alert_type=Alert.AlertType.CONVERSATION,
                    data={"archive_bucket": "false_alarms" if false_alarm else "resolved"},
                    acted_at=now - timezone.timedelta(days=rng.randint(3, 40)),
                )
                Alert.objects.filter(pk=a.pk).update(
                    created_at=now - timezone.timedelta(days=rng.randint(5, 60))
                )
                counts["alerts"] += 1

        # A couple of platform-level alerts that are not about one client.
        for title, descr in SYSTEM_ALERTS:
            a = Alert.objects.create(
                user=marco, created_by=marco,
                title=title, description=descr,
                priority=Alert.Priority.MEDIUM,
                status=Alert.AlertStatus.CREATED,
                alert_type=Alert.AlertType.DEFAULT,
            )
            Alert.objects.filter(pk=a.pk).update(
                created_at=now - timezone.timedelta(hours=rng.randint(2, 30))
            )
            counts["alerts"] += 1

        self.stdout.write(self.style.SUCCESS(
            "Created: " + ", ".join(f"{v} {k}" for k, v in counts.items())
        ))

        pending_past = Meeting.objects.filter(
            status=Meeting.Status.PENDING, scheduled_time__date__lt=today
        ).count()
        pending_today = Meeting.objects.filter(
            status=Meeting.Status.PENDING, scheduled_time__date=today
        ).count()
        self.stdout.write(
            f"Schedule check — overdue: {pending_past}, pending today: {pending_today}"
        )
