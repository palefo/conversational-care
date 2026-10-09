"""Opening and closing rooms, the lobby, staff permissions, and keeping the
room in step with what happens to the meeting."""
from unittest import mock

import jwt
from django.urls import reverse

from ConvAI.jobs import latest_for
from ConvAI.models import Meeting, Patient
from meetings import invites, links, rooms
from meetings.models import AgentRun, Attendance, LobbyEntry, MeetingSession

from .base import LK, MeetingsTestCase, navigator


def _claims(token):
    return jwt.decode(token, LK["LIVEKIT_API_SECRET"], algorithms=["HS256"],
                      options={"verify_aud": False})


class Rooms(MeetingsTestCase):
    def test_start_is_idempotent_and_dispatches_the_recorder(self):
        a = rooms.start(self.meeting, self.nav)
        b = rooms.start(self.meeting, self.nav)
        self.assertEqual(a.pk, b.pk)
        methods = [c.args[1] for c in self.twirp_mock.call_args_list]
        self.assertEqual(methods.count("CreateRoom"), 1)
        self.assertEqual(methods.count("CreateDispatch"), 1)
        self.assertEqual(AgentRun.objects.get().role, AgentRun.Role.SCRIBE)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.retries, 1)  # so "outcome missing" works

    def test_room_names_carry_no_personal_data(self):
        s = rooms.start(self.meeting, self.nav)
        self.assertTrue(s.room_name.startswith("cc-"))
        self.assertNotIn("Manuel", s.room_name)

    def test_capacity_is_enforced(self):
        from meetings.models import MeetingsSettings
        cfg = MeetingsSettings.load(); cfg.max_live_rooms = 1; cfg.save()
        rooms.start(self.meeting, self.nav)
        other = Meeting.objects.create(patient=self.patient, scheduled_time=self.meeting.scheduled_time,
                                       modality=Meeting.Modality.ONLINE)
        with self.assertRaises(rooms.MeetingError):
            rooms.start(other, self.nav)

    def test_off_refuses_new_rooms_but_lets_a_live_one_finish(self):
        s = rooms.start(self.meeting, self.nav)
        self.set_enabled("0")
        self.assertEqual(rooms.start(self.meeting, self.nav).pk, s.pk)
        other = Meeting.objects.create(patient=self.patient, scheduled_time=self.meeting.scheduled_time,
                                       modality=Meeting.Modality.ONLINE)
        with self.assertRaises(rooms.MeetingError):
            rooms.start(other, self.nav)

    def test_end_closes_everything_and_queues_the_mixdown(self):
        s = rooms.start(self.meeting, self.nav)
        rooms.end(s, reason="test")
        s.refresh_from_db()
        self.assertEqual(s.status, MeetingSession.Status.ENDED)
        self.assertFalse(AgentRun.objects.filter(state__in=AgentRun.LIVE_STATES).exists())
        self.assertIn("DeleteRoom", [c.args[1] for c in self.twirp_mock.call_args_list])
        self.assertEqual(latest_for(f"meetingsession:{s.pk}").kind, "meetings.finalize_recording")

    def test_tokens(self):
        s = rooms.start(self.meeting, self.nav)
        staff = _claims(rooms.staff_token(s, self.nav))
        self.assertEqual(staff["sub"], f"staff-{self.nav.pk}")
        self.assertEqual(staff["video"]["room"], s.room_name)
        self.assertFalse(staff["video"]["canUpdateOwnMetadata"])
        self.assertTrue(staff["video"]["canPublishData"])
        inv = invites.primary_invite(self.meeting, create=True)
        guest = _claims(rooms.invitee_token(s, inv))
        self.assertEqual(guest["sub"], inv.identity)
        self.assertFalse(guest["video"]["canPublishData"])
        self.assertEqual(guest["attributes"]["cc.role"], "caregiver")
        self.assertEqual(guest["attributes"]["cc.record"], "1")
        self.assertNotIn("roomCreate", guest["video"])


