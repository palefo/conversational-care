"""
Add a larger batch of varied demo data across the whole platform.

Where ``seed_demo_fill`` only fills gaps on existing patients, this command
*creates new* patients and gives each the full downstream chain so every page
has plenty of realistic content:

- New patients (with caregiver, details markdown, agent, phone).
- Meetings spread across every status/protocol (completed → answers + call
  recording; pending today/future; not-answered; interrupted).
- Chatbot conversations with multi-turn messages; some flagged important.
- Patient- and user-level alerts across priorities/statuses/types.
- Extra self-registrations (registered + approved).
- A few patients assigned to ``navigator2`` so multi-navigator views populate.

Idempotent: patients are keyed by (name, lastname); re-running reuses them and
only backfills what's missing. Never touches Pablo Fonseca.

    docker compose exec web python manage.py seed_bulk_fill --patients 12
"""

import math
import os
import struct
import wave

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ConvAI.models import (
    Agent,
    Alert,
    Answer,
    Caregiver,
    CallRecording,
    Conversation,
    Meeting,
    Message,
    Patient,
    Protocol,
    SelfRegistration,
)

PLATFORM_PHONE = "+14155550100"


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


# ── content pools ──────────────────────────────────────────────
PATIENT_NAMES = [
    ("Carmen", "Vásquez"), ("Jorge", "Mendoza"), ("Rosa", "Quispe"),
    ("Alberto", "Huamán"), ("Beatriz", "Flores"), ("Fernando", "Rojas"),
    ("Isabel", "Cárdenas"), ("Manuel", "Paredes"), ("Silvia", "Guerrero"),
    ("Ricardo", "Ávila"), ("Patricia", "León"), ("Óscar", "Benavides"),
    ("Julia", "Montoya"), ("Héctor", "Salcedo"), ("Norma", "Vega"),
    ("Gustavo", "Ibáñez"),
]

CAREGIVER_NAMES = [
    ("Lucía", "Ramos"), ("Miguel", "Torres"), ("Elena", "Ríos"),
    ("Diego", "Salazar"), ("Sofía", "Núñez"), ("Andrés", "Castro"),
    ("Verónica", "Pino"), ("Tomás", "Aguirre"), ("Marta", "Delgado"),
    ("Pedro", "Campos"), ("Ana", "Soto"), ("Luis", "Bravo"),
]

DETAILS_MD = [
    """\
**Diagnosis:** Mild cognitive impairment; supported at home by daughter.

**Key points**
- On a regular protocol schedule.
- Medication reviewed at each call.
- Caregiver engaged and reliable.
""",
    """\
**Diagnosis:** Moderate dementia; lives with spouse.

**Key points**
- Some sundowning in the evenings.
- Fall risk noted at home — rugs removed.
- Uses a weekly pill organiser.
""",
    """\
**Diagnosis:** Cognitive decline under review.

**Key points**
- Recent onboarding; baseline being established.
- Caregiver new to the role; needs extra support.
- Appetite reduced this month.
""",
]

CONV_POOL = [
    ("Medication adherence",
     "Discussed a missed dose and simple routines to improve adherence.",
     [
        ("He sometimes forgets whether he took his pills. What can we do?",
         "A weekly pill organiser and linking the dose to a fixed routine (like after breakfast) really help. Keep a simple tick-chart nearby, and tell your navigator if refusals continue."),
        ("Okay, we'll try the organiser.",
         "Great. Keep it in a visible, consistent place and check it together each day."),
     ]),
    ("Sleep & routine",
     "Advised on evening routine to ease restlessness and improve sleep.",
     [
        ("She's restless in the evenings and doesn't sleep well.",
         "Try keeping lights on before dusk, reducing noise, and a calm evening routine. Avoid caffeine and long daytime naps."),
        ("We'll keep the evenings calmer.",
         "Good plan. Soft lighting and a predictable routine make a real difference."),
     ]),
    ("Caregiver support",
     "Caregiver felt tired; offered reassurance and respite guidance.",
     [
        ("I'm feeling really tired and a bit overwhelmed.",
         "That's completely understandable — caregiving is demanding. Short breaks and respite services matter for your health too."),
        ("I don't have much help right now.",
         "You're not alone. I've flagged this so your navigator can talk through local respite options."),
     ]),
    ("Nutrition & appetite",
     "Discussed reduced appetite and practical mealtime tips.",
     [
        ("He's eating less than usual lately. Should I worry?",
         "Smaller, more frequent meals and familiar favourites can help. Keep mealtimes calm. If weight loss continues, tell your navigator."),
        ("I'll offer smaller meals more often.",
         "That's a good approach. Company at meals often helps too."),
     ]),
    ("Wandering & safety",
     "Reviewed a wandering episode and home-safety steps.",
     [
        ("He tried to leave the house on his own yesterday.",
         "Consider door chimes, a simple lock out of eye-line, and a calm redirection routine. I'll flag this for your navigator to follow up."),
        ("Thank you, we'll set that up.",
         "Good. Keep a recent photo handy and note the times of day it tends to happen."),
     ]),
    ("Mood & agitation",
     "Talked through afternoon agitation and calming techniques.",
     [
        ("She gets very agitated in the afternoons.",
         "A predictable routine, reduced noise, and a familiar activity can ease agitation. Note any triggers so we can spot a pattern."),
        ("We'll try a quiet activity after lunch.",
         "That often helps. Let your navigator know if it escalates."),
     ]),
]

