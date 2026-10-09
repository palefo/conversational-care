"""Settings → Online meetings: the tab this app adds to the core Settings page."""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import MeetingsSettings

_SELECT = {"class": "form-select"}
_INPUT = {"class": "form-control"}


class MeetingsSettingsForm(forms.ModelForm):
    class Meta:
        model = MeetingsSettings
        fields = ["enabled", "record_by_default", "auto_admit", "link_early_minutes",
                  "link_late_hours", "max_live_rooms", "max_minutes", "assistant_agent",
                  "interviewer_voice"]
        labels = {
            "enabled": _("Online meetings"),
            "record_by_default": _("Record audio"),
            "auto_admit": _("Let invited clients straight in"),
            "link_early_minutes": _("Link opens (minutes before)"),
            "link_late_hours": _("Link closes (hours after)"),
            "max_live_rooms": _("Meetings at the same time"),
            "max_minutes": _("Longest meeting (minutes)"),
            "assistant_agent": _("Assistant agent"),
            "interviewer_voice": _("Agent voice"),
        }
        widgets = {
            "enabled": forms.Select(attrs=_SELECT),
            "link_early_minutes": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 240}),
            "link_late_hours": forms.NumberInput(attrs={**_INPUT, "min": 1, "max": 48}),
            "max_live_rooms": forms.NumberInput(attrs={**_INPUT, "min": 1, "max": 50}),
            "max_minutes": forms.NumberInput(attrs={**_INPUT, "min": 10, "max": 480}),
            "assistant_agent": forms.Select(attrs=_SELECT),
            "interviewer_voice": forms.TextInput(attrs={**_INPUT, "placeholder": "marin"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from ConvAI.models import Agent
        field = self.fields["assistant_agent"]
        field.queryset = Agent.objects.filter(kind=Agent.Kind.PROMPT).order_by("name")
        field.empty_label = _("— None —")
        field.required = False


def _context(request, bound_form=None):
    from ConvAI.jobs import worker_seen

    from . import config
    from .models import MeetingSession

    return {
        "form": bound_form or MeetingsSettingsForm(instance=MeetingsSettings.load()),
        "status": config.status(),
        "live_rooms": MeetingSession.objects.filter(status=MeetingSession.Status.LIVE).count(),
        "worker_seen": worker_seen(),
    }


def _bind(request):
    return MeetingsSettingsForm(request.POST, instance=MeetingsSettings.load())


TAB = {
    "id": "meetings",
    "label": _("Online meetings"),
    "icon": "videocam",
    "template": "meetings/settings_tab.html",
    "context": _context,
    "bind": _bind,
}
