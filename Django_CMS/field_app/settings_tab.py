"""Settings → Third-party apps: register the Field App installations."""
from __future__ import annotations

from django import forms
from django.db.models import Count
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .models import FieldAppInstallation

_INPUT = {"class": "form-control"}


class InstallationForm(forms.ModelForm):
    class Meta:
        model = FieldAppInstallation
        fields = ["name", "base_url", "api_url", "client_id", "client_secret"]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "base_url": forms.URLInput(attrs={**_INPUT, "placeholder": "https://field.example.org"}),
            "api_url": forms.URLInput(attrs={**_INPUT, "placeholder": "http://host.docker.internal:8003"}),
            "client_id": forms.TextInput(attrs={**_INPUT, "autocomplete": "off"}),
            "client_secret": forms.PasswordInput(attrs={**_INPUT, "autocomplete": "new-password"}),
        }


def callback_url(request):
    return request.build_absolute_uri(reverse("field_app_callback"))


def _context(request, bound_form=None):
    installations = FieldAppInstallation.objects.annotate(
        n_connections=Count("connections", distinct=True),
        n_links=Count("links", distinct=True),
    )
    return {
        "form": bound_form or InstallationForm(),
        "installations": installations,
        "callback_url": callback_url(request),
    }


def _bind(request):
    return InstallationForm(request.POST)


TAB = {
    "id": "thirdparty",
    "label": _("Third-party apps"),
    "icon": "extension",
    "template": "field_app/settings_tab.html",
    "context": _context,
    "bind": _bind,
}
