"""LiveKit webhooks: signed, idempotent, and keeping the register."""
import base64
import hashlib
import json
import time

import jwt
from django.urls import reverse

from meetings import rooms
from meetings.models import AgentRun, Attendance, MeetingSession

from .base import LK, MeetingsTestCase


def _signed(body: bytes, *, secret=None, key=None):
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
    token = jwt.encode({"iss": key or LK["LIVEKIT_API_KEY"], "sha256": digest,
                        "exp": int(time.time()) + 60, "nbf": int(time.time()) - 5},
                       secret or LK["LIVEKIT_API_SECRET"], algorithm="HS256")
    return token


class Webhooks(MeetingsTestCase):
    def setUp(self):
        super().setUp()
        self.session = rooms.start(self.meeting, self.nav)
        self.url = reverse("meetings:webhook")

    def _post(self, event, *, tamper=False, secret=None):
        body = json.dumps(event).encode()
        auth = _signed(body, secret=secret)
        if tamper:
            body = body.replace(b"participant", b"participanx", 1)
        return self.client.post(self.url, body, content_type="application/webhook+json",
                                HTTP_AUTHORIZATION=auth)

    def _joined(self, identity, sid, attrs=None, event_id="EV_1", kind=None):
        p = {"sid": sid, "identity": identity, "name": "Ana", "attributes": attrs or {},
             "joinedAt": str(int(time.time()))}
        if kind:
            p["kind"] = kind
        return {"event": "participant_joined", "id": event_id,
                "room": {"name": self.session.room_name, "sid": "RM_1"}, "participant": p}

    def test_signed_event_is_recorded_once(self):
        ev = self._joined(f"staff-{self.nav.pk}", "PA_1")
        self.assertEqual(self._post(ev).status_code, 200)
        self.assertEqual(self._post(ev).status_code, 200)  # redelivery
        self.assertEqual(Attendance.objects.count(), 1)
        att = Attendance.objects.get()
        self.assertEqual(att.kind, Attendance.Kind.STAFF)
        self.assertEqual(att.user_id, self.nav.pk)

    def test_bad_signature_and_tampered_body_are_refused(self):
        ev = self._joined("inv-x", "PA_2")
        self.assertEqual(self._post(ev, secret="wrong-secret-wrong-secret-wrong-1234").status_code, 401)
        self.assertEqual(self._post(ev, tamper=True).status_code, 401)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_left_closes_the_attendance(self):
        self._post(self._joined("inv-abc", "PA_3"))
        left = self._joined("inv-abc", "PA_3", event_id="EV_2")
        left["event"] = "participant_left"
        self._post(left)
        self.assertIsNotNone(Attendance.objects.get().left_at)

    def test_recorder_leaving_mid_meeting_is_replaced(self):
        self._post(self._joined("inv-abc", "PA_4", event_id="EV_a"))
        run = AgentRun.objects.get(role=AgentRun.Role.SCRIBE)
        AgentRun.objects.filter(pk=run.pk).update(agent_identity="agent-scribe-1",
                                                  state=AgentRun.State.RUNNING)
        self._post(self._joined("agent-scribe-1", "PA_5", {"cc.role": "scribe"}, event_id="EV_b", kind="AGENT"))
        left = self._joined("agent-scribe-1", "PA_5", {"cc.role": "scribe"}, event_id="EV_c", kind="AGENT")
        left["event"] = "participant_left"
        self._post(left)
        self.assertEqual(AgentRun.objects.filter(role=AgentRun.Role.SCRIBE).count(), 2)

    def test_room_finished_ends_the_session(self):
        self._post({"event": "room_finished", "id": "EV_9",
                    "room": {"name": self.session.room_name}})
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, MeetingSession.Status.ENDED)

    def test_other_rooms_are_ignored(self):
        resp = self._post({"event": "room_finished", "id": "EV_10", "room": {"name": "someone-else"}})
        self.assertEqual(resp.status_code, 200)
