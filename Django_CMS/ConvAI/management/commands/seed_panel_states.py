"""
Seed one client whose records cover every state of the detail panel.

The panel changes shape with what it is describing — a call still to be made,
one that happened and was transcribed, one that happened and was not, an
in-person meeting, an alert, a chat. Checking a change to it meant hunting for a
client that happened to be in the right state, and some states had no example at
all. This makes one client that is in all of them at once.

Everything hangs off a single client, "Elena Marchetti", so the whole set is one
row in the client list and one delete to remove.

Re-running replaces this client's records rather than adding to them: it
clears everything hanging off the seeded client first, then rebuilds.

    docker compose exec web python manage.py seed_panel_states
    docker compose exec web python manage.py seed_panel_states --first Marco --last "Da Re" \
        --phone +390512223344 --caregiver-phone +390512223345
    docker compose exec web python manage.py seed_panel_states --remove
"""

import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ConvAI.models import (
    Alert,
    Answer,
    CallRecording,
    Caregiver,
    Conversation,
    Meeting,
    Message,
    Note,
    Patient,
    Protocol,
    Question,
)
from ConvAI.management.commands.seed_demo_fill import _make_sine_wav
from ConvAI.roles import NAVIGATOR

FIRST, LAST = "Elena", "Marchetti"
PHONE = "+390511234567"
CAREGIVER_PHONE = "+390511234568"
PLATFORM_PHONE = "+14155550100"

# A protocol small enough that the counts on screen can be checked by eye.
PROTOCOL_NUM = 2
QUESTIONS = [
    "How have you been sleeping this week?",
    "Any dizziness or unsteadiness?",
    "Has your appetite changed?",
]
ANSWERS = [
    "Waking around three, then unable to get back to sleep. Two nights out of seven.",
    "Twice, on standing up from the chair. No falls.",
    "No change. Eating normally.",
]

# Segments as Whisper returns them once verbose_json is asked for. Speaker is
# filled here to show the two-channel case; a mono recording leaves it null and
# the panel simply renders no label.
SEGMENTS = [
    (12.0, 19.5, 1, "Good morning Elena, how have you been sleeping this week?"),
    (21.0, 27.4, 2, "Not so well. I wake up around three and then I just lie there."),
    (64.0, 70.2, 1, "And the dizziness you mentioned last time, has that come back?"),
    (72.0, 77.8, 2, "Yes, twice. When I stand up too quickly from the chair."),
    (86.0, 91.3, 1, "Did you have to sit back down, or hold on to anything?"),
    (94.0, 100.6, 2, "I held the back of the chair. It passes after a moment."),
    (132.0, 138.9, 1, "Thank you. I will make a note of that for the care team."),
    (341.0, 349.2, 2, "The other thing is the afternoon pill. Could I take it at two?"),
    (352.0, 360.5, 1, "I will pass that to them. Keep to four until someone confirms."),
    (508.0, 515.1, 2, "Alright. And my appetite is fine, I am eating normally."),
]

MOMENTS = [
    ("Dizziness on standing, twice since Monday. No falls.", 3),
    ("Waking around three and unable to get back to sleep.", 1),
    ("Asked to move the 16:00 dose to 14:00. Referred to the care team.", 7),
]

TRANSCRIPT = (
    "Navigator: Good morning Elena, how have you been sleeping this week?\n"
    "Elena: Not so well. I wake up around three and then I just lie there.\n"
    "Navigator: And the dizziness you mentioned last time, has that come back?\n"
    "Elena: Yes, twice. When I stand up too quickly from the chair.\n"
    "Navigator: Did you have to sit back down, or hold on to anything?\n"
    "Elena: I held the back of the chair. It passes after a moment.\n"
    "Navigator: Thank you. I will make a note of that for the care team."
)

SUMMARY = (
    "Elena reported two nights of disturbed sleep and mild dizziness on standing. "
    "Medication was taken as prescribed. She asked whether the afternoon dose can "
    "be moved earlier; this was referred to the care team."
)

CHAT = [
    ("Can I take the afternoon pill at two instead of four?",
     "I can pass that on to your care team. For now it is best to keep to the "
     "schedule you were given. Have you noticed anything different when you take it at four?"),
    ("I feel a bit lightheaded in the mornings, not so much later.",
     "Thank you for telling me. I have flagged this for the care team so they can "
     "look at it before your next call."),
    ("Alright. Will someone call me?",
     "Yes, your next call is booked for Friday. I have added a note about this for "
     "whoever calls."),
]