ANSWERS_POOL = {
    2: [
        "Stated the month but not the year; mild disorientation to time.",
        "Knew she was at home; no place confusion today.",
        "Repeated all three objects on the first attempt.",
        "Recalled two of three objects after the delay.",
        "Caregiver reports memory is stable compared to last week.",
    ],
    3: [
        "Took all medications on schedule this week; no missed doses.",
        "No wandering episodes reported.",
        "Sleep is reasonable; occasional early waking.",
        "Manages self-care with light prompting.",
        "No new safety concerns at home this week.",
    ],
}

PATIENT_ALERTS = [
    ("Missed medication reported", "HIGH", "CREATED",
     "Caregiver reported two missed evening doses this week."),
    ("Caregiver requested respite", "MEDIUM", "IN_PROGRESS",
     "Caregiver feeling overwhelmed; respite options being reviewed."),
    ("Wandering episode flagged", "HIGH", "CREATED",
     "Patient attempted to leave home unaccompanied; home safety review needed."),
    ("Reduced appetite noted", "LOW", "RESOLVED",
     "Appetite improved after smaller, more frequent meals."),
]

USER_ALERTS = [
    ("Weekly caseload review due", "MEDIUM", "CREATED",
     "Several patients have upcoming protocol calls this week — review the calendar."),
    ("Follow up on missed call", "LOW", "IN_PROGRESS",
     "A scheduled call was not answered; reschedule and confirm with the caregiver."),
    ("New self-registration to approve", "MEDIUM", "CREATED",
     "A caregiver self-registered and is awaiting approval."),
]

EXTRA_SELF_REGS = [
    ("Marta", "Sánchez", "+5199000201", "marta.sanchez@example.com"),
    ("Carlos", "Reyes", "+5199000202", "carlos.reyes@example.com"),
    ("Lucía", "Fernández", "+5199000203", "lucia.fernandez@example.com"),
    ("Óscar", "Medina", "+5199000204", "oscar.medina@example.com"),
    ("Rosa", "Cabrera", "+5199000205", "rosa.cabrera@example.com"),
]


