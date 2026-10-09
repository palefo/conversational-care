"""Self-registrations waiting for approval, in the dashboard's alert queue.

*Admins only*, since only an admin can approve one, and *only while
self-registration is switched on*. A row stays until the person is approved and
then goes by itself: it is read from SelfRegistration, not copied into an
Alert that someone would have to resolve as well.

    python3 manage.py test ConvAI.test_dashboard_registrations --settings=test_settings
"""
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ConvAI.models import Alert, ConvAIUser, SelfRegistration, SiteConfiguration


def _self_registration(value="1"):
    cfg = SiteConfiguration.load()
    cfg.self_registration_enabled = value
    cfg.save()


class DashboardRegistrations(TestCase):
    """Every test starts from a cold settings cache — see SenseiTestCase."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = ConvAIUser.objects.create_user(
            username="admin", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.waiting = SelfRegistration.objects.create(
            name="Cho", lastname="Yoon", phone_number="+821089019271",
            details={"source": "self-registration-agent"})

    def rows(self, **params):
        response = self.client.get(reverse("dashboard"), params)
        self.assertEqual(response.status_code, 200)
        return response

    def queue(self, **params):
        return [row.get("name") or row.get("patient") or row.get("title")
                for row in self.rows(**params).context["alerts"]]

    def test_an_admin_sees_it_while_self_registration_is_on(self):
        _self_registration()
        response = self.rows()
        self.assertContains(response, "Cho Yoon")
        self.assertContains(response, "Self-registration")
        self.assertContains(response, "?tab=registrations#registrations")

    def test_it_counts_towards_the_alerts_to_review(self):
        _self_registration()
        self.assertEqual(self.rows().context["alert_counts"]["all"], 1)

    def test_nothing_while_self_registration_is_off(self):
        _self_registration("0")
        response = self.rows()
        self.assertNotContains(response, "Cho Yoon")
        self.assertEqual(response.context["alert_counts"]["all"], 0)

    def test_a_navigator_never_sees_it(self):
        _self_registration()
        nav = ConvAIUser.objects.create_user(username="nav", password="x")
        nav.groups.add(Group.objects.get_or_create(name="Navigator")[0])
        self.client.force_login(nav)
        self.assertNotContains(self.rows(), "Cho Yoon")

    def test_it_goes_once_approved(self):
        _self_registration()
        self.waiting.state = SelfRegistration.State.APPROVED
        self.waiting.save()
        self.assertNotContains(self.rows(), "Cho Yoon")

    def test_the_search_box_finds_it_by_name(self):
        _self_registration()
        self.assertIn("Cho Yoon", self.queue(q="yoon"))
        self.assertNotIn("Cho Yoon", self.queue(q="somebody else"))

    def test_it_never_pushes_a_high_alert_down(self):
        _self_registration()
        Alert.objects.create(title="Crisis", priority=Alert.Priority.HIGH, user=self.admin)
        Alert.objects.create(title="Routine", priority=Alert.Priority.LOW, user=self.admin)
        self.assertEqual(self.queue(), ["Crisis", "Cho Yoon", "Routine"])
