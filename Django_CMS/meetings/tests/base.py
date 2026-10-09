"""Shared fixtures for the meetings tests. LiveKit is never contacted: every
server call goes through livekit_api._twirp, which the tests patch."""
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from ConvAI.models import Caregiver, ConvAIUser, Meeting, Patient, Protocol, Question

from meetings.models import MeetingsSettings

LK = dict(
    LIVEKIT_URL="wss://lk.example.org",
    LIVEKIT_API_URL="http://livekit:7880",
    LIVEKIT_API_KEY="APIkey123",
    LIVEKIT_API_SECRET="secret-secret-secret-secret-secret-1234",
    MEETINGS_SERVICE_KEY="svc-key",
    MEETINGS_PUBLIC_BASE_URL="https://care.example.org",
    JOBS_EAGER=False,
    JOBS_RUNNER="worker",
)


def navigator(username="nav"):
    user = ConvAIUser.objects.create_user(username=username, password="x",
                                          first_name=username.title())
    user.groups.add(Group.objects.get_or_create(name="Navigator")[0])
    return user


def fake_twirp(service, method, body, grants):
    if method == "CreateDispatch":
        return {"id": "AD_test"}
    if method == "ListParticipants":
        return {"participants": []}
    return {}


@override_settings(**LK)
class MeetingsTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        # The app reads its connection settings from the environment before
        # Django settings, and urls.py loads .env into the environment. On a
        # server with online meetings configured, the real keys would win over
        # override_settings — so the test values go into the environment too.
        env = mock.patch.dict(os.environ, {k: v for k, v in LK.items() if isinstance(v, str)})
        env.start()
        self.addCleanup(env.stop)
        s = MeetingsSettings.load()
        s.enabled = "1"
        s.save()
        self.nav = navigator()
        self.carer = Caregiver.objects.create(name="Ana", lastname="Ortega",
                                              phone_number="+447222222222",
                                              email="ana@example.org")
        self.patient = Patient.objects.create(name="Manuel", lastname="Ortega",
                                              caregiver=self.carer, navigator=self.nav)
        self.meeting = Meeting.objects.create(
            patient=self.patient, scheduled_time=timezone.now() + timedelta(minutes=5),
            modality=Meeting.Modality.ONLINE)
        self.protocol = Protocol.objects.create(number=1, title="Check-in")
        self.q1 = Question.objects.create(protocol=self.protocol, order=1, prompt_md="How is **sleep**?")
        self.q2 = Question.objects.create(protocol=self.protocol, order=2, prompt_md="Meals?")
        self.twirp = mock.patch("meetings.livekit_api._twirp", side_effect=fake_twirp)
        self.twirp_mock = self.twirp.start()
        self.addCleanup(self.twirp.stop)

    def set_enabled(self, value):
        s = MeetingsSettings.load()
        s.enabled = value
        s.save()
