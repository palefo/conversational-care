"""Client links: unforgeable, revocable, harmless to scanners, and leaving the
address bar once used."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from ConvAI.models import Meeting
from meetings import invites, links, rooms
from meetings.models import MeetingInvite

from .base import MeetingsTestCase


class Links(MeetingsTestCase):
    def setUp(self):
        super().setUp()
        self.invite = invites.primary_invite(self.meeting, create=True, by=self.nav)

    def test_invitee_is_the_caregiver_by_first_name(self):
        self.assertEqual(self.invite.invitee, MeetingInvite.Invitee.CAREGIVER)
        self.assertEqual(self.invite.display_name, "Ana")

    def test_token_round_trip_and_forgery(self):
        token = links.token_for(self.invite)
        self.assertEqual(links.resolve(self.invite.public_id, token).pk, self.invite.pk)
        self.assertIsNone(links.resolve(self.invite.public_id, token[:-1] + "x"))
        self.assertIsNone(links.resolve("nope", token))

    def test_rotation_kills_the_old_link(self):
        old = links.token_for(self.invite)
        self.invite.rotate()
        self.assertIsNone(links.resolve(self.invite.public_id, old))
        self.assertIsNotNone(links.resolve(self.invite.public_id, links.token_for(self.invite)))

    def test_window(self):
        now = timezone.now()
        self.assertEqual(links.verdict(self.invite, now=now), links.Verdict.OK)
        self.assertEqual(links.verdict(self.invite, now=now - timedelta(hours=2)), links.Verdict.EARLY)
        self.assertEqual(links.verdict(self.invite, now=now + timedelta(hours=5)), links.Verdict.UNAVAILABLE)

    def test_rescheduling_moves_the_window_with_the_meeting(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(scheduled_time=timezone.now() + timedelta(days=2))
        self.invite.refresh_from_db()
        self.assertEqual(links.verdict(self.invite), links.Verdict.EARLY)

    def test_a_live_room_admits_outside_the_window(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(scheduled_time=timezone.now() + timedelta(days=2))
        rooms.start(Meeting.objects.get(pk=self.meeting.pk), self.nav)
        self.invite.refresh_from_db()
        self.assertEqual(links.verdict(self.invite), links.Verdict.OK)

    def test_unavailable_when_revoked_cancelled_or_off(self):
        self.invite.revoke("test")
        self.assertEqual(links.verdict(self.invite), links.Verdict.UNAVAILABLE)
        self.invite.rotate()
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.CANCELLED)
        self.invite.refresh_from_db()
        self.assertEqual(links.verdict(self.invite), links.Verdict.UNAVAILABLE)
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.PENDING)
        self.set_enabled("0")
        self.invite.refresh_from_db()
        self.assertEqual(links.verdict(self.invite), links.Verdict.UNAVAILABLE)

    def test_get_changes_nothing(self):
        path = links.path_for(self.invite)
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertNotIn("mtg_invite", self.client.session)
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.open_count, 0)

    def test_post_passes_a_real_browsers_csrf_origin_check(self):
        """Regression: with no-referrer, browsers sent Origin: null and every
        Continue press was refused by CSRF."""
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        page = c.get(links.path_for(self.invite))
        token = page.cookies["csrftoken"].value
        resp = c.post(links.path_for(self.invite), {"csrfmiddlewaretoken": token},
                      HTTP_ORIGIN="http://testserver", HTTP_REFERER="http://testserver" + links.path_for(self.invite))
        self.assertEqual(resp.status_code, 302)

    def test_post_binds_and_leaves_the_address_bar(self):
        resp = self.client.post(links.path_for(self.invite))
        self.assertRedirects(resp, reverse("meetings:lobby"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["mtg_invite"], self.invite.pk)
        lobby = self.client.get(reverse("meetings:lobby"))
        self.assertEqual(lobby.status_code, 200)
        self.assertContains(lobby, "Ana")
        self.assertIn("camera=(self)", lobby["Permissions-Policy"])
        self.assertIn("wss://lk.example.org", lobby["Content-Security-Policy"])

    def test_rotating_unbinds_a_browser_already_in_the_lobby(self):
        self.client.post(links.path_for(self.invite))
        self.invite.rotate()
        self.assertEqual(self.client.get(reverse("meetings:lobby")).status_code, 404)

    def test_bad_links_all_look_the_same(self):
        good = links.path_for(self.invite)
        bad = self.client.get(good[:-2] + "zz")
        self.invite.revoke("x")
        revoked = self.client.get(good)
        self.assertEqual(bad.status_code, 404)
        self.assertEqual(revoked.status_code, 404)
        self.assertEqual(bad.content, revoked.content)

    def test_early_link_shows_the_time_not_the_room(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(scheduled_time=timezone.now() + timedelta(days=1))
        resp = self.client.post(links.path_for(self.invite))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("mtg_invite", self.client.session)
        self.assertContains(resp, "calendar.ics")
        ics = self.client.get(links.path_for(self.invite) + "/calendar.ics")
        self.assertEqual(ics.status_code, 200)
        self.assertIn(b"BEGIN:VEVENT", ics.content)

    def test_brute_force_is_throttled(self):
        for _ in range(31):
            self.client.get(f"/m/{self.invite.public_id}/wrongtoken")
        resp = self.client.get(links.path_for(self.invite))
        self.assertEqual(resp.status_code, 429)
