"""What this app contributes to the core, through ConvAI.extensions."""
from __future__ import annotations

import logging
import uuid

from django import forms
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .client import Api, FieldAppError
from .models import AgentDataSource, FieldAppConnection, FieldAppInstallation, PatientFieldAppLink

logger = logging.getLogger(__name__)

# How long a fetched record is reused. A conversation turn may run the agent
# more than once; a minute keeps that to one call without serving stale data
# for long.
CONTEXT_CACHE_SECONDS = 60

DEFAULT_INSTRUCTIONS = (
    "Below is this client's record from {name}, the app their link worker uses on "
    "home visits: their questionnaire answers, newest first. Use it to understand "
    "and personalise your replies. Do not read it out or list it back, do not "
    "diagnose from it, and do not assume it is complete or current — each round "
    "says when it was collected. If the client contradicts it, believe the client."
)


def active_installations():
    return FieldAppInstallation.objects.filter(is_active=True)


def connection_for(user, installation):
    if not getattr(user, "is_authenticated", False):
        return None
    return FieldAppConnection.objects.filter(installation=installation, user=user).first()


def mark_broken(conn):
    if conn and not conn.broken_at:
        conn.broken_at = timezone.now()
        conn.save(update_fields=["broken_at"])


# ── Agent form → Data sources ───────────────────────────────────────────────

class DataSourceForm(forms.Form):
    enabled = forms.BooleanField(required=False, label=_("Give the agent the client's Field App record"))
    rounds = forms.TypedChoiceField(
        label=_("Answers per questionnaire"), coerce=int, initial=1,
        choices=[(1, _("Latest only")), (2, "2"), (3, "3"), (5, "5")],
        help_text=_("How many recent rounds of each questionnaire to include."),
    )
    instructions = forms.CharField(
        label=_("What the agent is told about it"), required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_("Leave empty for the default wording."),
    )


class DataSourceSection:
    """One installation's block in the Data sources tab."""

    template = "field_app/data_source.html"

    def __init__(self, agent, installation, data=None):
        self.agent, self.installation = agent, installation
        existing = (AgentDataSource.objects.filter(agent=agent, installation=installation).first()
                    if agent and agent.pk else None)
        initial = ({"enabled": existing.enabled, "rounds": existing.rounds,
                    "instructions": existing.instructions} if existing else {})
        self.form = DataSourceForm(data, initial=initial, prefix=f"fieldapp{installation.pk}")
        self.ctx = {
            "form": self.form,
            "installation": installation,
            "default_instructions": DEFAULT_INSTRUCTIONS.format(name=installation.name),
            "linked_clients": installation.links.count(),
        }

    def is_valid(self):
        return self.form.is_valid()

    def save(self, agent):
        d = self.form.cleaned_data
        AgentDataSource.objects.update_or_create(
            agent=agent, installation=self.installation,
            defaults={"enabled": d["enabled"], "rounds": d["rounds"],
                      "instructions": d["instructions"].strip()},
        )


# ── The record, for one run ──────────────────────────────────────────────────

def _patient_for_thread(thread_id):
    from ConvAI.models import Conversation

    try:
        conv_id = uuid.UUID(str(thread_id))
    except (TypeError, ValueError):
        return None
    conv = Conversation.objects.select_related("patient").filter(id=conv_id).first()
    return conv.patient if conv else None


def token_for(patient, link):
    """Whose connection reads this client's record.

    The client's own link worker (navigator) first: the record is read as the
    person responsible for them. Failing that, whoever made the link. Never a
    connection unrelated to this client.
    """
    for user_id in (patient.navigator_id, link.linked_by_id):
        if not user_id:
            continue
        conn = FieldAppConnection.objects.filter(
            installation=link.installation, user_id=user_id, broken_at__isnull=True).first()
        if conn:
            return conn
    return None


def record_markdown(source, patient):
    """The Markdown Field App returns for this client's linked athlete, or ''."""
    link = PatientFieldAppLink.objects.filter(patient=patient, installation=source.installation).first()
    if not link:
        return ""
    conn = token_for(patient, link)
    if not conn:
        logger.info("Field App: client %s is linked but no usable connection can read it", patient.pk)
        return ""
    key = f"field_app:ctx:{conn.pk}:{link.athlete_id}:{source.rounds}"
    md = cache.get(key)
    if md is None:
        try:
            md = Api(source.installation, conn.token).context_markdown(link.athlete_id, source.rounds)
        except FieldAppError as exc:
            if exc.unauthorized:
                mark_broken(conn)
            logger.warning("Field App: could not fetch athlete %s for client %s: %s",
                           link.athlete_id, patient.pk, exc)
            return ""
        cache.set(key, md, CONTEXT_CACHE_SECONDS)
    return md.strip()


class FieldAppAddon:
    def settings_tabs(self):
        from .settings_tab import TAB
        return [TAB]

    def profile_cards(self, request):
        installations = list(active_installations())
        if not installations:
            return []
        from ConvAI.roles import is_navigator
        if not is_navigator(request.user):
            return []
        rows = [{"installation": i, "connection": connection_for(request.user, i)} for i in installations]
        return [{"template": "field_app/profile_card.html", "ctx": {"rows": rows}}]

    def client_cards(self, request, patient):
        from .views import client_card_context
        ctx = client_card_context(request, patient)
        return [{"template": "field_app/client_card.html", "ctx": ctx}] if ctx else []

    def agent_data_sources(self, agent, data):
        return [DataSourceSection(agent, i, data) for i in active_installations()]

    def agent_context(self, agent, thread_id):
        sources = list(AgentDataSource.objects.select_related("installation").filter(
            agent=agent, enabled=True, installation__is_active=True))
        if not sources:
            return ""
        patient = _patient_for_thread(thread_id)
        if patient is None:
            return ""
        parts = []
        for source in sources:
            md = record_markdown(source, patient)
            if not md:
                continue
            name = source.installation.name
            intro = source.instructions or DEFAULT_INSTRUCTIONS.format(name=name)
            parts.append(f"## Field App record\n\n{intro}\n\n<field_app_record source=\"{name}\">\n{md}\n</field_app_record>")
        return "\n\n".join(parts)