class Lobby(MeetingsTestCase):
    def setUp(self):
        super().setUp()
        self.invite = invites.primary_invite(self.meeting, create=True)
        self.client.post(links.path_for(self.invite))

    def test_no_host_then_waiting_then_admitted(self):
        r = self.client.post(reverse("meetings:knock")).json()
        self.assertEqual(r["state"], "no_host")
        s = rooms.start(self.meeting, self.nav)
        self.assertEqual(self.client.get(reverse("meetings:lobby_status")).json()["state"], "waiting")
        rooms.decide(s, self.invite, admit=True, by=self.nav)
        r = self.client.get(reverse("meetings:lobby_status")).json()
        self.assertEqual(r["state"], "admitted")
        self.assertEqual(_claims(r["token"])["sub"], self.invite.identity)
        self.assertEqual(r["ws_url"], "wss://lk.example.org")

    def test_no_token_before_admission(self):
        rooms.start(self.meeting, self.nav)
        r = self.client.post(reverse("meetings:knock")).json()
        self.assertEqual(r["state"], "waiting")
        self.assertNotIn("token", r)

    def test_denied_and_asking_again(self):
        s = rooms.start(self.meeting, self.nav)
        self.client.post(reverse("meetings:knock"))
        rooms.decide(s, self.invite, admit=False, by=self.nav)
        self.assertEqual(self.client.get(reverse("meetings:lobby_status")).json()["state"], "denied")
        self.assertEqual(self.client.post(reverse("meetings:knock")).json()["state"], "waiting")

    def test_auto_admit_once_the_host_is_in(self):
        s = rooms.start(self.meeting, self.nav)
        MeetingSession.objects.filter(pk=s.pk).update(auto_admit=True)
        Attendance.objects.create(session=s, identity=f"staff-{self.nav.pk}", participant_sid="PA1",
                                  kind=Attendance.Kind.STAFF)
        self.assertEqual(self.client.post(reverse("meetings:knock")).json()["state"], "admitted")

    def test_knock_records_the_recording_notice(self):
        rooms.start(self.meeting, self.nav)
        self.client.post(reverse("meetings:knock"))
        self.assertEqual(self.invite.consents.count(), 1)


