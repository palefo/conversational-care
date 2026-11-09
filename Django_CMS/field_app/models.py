from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class FieldAppInstallation(models.Model):
    """One Field App deployment this platform is registered with.

    Field App's own Settings → Third-party apps gives the client id and secret;
    it needs this platform's callback URL (shown on our Settings tab).

    Like the platform's other integration credentials (Settings → Integrations),
    the secret and the users' tokens are stored as plain columns: protect the
    database and its backups accordingly.
    """

    name = models.CharField(_("Name"), max_length=80, default="Field App")
    base_url = models.URLField(
        _("Field App address"),
        help_text=_("Where people open Field App in their browser, e.g. https://field.example.org"),
    )
    api_url = models.URLField(
        _("Server address (optional)"), blank=True,
        help_text=_("How this server reaches Field App, if different — e.g. "
                    "http://host.docker.internal:8003 when both run in Docker on one machine."),
    )
    client_id = models.CharField(_("Client ID"), max_length=80)
    client_secret = models.CharField(_("Client secret"), max_length=200)
    is_active = models.BooleanField(_("Active"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def browser_root(self):
        return self.base_url.rstrip("/")

    @property
    def api_root(self):
        return (self.api_url or self.base_url).rstrip("/")


class FieldAppConnection(models.Model):
    """A user's Field App account, connected through Field App's consent screen.

    The token is read-only and acts as that Field App user: it sees exactly the
    athletes they can see there.
    """

    installation = models.ForeignKey(FieldAppInstallation, on_delete=models.CASCADE, related_name="connections")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="field_app_connections")
    token = models.CharField(max_length=200)
    remote_user_id = models.IntegerField(null=True, blank=True)
    remote_username = models.CharField(max_length=150, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    # Set when Field App rejects the token (revoked there, or the app switched off).
    broken_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("installation", "user")]

    def __str__(self):
        return f"{self.user} → {self.installation} ({self.remote_username})"


class PatientFieldAppLink(models.Model):
    """This client is that athlete in Field App. Made by a link worker."""

    patient = models.ForeignKey("ConvAI.Patient", on_delete=models.CASCADE, related_name="field_app_links")
    installation = models.ForeignKey(FieldAppInstallation, on_delete=models.CASCADE, related_name="links")
    athlete_id = models.PositiveIntegerField()
    athlete_name = models.CharField(max_length=250, blank=True)
    linked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    linked_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("patient", "installation")]

    def __str__(self):
        return f"{self.patient} ↔ {self.installation} #{self.athlete_id}"


class AgentDataSource(models.Model):
    """Agent form → Data sources: give this agent the client's Field App record."""

    agent = models.ForeignKey("ConvAI.Agent", on_delete=models.CASCADE, related_name="field_app_sources")
    installation = models.ForeignKey(FieldAppInstallation, on_delete=models.CASCADE, related_name="agent_sources")
    enabled = models.BooleanField(default=False)
    rounds = models.PositiveSmallIntegerField(default=1)
    instructions = models.TextField(blank=True)

    class Meta:
        unique_together = [("agent", "installation")]
