"""
Fill in demo content across the whole app so every page has consistent data.

Complements ``seed_pablo_timeline`` (which seeds one rich patient). This command
levels up the *other* existing patients and populates the pages that are still
empty, without deleting anything:

- Call recordings (playable WAV) + protocol answers for every COMPLETED meeting.
- At least one chatbot conversation per patient (so timelines aren't empty).
- A caregiver for patients that lack one.
- A few extra meetings for patients that have none.
- A PatientTester user linked 1:1 to a patient (so the audio chatbot works).
- One or two extra Navigator users (so the Users page has variety).
- A couple of user-level alerts and a few self-registrations.

Idempotent: safe to re-run. Existing rows are reused/skipped, not duplicated.
It never touches Pablo Fonseca (seeded separately).

    docker compose exec web python manage.py seed_demo_fill
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
    Question,
    SelfRegistration,
)

PABLO = ("Pablo", "Fonseca")  # never touched here
PLATFORM_PHONE = "+14155550100"


# ─────────────────────────── audio ────────────────────────────
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


# ─────────────────────── content pools ────────────────────────
# Short, plausible answers aligned to the 5 questions of protocols 2 and 3
# (defined in seed_pablo_timeline). Rotated per patient for variety.
ANSWERS = {
    2: [
        [
            "Stated the month but not the year; mild disorientation to time.",
            "Knew she was at home; no place confusion today.",
            "Repeated all three objects on the first attempt.",
            "Recalled two of three objects after the delay.",
            "Caregiver reports memory is stable compared to last week.",
        ],
        [
            "Could not give the date; guessed the wrong decade.",
            "Asked twice which building he was in — mild place disorientation.",
            "Repeated the three objects correctly.",
            "Recalled only one object after a few minutes.",
            "Caregiver feels confusion has increased slightly this week.",
        ],
    ],
    3: [
        [
            "Took all medications on schedule this week; no missed doses.",
            "No wandering episodes reported.",
            "Sleep is reasonable; occasional early waking.",
            "Manages self-care with light prompting.",
            "No new safety concerns at home this week.",
        ],
        [
            "Missed one morning dose; caregiver now using a pill organiser.",
            "One episode of trying to leave in the afternoon; redirected calmly.",
            "Restless in the evenings; some sundowning after dark.",
            "Needs help with bathing and dressing.",
            "Loose rug flagged as a fall risk; caregiver will remove it.",
        ],
    ],
}

# (topic, summary, [(user, bot), ...]) — small pool rotated across patients.
CONV_POOL = [
    ("Medication adherence",
     "Discussed a missed dose and simple routines to improve adherence.",
     [
        ("He sometimes forgets whether he took his pills. What can we do?",
         "A weekly pill organiser and linking the dose to a fixed routine (like after breakfast) really help. Keep a simple tick-chart nearby, and tell your navigator if refusals continue."),
        ("Okay, we'll try the organiser.",
         "Great. Keep it in a visible, consistent place and check it together each day. I'll note this so your navigator can follow up if needed."),
     ]),
    ("Sleep & routine",
     "Advised on evening routine to ease restlessness and improve sleep.",
     [
        ("She's restless in the evenings and doesn't sleep well.",
         "Try keeping lights on before dusk, reducing noise, and a calm, familiar evening routine. Avoid caffeine and long daytime naps — these often help settle restlessness."),
        ("We'll keep the evenings calmer.",
         "Good plan. Soft lighting and a predictable routine make a real difference. Let your navigator know if it doesn't improve within a week."),
     ]),
    ("Caregiver support",
     "Caregiver felt tired; offered reassurance and respite guidance.",
     [
        ("I'm feeling really tired and a bit overwhelmed.",
         "That's completely understandable — caregiving is demanding. Short breaks, sharing tasks with family, and respite services matter for your health too. You're doing important work."),
        ("I don't have much help right now.",
         "You're not alone. I've flagged this so your navigator can talk through local respite and support options with you."),
     ]),
    ("Nutrition & appetite",
     "Discussed reduced appetite and practical mealtime tips.",
     [
        ("He's eating less than usual lately. Should I worry?",
         "Smaller, more frequent meals and familiar favourite foods can help. Keep mealtimes calm and unhurried. If weight loss or refusal continues, tell your navigator so it can be reviewed."),
        ("I'll offer smaller meals more often.",
         "That's a good approach. Gentle encouragement and company at meals often help too."),
     ]),
]

DETAILS_MD_TEMPLATE = """\
**Diagnosis:** Cognitive decline under review; supported at home.