class Command(BaseCommand):
    help = "Seed one client covering every detail-panel state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--remove", action="store_true",
            help="Delete the seeded client and everything hanging off it.",
        )
        parser.add_argument("--first", default=FIRST, help="Client's first name.")
        parser.add_argument("--last", default=LAST, help="Client's last name.")
        parser.add_argument("--phone", default=PHONE, help="Client's phone number, E.164.")
        parser.add_argument("--caregiver-phone", default=CAREGIVER_PHONE)
        parser.add_argument(
            "--navigator", default="",
            help=("Username of the navigator the client is assigned to. "
                  "Defaults to the first user in the Navigator group, then to "
                  "any superuser. The client has to belong to someone: "
                  "permission everywhere is decided by the assigned navigator, "
                  "so an unassigned one is invisible to every non-admin."),
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        # Held on the instance so the helpers below do not each need them passed
        # through; one command run is one client.
        self.first = opts["first"]
        self.last = opts["last"]
        self.phone = opts["phone"]
        self.caregiver_phone = opts["caregiver_phone"]
        self.navigator = self._navigator(opts["navigator"])

        if opts["remove"]:
            return self._remove()

        # Times are snapped to a working hour rather than offset from the
        # current minute: a demo where every call is at 23:55 reads as broken,
        # and a scheduled_time derived from "now" never matches on a re-run,
        # which is what made the first version duplicate everything instead of
        # updating it.
        today = timezone.localtime(timezone.now()).replace(
            hour=10, minute=30, second=0, microsecond=0)
        now = today

        patient = self._patient()
        protocol = self._protocol()
        self._clear(patient)

        made = []

        # 1 ── A call still to be made. Summary and Transcript both show their
        #      placeholder; Protocols is the tab that opens.
        pending = self._meeting(
            patient, now + timezone.timedelta(days=2, hours=1),
            Meeting.Status.PENDING, modality=0, tag="pending-call",
        )
        made.append("scheduled call, nothing answered yet")

        # 2 ── A call that happened, was recorded, transcribed and summarised.
        #      Every tab has content.
        done = self._meeting(
            patient, now - timezone.timedelta(days=7),
            Meeting.Status.COMPLETED, modality=0, tag="completed-call",
        )
        self._answers(done, protocol, ANSWERS)
        done.protocol_summary = SUMMARY
        done.save(update_fields=["protocol_summary"])
        self._notes(done, [
            "Client asked about moving the afternoon dose. Passed to the care team.",
            "Sleep pattern to be reviewed again next week.",
        ])
        self._recording(done, transcript=TRANSCRIPT)
        made.append("completed call: segments, key moments, summary, notes")

        # 3 ── A call that happened and was recorded but never transcribed, so
        #      the Transcript tab shows the recording without the text.
        untranscribed = self._meeting(
            patient, now - timezone.timedelta(days=14),
            Meeting.Status.COMPLETED, modality=0, tag="untranscribed-call",
        )
        self._answers(untranscribed, protocol, ANSWERS[:2])
        self._recording(untranscribed, transcript="")
        made.append("completed call, recorded but not transcribed")

        # 4 ── A completed call with no recording at all.
        norec = self._meeting(
            patient, now - timezone.timedelta(days=21),
            Meeting.Status.COMPLETED, modality=0, tag="no-recording-call",
        )
        self._answers(norec, protocol, ANSWERS[:1])
        made.append("completed call with no recording")

        # 5 ── An in-person meeting: no Transcript tab at all.
        visit = self._meeting(
            patient, now - timezone.timedelta(days=5),
            Meeting.Status.COMPLETED, modality=1, tag="in-person-visit",
        )
        visit.location = "Via Alberti 12, Bologna"
        self._answers(visit, protocol, ANSWERS)
        visit.protocol_summary = (
            "Home visit completed. Elena had prepared her medication list in advance. "
            "The hallway rug was identified as a trip hazard and has been rolled up."
        )
        visit.save(update_fields=["location", "protocol_summary"])
        self._notes(visit, ["Rug removed from hallway. Daughter present for the second half."])
        made.append("in-person meeting, completed")

        # 6 ── An in-person meeting still to come, so the empty summary shows
        #      the meeting wording rather than the call wording.
        self._meeting(
            patient, now + timezone.timedelta(days=9),
            Meeting.Status.PENDING, modality=1, tag="pending-visit",
        )
        made.append("in-person meeting, upcoming")

        # 7 ── A chat, which the chat panel reads by day.
        self._conversation(patient, now - timezone.timedelta(days=8))
        made.append("chat conversation")

        # 8 ── An alert raised from that chat.
        self._alert(patient, now - timezone.timedelta(days=8))
        made.append("alert, in review")

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {patient.name} {patient.lastname} ({patient.phone_number}) "
            f"for navigator {self.navigator.get_username()}"
        ))
        for line in made:
            self.stdout.write(f"  · {line}")
        self.stdout.write(
            "\nOpen the client, then the calls list, the alerts list and the "
            "conversation to see each panel."
        )

    # ────────────────────────────── pieces ──────────────────────────────
    def _clear(self, patient):
        """Wipe this client's seeded records so a re-run replaces rather than adds.

        Scoped to the seeded client only — nothing else in the database is
        touched. Cheaper and far more predictable than trying to match each
        record back to the one that made it.
        """
        from ConvAI.models import Note
        Note.objects.filter(meeting__patient=patient).delete()
        Note.objects.filter(alert__patient=patient).delete()
        sids = ["PANELSEED%06d" % m.pk for m in Meeting.objects.filter(patient=patient)]
        CallRecording.objects.filter(recording_sid__in=sids).delete()
        Meeting.objects.filter(patient=patient).delete()
        Alert.objects.filter(patient=patient).delete()
        for conv in Conversation.objects.filter(patient=patient):
            Message.objects.filter(conversation_id=str(conv.id)).delete()
            Note.objects.filter(conversation=conv).delete()
        Conversation.objects.filter(patient=patient).delete()
        # Also anything addressed to this client's or caregiver's number, so a
        # stray exchange from testing the chatbot does not sit alongside the
        # seeded one and make the demo ambiguous. Still scoped to this client.
        numbers = [n for n in (str(patient.phone_number or ''),
                               str(getattr(patient.caregiver, 'phone_number', '') or '')) if n]
        if numbers:
            Message.objects.filter(user__in=numbers).delete()

    def _navigator(self, username):
        """Who the seeded client belongs to.

        Not optional in practice. Every permission check in the app asks whether
        the client's navigator is you, so a client with none is one that only an
        admin can open — which is the opposite of what a fixture for exercising
        the panel is for. Notes take their author from here too.
        """
        User = get_user_model()
        if username:
            try:
                return User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError(f"No user named {username!r}.")
        who = (User.objects.filter(groups__name=NAVIGATOR).order_by("pk").first()
               or User.objects.filter(is_superuser=True).order_by("pk").first())
        if not who:
            raise CommandError(
                "No navigator and no superuser to assign the client to. "
                "Create one first, or pass --navigator <username>."
            )
        return who

    def _patient(self):
        patient, _ = Patient.objects.get_or_create(
            phone_number=self.phone,
            defaults={"name": self.first, "lastname": self.last},
        )
        patient.name, patient.lastname = self.first, self.last
        patient.navigator = self.navigator
        patient.save(update_fields=["name", "lastname", "navigator"])

        if not patient.caregiver:
            caregiver, _ = Caregiver.objects.get_or_create(
                phone_number=self.caregiver_phone,
                defaults={"name": "Giulia", "lastname": self.last},
            )
            patient.caregiver = caregiver
            patient.save(update_fields=["caregiver"])
        return patient

    def _protocol(self):
        """A protocol with three questions, so 0/3, 1/3 and 3/3 are all visible."""
        protocol, _ = Protocol.objects.get_or_create(
            number=PROTOCOL_NUM,
            defaults={"title": "Weekly wellbeing", "description": "A short weekly check."},
        )
        for order, prompt in enumerate(QUESTIONS, start=1):
            Question.objects.get_or_create(
                protocol=protocol, order=order, defaults={"prompt_md": prompt},
            )
        return protocol

    def _meeting(self, patient, when, status, modality, tag):
        meeting = Meeting(patient=patient, scheduled_time=when)
        meeting.type = Meeting.MeetingType.REGULAR
        meeting.status = status
        meeting.modality = modality
        meeting.scheduled_protocol = PROTOCOL_NUM
        if status == Meeting.Status.COMPLETED:
            meeting.executed_protocol = PROTOCOL_NUM
        meeting.save()
        return meeting

    def _answers(self, meeting, protocol, responses):
        for question, response in zip(protocol.questions.order_by("order"), responses):
            Answer.objects.update_or_create(
                meeting=meeting, question=question, defaults={"response": response},
            )

    def _notes(self, meeting, bodies):
        """Notes as rows, with the navigator as the author."""
        for body in bodies:
            Note.objects.create(meeting=meeting, body=body, author=meeting.patient.navigator)

    def _recording(self, meeting, transcript):
        """A real playable WAV, so the player in the panel is not a dead control."""
        sid = f"PANELSEED{meeting.pk:06d}"  # meeting pk is unique across clients
        rec_dir = os.path.join(settings.MEDIA_ROOT, "call_recordings")
        wav_path = os.path.join(rec_dir, f"{sid}.wav")
        if not os.path.exists(wav_path):
            _make_sine_wav(wav_path, freq=196.0, seconds=6)

        duration = 762
        rec = CallRecording.objects.create(
            recording_sid=sid,
            **{
                "from_number": PLATFORM_PHONE,
                "to_number": str(meeting.patient.phone_number),
                "start_time": meeting.scheduled_time,
                "end_time": meeting.scheduled_time + timezone.timedelta(seconds=duration),
                "duration": duration,
                "filename": wav_path,
                "transcript": transcript,
                "transcript_segments": [
                    {"start": a, "end": b, "speaker": sp, "text": txt}
                    for a, b, sp, txt in SEGMENTS
                ] if transcript else [],
                "transcript_moments": [
                    {"text": txt, "segment": i, "start": SEGMENTS[i][0]}
                    for txt, i in MOMENTS
                ] if transcript else [],
                "transcript_summary": SUMMARY if transcript else "",
                "transcribed_at": timezone.now() if transcript else None,
            },
        )
        return rec

    def _conversation(self, patient, day):
        """analyzed=True matters: the summary card is gated on it, so without it
        the chat panel shows the "classification pending" state instead."""
        conv = Conversation(patient=patient)
        conv.topic = "Medication"
        conv.summary = (
            "Elena asked twice about moving her afternoon medication earlier and "
            "mentioned feeling lightheaded in the mornings. The agent kept her to "
            "the current schedule and flagged the exchange for the care team."
        )
        conv.is_important = True
        conv.analyzed = True
        conv.started_at = day
        conv.last_message_at = day + timezone.timedelta(minutes=2 * len(CHAT))
        conv.save()

        # The chat panel finds messages by phone number and day, so both have to
        # be right. timestamp is auto_now_add, so it is backdated after insert.
        for i, (asked, replied) in enumerate(CHAT):
            msg = Message.objects.create(
                conversation_id=str(conv.id),
                user=str(patient.phone_number),
                user_message=asked,
                response_message=replied,
            )
            Message.objects.filter(pk=msg.pk).update(
                timestamp=day + timezone.timedelta(minutes=2 * i)
            )
        return conv

    def _alert(self, patient, when):
        alert = Alert(patient=patient, title="Repeated dizziness reported")
        alert.description = (
            "Dizziness on standing reported twice in one week. No falls recorded, "
            "but the pattern has not improved since the last medication change."
        )
        alert.priority = Alert.Priority.HIGH
        alert.status = Alert.AlertStatus.IN_PROGRESS
        alert.alert_type = Alert.AlertType.CONVERSATION
        alert.save()
        # created_at is auto_now_add, same story as the messages.
        Alert.objects.filter(pk=alert.pk).update(created_at=when)
        # A note is a row, so the alert arrives with one in its Notes tab —
        # the same record the panel and the alert page both write.
        Note.objects.create(
            alert=alert, body="Discussed with the care team. Dose moved to 14:00.",
            author=patient.navigator,
        )
        return alert

    # ────────────────────────────── removal ─────────────────────────────
    def _remove(self):
        patient = Patient.objects.filter(phone_number=self.phone).first()
        if not patient:
            self.stdout.write("Nothing seeded to remove.")
            return
        sids = ["PANELSEED%06d" % m.pk for m in Meeting.objects.filter(patient=patient)]
        CallRecording.objects.filter(recording_sid__in=sids).delete()
        Message.objects.filter(user=str(patient.phone_number)).delete()
        name = f"{patient.name} {patient.lastname}"
        patient.delete()
        self.stdout.write(self.style.SUCCESS(f"Removed {name} and everything under it."))
