"""The feature off, or the app gone: core pages unchanged, nothing reachable."""
from django.urls import reverse

from ConvAI import extensions
from ConvAI.models import Meeting
from meetings import invites, links

from .base import MeetingsTestCase


class SwitchedOff(MeetingsTestCase):
    def test_settings_tab_stays_visible_to_admins(self):
        self.set_enabled("0")
        from ConvAI.models import ConvAIUser
        admin = ConvAIUser.objects.create_superuser("root", "r@example.org", "x")
        self.client.force_login(admin)
        resp = self.client.get(reverse("config"))
        self.assertContains(resp, 'data-tab="meetings"')
        self.assertContains(resp, "Online meetings are off")

    def test_saving_the_tab(self):
        from ConvAI.models import ConvAIUser
        from meetings.models import MeetingsSettings
        admin = ConvAIUser.objects.create_superuser("root", "r@example.org", "x")
        self.client.force_login(admin)
        self.client.post(reverse("config_save"), {
            "section": "meetings", "enabled": "0", "record_by_default": "on",
            "link_early_minutes": 15, "link_late_hours": 2, "max_live_rooms": 3,
            "max_minutes": 60, "interviewer_voice": ""})
        s = MeetingsSettings.load()
        self.assertEqual((s.enabled, s.link_early_minutes, s.max_live_rooms), ("0", 15, 3))

    def test_links_and_rooms_are_unreachable(self):
        inv = invites.primary_invite(self.meeting, create=True)
        self.set_enabled("0")
        self.assertEqual(self.client.get(links.path_for(inv)).status_code, 404)
        self.client.force_login(self.nav)
        self.assertEqual(self.client.get(reverse("meetings:staff_room", args=[self.meeting.pk])).status_code, 404)

    def test_panel_says_it_is_off(self):
        self.set_enabled("0")
        self.client.force_login(self.nav)
        resp = self.client.get(reverse("panel_fragment"), {"item": self.meeting.panel_token})
        self.assertContains(resp, "Online meetings are off")
        self.assertFalse(extensions.online_available())

    def test_not_offered_when_booking(self):
        self.set_enabled("0")
        from ConvAI.forms import MeetingForm
        self.assertNotIn(Meeting.Modality.ONLINE, [v for v, _l in MeetingForm().fields["modality"].choices])