**Key points**
- Followed up on a regular protocol schedule.
- Caregiver involved in daily support.
- Medication reviewed at each call.
"""

CAREGIVER_NAMES = [
    ("Lucía", "Ramos"), ("Miguel", "Torres"), ("Elena", "Ríos"),
    ("Diego", "Salazar"), ("Sofía", "Núñez"), ("Andrés", "Castro"),
]

SELF_REGS = [
    ("Teresa", " Chávez".strip(), "+5199000101", "teresa.chavez@example.com"),
    ("Raúl", "Espinoza", "+5199000102", "raul.espinoza@example.com"),
    ("Gloria", "Palomino", "+5199000103", "gloria.palomino@example.com"),
]


def _book(meeting, number, done=False):
    """Point a seeded meeting at a real protocol, and put the client on it.

    What a call covers is a relation now. Seeding the old integer column left
    meetings whose protocol the panel could not see, and clients whose panel was
    empty because nothing was on their programme.
    """
    protocol = Protocol.objects.filter(number=number).first()
    if protocol is None:
        return
    meeting.scheduled_protocols.add(protocol)
    if done:
        meeting.executed_protocols.add(protocol)
    meeting.patient.protocols.add(protocol)


class Command(BaseCommand):
    help = "Fill demo content across all pages (calls, protocols, conversations, users, self-registrations)."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()
        User = get_user_model()
        navigator = User.objects.filter(is_superuser=True).order_by("id").first()
        agent = Agent.objects.order_by("id").first()
        rec_dir = settings.CALL_RECORDINGS_DIR

        questions_by_proto = {
            n: list(Protocol.objects.get(number=n).questions.all())
            for n in (2, 3) if Protocol.objects.filter(number=n).exists()
        }

        patients = list(
            Patient.objects.exclude(name__iexact=PABLO[0], lastname__iexact=PABLO[1])
            .order_by("id")
        )

        n_rec = n_ans = n_conv = n_cg = n_meet = 0

        for idx, p in enumerate(patients):
            # ── caregiver if missing ──────────────────────────────
            if not p.caregiver:
                fn, ln = CAREGIVER_NAMES[idx % len(CAREGIVER_NAMES)]
                p.caregiver = Caregiver.objects.create(
                    name=fn, lastname=ln,
                    phone_number=f"+51990001{idx:02d}",
                )
                n_cg += 1

            # ── details if empty ──────────────────────────────────
            if not (p.details or "").strip():
                p.details = DETAILS_MD_TEMPLATE
            if not p.agent:
                p.agent = agent
            p.save()

            # ── ensure at least two meetings ──────────────────────
            if p.meetings.count() == 0:
                for k, (days_ago, status_name) in enumerate(
                    [(20, "COMPLETED"), (18, "NOT_ANSWERED"), (-4, "PENDING")]
                ):
                    proto = 2 if (idx + k) % 2 == 0 else 3
                    st = getattr(Meeting.Status, status_name)
                    m = Meeting.objects.create(
                        patient=p,
                        scheduled_time=now - timezone.timedelta(days=days_ago),
                        type=Meeting.MeetingType.REGULAR,
                        status=st,
                    )
                    _book(m, proto, done=st == Meeting.Status.COMPLETED)
                    n_meet += 1

            # ── completed meetings → answers + call recording ─────
            for m in p.meetings.filter(status=Meeting.Status.COMPLETED):
                covered = (list(m.executed_protocols.all())
                           or list(m.scheduled_protocols.all()))
                proto = covered[0].number if covered else (2 if idx % 2 == 0 else 3)
                if not m.executed_protocols.exists():
                    _book(m, proto, done=True)
                for i in range(5):
                    setattr(m, f"step_{i}", True)
                m.save()

                qs = questions_by_proto.get(proto, [])
                if qs and not m.answers.exists():
                    pool = ANSWERS.get(proto, [[]])
                    responses = pool[idx % len(pool)]
                    for q, resp in zip(qs, responses):
                        Answer.objects.create(meeting=m, question=q, response=resp)
                        n_ans += 1

                to_num = str(p.phone_number or "")
                sid = f"SIMREC-{p.pk}-{m.pk}"
                if to_num and not CallRecording.objects.filter(recording_sid=sid).exists():
                    dur = 1800 + (m.pk % 20) * 60  # 30–50 min
                    wav_path = os.path.join(rec_dir, f"{sid}.wav")
                    _make_sine_wav(wav_path, freq=180.0 + (m.pk % 12) * 24.0, seconds=6)
                    CallRecording.objects.create(
                        recording_sid=sid,
                        from_number=PLATFORM_PHONE,
                        to_number=to_num,
                        start_time=m.scheduled_time,
                        end_time=m.scheduled_time + timezone.timedelta(seconds=dur),
                        duration=dur,
                        filename=wav_path,
                        # Attributed at creation, as a real call now is.
                        meeting=m,
                        patient=p,
                        leg=CallRecording.Leg.DYAD,
                    )
                    n_rec += 1

            # ── ensure at least one conversation ──────────────────
            if Conversation.objects.filter(patient=p).count() == 0:
                topic, summary, pairs = CONV_POOL[idx % len(CONV_POOL)]
                day_start = now - timezone.timedelta(days=7 + idx)
                conv = Conversation.objects.create(
                    patient=p, agent=agent, topic=topic, summary=summary,
                    started_at=day_start,
                    last_message_at=day_start + timezone.timedelta(minutes=len(pairs) * 3),
                    analyzed=True, analyzed_at=day_start,
                )
                for j, (um, bm) in enumerate(pairs):
                    msg = Message.objects.create(
                        conversation_id=str(conv.id),
                        user=str(p.phone_number or p.pk),
                        user_message=um, response_message=bm,
                    )
                    Message.objects.filter(pk=msg.pk).update(
                        timestamp=day_start + timezone.timedelta(minutes=j * 3)
                    )
                n_conv += 1

        # ── tester account linked 1:1 to first patient ────────────
        tester_grp, _ = Group.objects.get_or_create(name="PatientTester")
        first_patient = patients[0] if patients else None
        if first_patient and not first_patient.tester_account:
            tuser, created = User.objects.get_or_create(
                username="tester",
                defaults={"first_name": "Demo", "last_name": "Tester"},
            )
            if created:
                tuser.set_password("tester1234")
                tuser.save()
            tuser.groups.add(tester_grp)
            first_patient.tester_account = tuser
            if not first_patient.agent:
                first_patient.agent = agent
            first_patient.save()

        # ── extra navigator user for the Users page ───────────────
        nav_grp, _ = Group.objects.get_or_create(name="Navigator")
        nav2, created = User.objects.get_or_create(
            username="navigator2",
            defaults={"first_name": "Ana", "last_name": "Navigator"},
        )
        if created:
            nav2.set_password("navigator1234")
            nav2.save()
        nav2.groups.add(nav_grp)

        # ── user-level alerts (assigned to a navigator) ───────────
        if navigator:
            for title, prio, status, desc in [
                ("Weekly caseload review due", "MEDIUM", "CREATED",
                 "Several patients have upcoming protocol calls this week — review the calendar."),
                ("Follow up on missed call", "LOW", "IN_PROGRESS",
                 "A scheduled call was not answered; reschedule and confirm with the caregiver."),
            ]:
                if not Alert.objects.filter(title=title, user=navigator).exists():
                    Alert.objects.create(
                        user=navigator, title=title, description=desc,
                        priority=getattr(Alert.Priority, prio),
                        status=getattr(Alert.AlertStatus, status),
                        alert_type=Alert.AlertType.DEFAULT,
                        created_by=navigator,
                    )

        # ── dashboard liveliness (today's calls, msgs, important convs) ──
        # A few PENDING calls scheduled for *today* so the dashboard's
        # "calls today / overdue / next" cards have content.
        n_today = 0
        if navigator:
            nav_patients = [p for p in patients if p.navigator_id == navigator.id]
            have_today = Meeting.objects.filter(
                patient__navigator=navigator,
                scheduled_time__date=now.date(),
                status=Meeting.Status.PENDING,
            ).exists()
            if not have_today and nav_patients:
                offsets = [-2, 1, 3]  # hours relative to now: one overdue, two upcoming
                for k, hrs in enumerate(offsets):
                    tp = nav_patients[k % len(nav_patients)]
                    today_m = Meeting.objects.create(
                        patient=tp,
                        scheduled_time=now + timezone.timedelta(hours=hrs),
                        type=Meeting.MeetingType.REGULAR,
                        status=Meeting.Status.PENDING,
                    )
                    _book(today_m, 2 if k % 2 == 0 else 3)
                    n_today += 1

            # Some messages timestamped *today* so the message-trend chart isn't flat.
            todays_msgs = Message.objects.filter(
                timestamp__date=now.date(),
                user__in=[str(p.phone_number) for p in nav_patients if p.phone_number],
            ).exists()
            if not todays_msgs and nav_patients:
                start = now.replace(hour=9, minute=0, second=0, microsecond=0)
                snippets = [
                    ("Good morning, he slept a little better last night.",
                     "That's good to hear. Keeping the calm evening routine going should help."),
                    ("She took her medication on time today.",
                     "Well done — consistency really matters. Keep using the organiser."),
                    ("Quick question about his afternoon walk.",
                     "Happy to help — a short, familiar route with company is ideal."),
                ]
                for k, (um, bm) in enumerate(snippets):
                    tp = nav_patients[k % len(nav_patients)]
                    conv = Conversation.objects.create(
                        patient=tp, agent=agent, topic="Daily check-in",
                        summary="Brief daily check-in from the caregiver.",
                        started_at=start, last_message_at=start,
                    )
                    msg = Message.objects.create(
                        conversation_id=str(conv.id),
                        user=str(tp.phone_number or tp.pk),
                        user_message=um, response_message=bm,
                    )
                    Message.objects.filter(pk=msg.pk).update(
                        timestamp=start + timezone.timedelta(hours=k * 3)
                    )

        # A couple of conversations flagged important & unvisited so the
        # dashboard's "needs attention" panel is populated.
        if not Conversation.objects.filter(is_important=True, visited=False).exists():
            for conv in Conversation.objects.filter(
                patient__navigator=navigator
            ).order_by("-last_message_at")[:2]:
                conv.is_important = True
                conv.visited = False
                conv.save(update_fields=["is_important", "visited"])

        # ── self-registrations ────────────────────────────────────
        n_selfreg = 0
        for i, (fn, ln, phone, email) in enumerate(SELF_REGS):
            obj, created = SelfRegistration.objects.get_or_create(
                name=fn, lastname=ln,
                defaults={
                    "phone_number": phone, "email": email,
                    "state": SelfRegistration.State.APPROVED if i == 0
                    else SelfRegistration.State.REGISTERED,
                },
            )
            if created:
                n_selfreg += 1

        self.stdout.write(self.style.SUCCESS(
            f"Filled demo content across {len(patients)} patients: "
            f"+{n_rec} recordings, +{n_ans} answers, +{n_conv} conversations, "
            f"+{n_cg} caregivers, +{n_meet} meetings, +{n_selfreg} self-registrations. "
            f"tester/navigator2 users ensured. Navigator={navigator}."
        ))
