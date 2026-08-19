"""Root ConvAI urlconf.

Composes the web routes with, optionally, the REST API (gated by ENABLE_API).
Async WhatsApp/SMS replies run on an in-process worker pool and need no extra
routes or dashboard.
"""
from django.conf import settings
from django.urls import include, path

urlpatterns = [
    path("", include("ConvAI.urls_web")),
]

# REST API is opt-in per deployment.
if getattr(settings, "ENABLE_API", True):
    urlpatterns += [path("", include("ConvAI.api.urls"))]