class StaffViews(MeetingsTestCase):
    def test_room_page_and_start(self):
        self.client.force_login(self.nav)
        page = self.client.get(reverse("meetings:staff_room", args=[self.meeting.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertIn("camera=(self)", page["Permissions-Policy"])
        r = self.client.post(reverse("meetings:start", args=[self.meeting.pk])).json()
        self.assertTrue(r["ok"])
        self.assertEqual(_claims(r["token"])["sub"], f"staff-{self.nav.pk}")

    def test_another_navigator_cannot_see_it(self):
        other = navigator("other")
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("meetings:staff_room", args=[self.meeting.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("meetings:start", args=[self.meeting.pk])).status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        resp = self.client.get(reverse("meetings:staff_room", args=[self.meeting.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_interview_needs_a_room_and_a_respondent(self):
        self.client.force_login(self.nav)
        url = reverse("meetings:interview_start", args=[self.meeting.pk])
        self.assertEqual(self.client.post(url, {"protocol": 1, "respondent": "inv-x"}).status_code, 409)
        self.client.post(reverse("meetings:start", args=[self.meeting.pk]))
        self.assertEqual(self.client.post(url, {"protocol": 1}).status_code, 400)
        r = self.client.post(url, {"protocol": 1, "respondent": "inv-x", "respondent_name": "Ana"})
        self.assertEqual(r.status_code, 200)
        run = AgentRun.objects.get(role=AgentRun.Role.INTERVIEWER)
        self.assertEqual(run.respondent_identity, "inv-x")
        self.assertEqual(run.controller_identity, f"staff-{self.nav.pk}")
        # Not twice at once.
        self.assertEqual(self.client.post(url, {"protocol": 1, "respondent": "inv-x"}).status_code, 409)

    def test_interview_refused_while_out_by_text(self):
        self.client.force_login(self.nav)
        self.client.post(reverse("meetings:start", args=[self.meeting.pk]))
        from django.utils import timezone
        Patient.objects.filter(pk=self.patient.pk).update(
            automation_meeting=self.meeting, automation_protocol=1,
            automation_expires_at=timezone.now() + timezone.timedelta(hours=1))
        r = self.client.post(reverse("meetings:interview_start", args=[self.meeting.pk]),
                             {"protocol": 1, "respondent": "inv-x"})
        self.assertEqual(r.status_code, 409)

    def test_text_automation_refused_while_interviewing(self):
        self.client.force_login(self.nav)
        self.client.post(reverse("meetings:start", args=[self.meeting.pk]))
        self.client.post(reverse("meetings:interview_start", args=[self.meeting.pk]),
                         {"protocol": 1, "respondent": "inv-x"})
        from django.test import override_settings
        with mock.patch("ConvAI.views.protocols.get_bool", return_value=True), \
             mock.patch("ConvAI.views.protocols.send_whatsapp_text_result") as send:
            self.client.post(reverse("start_protocol_automation", args=[self.meeting.pk, 1]))
        send.assert_not_called()

    def test_state_lists_the_lobby(self):
        self.client.force_login(self.nav)
        self.client.post(reverse("meetings:start", args=[self.meeting.pk]))
        inv = invites.primary_invite(self.meeting, create=True)
        rooms.knock(inv)
        data = self.client.get(reverse("meetings:state", args=[self.meeting.pk])).json()
        self.assertEqual(data["lobby"][0]["name"], "Ana")
        self.client.post(reverse("meetings:lobby_decide", args=[self.meeting.pk, inv.public_id, "admit"]))
        self.assertEqual(LobbyEntry.objects.get().state, LobbyEntry.State.ADMITTED)

    def test_send_link_by_email(self):
        self.client.force_login(self.nav)
        with mock.patch("ConvAI.mailer.send_email") as send:
            r = self.client.post(reverse("meetings:invite_send", args=[self.meeting.pk]),
                                 {"channel": "email"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertTrue(r["ok"])
        kwargs = send.call_args.kwargs
        self.assertIn("https://care.example.org/m/", kwargs["text"])
        self.assertEqual(kwargs["attachments"][0][0], "meeting.ics")

    def test_panel_offers_the_room(self):
        self.client.force_login(self.nav)
        resp = self.client.get(reverse("panel_fragment"), {"item": self.meeting.panel_token})
        self.assertContains(resp, reverse("meetings:staff_room", args=[self.meeting.pk]))
        self.assertContains(resp, "Send link")


class Lifecycle(MeetingsTestCase):
    def setUp(self):
        super().setUp()
        self.invite = invites.primary_invite(self.meeting, create=True)
        self.session = rooms.start(self.meeting, self.nav)
        self.client.force_login(self.nav)

    def test_cancel_revokes_and_closes(self):
        self.client.post(reverse("cancel_meeting", args=[self.meeting.pk]), {"reason": "ill"})
        self.invite.refresh_from_db(); self.session.refresh_from_db()
        self.assertTrue(self.invite.is_revoked)
        self.assertEqual(self.session.status, MeetingSession.Status.ENDED)

    def test_outcome_closes_the_room(self):
        self.client.post(reverse("complete_meeting", args=[self.meeting.pk]),
                         {"status": Meeting.Status.COMPLETED})
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, MeetingSession.Status.ENDED)

    def test_moving_to_another_client_revokes(self):
        other = Patient.objects.create(name="Rosa", lastname="Diaz", navigator=self.nav)
        from django.utils import timezone
        self.client.post(reverse("edit_meeting", args=[self.meeting.pk]), {
            "patient": other.pk, "modality": Meeting.Modality.ONLINE,
            "type": Meeting.MeetingType.REGULAR,
            "scheduled_time": timezone.localtime(self.meeting.scheduled_time).strftime("%Y-%m-%dT%H:%M")})
        self.invite.refresh_from_db()
        self.assertTrue(self.invite.is_revoked)

    def test_rescheduling_keeps_the_link(self):
        from django.utils import timezone
        when = timezone.localtime(self.meeting.scheduled_time + timezone.timedelta(days=1))
        self.client.post(reverse("edit_meeting", args=[self.meeting.pk]), {
            "patient": self.patient.pk, "modality": Meeting.Modality.ONLINE,
            "type": Meeting.MeetingType.REGULAR, "scheduled_time": when.strftime("%Y-%m-%dT%H:%M")})
        self.invite.refresh_from_db()
        self.assertFalse(self.invite.is_revoked)