class Command(BaseCommand):
    help = "Create a larger batch of varied demo data across the whole platform."

    def add_arguments(self, parser):
        parser.add_argument("--patients", type=int, default=12,
                            help="How many new patients to ensure (max 16).")

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()
        User = get_user_model()
        marco = (User.objects.filter(is_superuser=True).order_by("id").first()
                 or User.objects.order_by("id").first())
        nav2 = User.objects.filter(username="navigator2").first()
        agent = Agent.objects.order_by("id").first()
        rec_dir = settings.CALL_RECORDINGS_DIR

        questions_by_proto = {
            n: list(Protocol.objects.get(number=n).questions.all())
            for n in (2, 3) if Protocol.objects.filter(number=n).exists()
        }

        n_pat = n_cg = n_meet = n_ans = n_rec = n_conv = n_msg = n_alert = 0
        want = max(1, min(options["patients"], len(PATIENT_NAMES)))

        for idx in range(want):
            fn, ln = PATIENT_NAMES[idx]
            navigator = nav2 if (nav2 and idx % 4 == 3) else marco

            patient, created = Patient.objects.get_or_create(
                name=fn, lastname=ln,
                defaults={
                    "phone_number": f"+5199010{idx:04d}"[:13],
                    "navigator": navigator,
                    "agent": agent,
                    "details": DETAILS_MD[idx % len(DETAILS_MD)],
                },
            )
            if created:
                n_pat += 1
            # backfill essentials on reuse
            changed = False
            if not patient.navigator:
                patient.navigator = navigator; changed = True
            if not patient.agent:
                patient.agent = agent; changed = True
            if not (patient.details or "").strip():
                patient.details = DETAILS_MD[idx % len(DETAILS_MD)]; changed = True
            if not patient.caregiver:
                cfn, cln = CAREGIVER_NAMES[idx % len(CAREGIVER_NAMES)]
                patient.caregiver = Caregiver.objects.create(
                    name=cfn, lastname=cln, phone_number=f"+5199020{idx:04d}"[:13],
                )
                n_cg += 1; changed = True
            if changed:
                patient.save()

            # ── meetings across statuses ──────────────────────────
            if patient.meetings.count() == 0:
                proto = 2 if idx % 2 == 0 else 3
                other = 3 if proto == 2 else 2
                plan = [
                    (26, "COMPLETED", proto),
                    (19, "COMPLETED", other),
                    (12, "NOT_ANSWERED", proto),
                    (6, "INTERRUPTED", other),
                    (-1, "PENDING", proto),   # upcoming
                    (0, "PENDING", other),    # today
                ]
                for days_ago, status_name, pr in plan:
                    st = getattr(Meeting.Status, status_name)
                    when = now - timezone.timedelta(days=days_ago) if days_ago >= 0 \
                        else now + timezone.timedelta(days=-days_ago, hours=2)
                    m = Meeting.objects.create(
                        patient=patient,
                        scheduled_time=when,
                        type=Meeting.MeetingType.REGULAR,
                        status=st,
                        scheduled_protocol=pr,
                        executed_protocol=pr if st == Meeting.Status.COMPLETED else None,
                    )
                    n_meet += 1

                    if st == Meeting.Status.COMPLETED:
                        for i in range(5):
                            setattr(m, f"step_{i}", True)
                        m.save()
                        qs = questions_by_proto.get(pr, [])
                        responses = ANSWERS_POOL.get(pr, [])
                        for q, resp in zip(qs, responses):
                            Answer.objects.get_or_create(
                                meeting=m, question=q, defaults={"response": resp}
                            )
                            n_ans += 1
                        # call recording
                        to_num = str(patient.phone_number or "")
                        sid = f"BULKREC-{patient.pk}-{m.pk}"
                        if to_num and not CallRecording.objects.filter(recording_sid=sid).exists():
                            dur = 1500 + (m.pk % 25) * 60
                            wav_path = os.path.join(rec_dir, f"{sid}.wav")
                            _make_sine_wav(wav_path, freq=170.0 + (m.pk % 14) * 22.0, seconds=6)
                            CallRecording.objects.create(
                                recording_sid=sid,
                                from_number=PLATFORM_PHONE,
                                to_number=to_num,
                                start_time=m.scheduled_time,
                                end_time=m.scheduled_time + timezone.timedelta(seconds=dur),
                                duration=dur,
                                filename=wav_path,
                                transcript="Simulated call transcript for demo purposes.",
                                transcript_summary="Caregiver check-in; medication and routine reviewed.",
                                transcribed_at=m.scheduled_time,
                            )
                            n_rec += 1

            # ── conversations ─────────────────────────────────────
            if Conversation.objects.filter(patient=patient).count() < 2:
                for c in range(2):
                    topic, summary, pairs = CONV_POOL[(idx + c) % len(CONV_POOL)]
                    day_start = now - timezone.timedelta(days=9 - c * 3, hours=idx % 6)
                    important = (idx + c) % 5 == 0
                    conv = Conversation.objects.create(
                        patient=patient, agent=agent, topic=topic, summary=summary,
                        started_at=day_start,
                        last_message_at=day_start + timezone.timedelta(minutes=len(pairs) * 3),
                        analyzed=True, analyzed_at=day_start,
                        is_important=important, visited=not important,
                    )
                    n_conv += 1
                    for j, (um, bm) in enumerate(pairs):
                        msg = Message.objects.create(
                            conversation_id=str(conv.id),
                            user=str(patient.phone_number or patient.pk),
                            user_message=um, response_message=bm,
                            liked=(j == 0 and c == 0),
                        )
                        Message.objects.filter(pk=msg.pk).update(
                            timestamp=day_start + timezone.timedelta(minutes=j * 3)
                        )
                        n_msg += 1

            # ── patient alerts ────────────────────────────────────
            if not patient.alerts.exists():
                title, prio, status, desc = PATIENT_ALERTS[idx % len(PATIENT_ALERTS)]
                Alert.objects.create(
                    patient=patient, title=title, description=desc,
                    priority=getattr(Alert.Priority, prio),
                    status=getattr(Alert.AlertStatus, status),
                    alert_type=Alert.AlertType.CONVERSATION,
                    created_by=marco,
                    user=patient.navigator,
                )
                n_alert += 1

        # ── user-level alerts for navigators ──────────────────────
        for nav in [u for u in (marco, nav2) if u]:
            for title, prio, status, desc in USER_ALERTS:
                if not Alert.objects.filter(title=title, user=nav).exists():
                    Alert.objects.create(
                        user=nav, title=title, description=desc,
                        priority=getattr(Alert.Priority, prio),
                        status=getattr(Alert.AlertStatus, status),
                        alert_type=Alert.AlertType.DEFAULT,
                        created_by=marco,
                    )
                    n_alert += 1

        # ── extra self-registrations ──────────────────────────────
        n_sr = 0
        for i, (fn, ln, phone, email) in enumerate(EXTRA_SELF_REGS):
            _, created = SelfRegistration.objects.get_or_create(
                name=fn, lastname=ln,
                defaults={
                    "phone_number": phone, "email": email,
                    "state": SelfRegistration.State.APPROVED if i % 3 == 0
                    else SelfRegistration.State.REGISTERED,
                },
            )
            if created:
                n_sr += 1

        self.stdout.write(self.style.SUCCESS(
            f"Bulk fill complete: +{n_pat} patients, +{n_cg} caregivers, "
            f"+{n_meet} meetings, +{n_ans} answers, +{n_rec} recordings, "
            f"+{n_conv} conversations, +{n_msg} messages, +{n_alert} alerts, "
            f"+{n_sr} self-registrations."
        ))
