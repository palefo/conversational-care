"""
URL configuration for Django_CMS project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path
from django.views.i18n import JavaScriptCatalog


from dotenv import load_dotenv
import os
load_dotenv()  # Load environment variables from .env

urlpatterns = [
    #path('ConvAI/', include('ConvAI.urls')),
    #path('', views.home_pg, name='home'),
 ] + i18n_patterns(
    path('jsi18n/', JavaScriptCatalog.as_view(), name='javascript-catalog'),
    path('admin/', admin.site.urls),
    path('filer/', include('filer.urls')),
    #path('', include('cms.urls')),
)

urlpatterns += [
    path('', include('ConvAI.urls')),
]

# Online meetings, only where the app is part of the deployment (MEETINGS_APP).
if getattr(settings, "MEETINGS_APP", False):
    urlpatterns += [path('', include('meetings.urls'))]

# Serve ONLY public branding assets (the logo shown on the login page) from
# /media/. All other media is private — care plans, voice notes, TTS, call
# recordings — and is reachable exclusively through ownership-checked views or
# short-lived signed tokens, never as a static /media/ URL. See file_storage.md.
#
# Served whatever DEBUG says: `static()` returns nothing once DEBUG is off, so a
# logo uploaded in Settings → Branding stopped showing on every installation
# running in production mode. A logo is a few KB requested once per visitor,
# the same order as the static files the app server already serves; a reverse
# proxy in front may still take /media/branding/ over, as file_storage.md says.
from django.urls import re_path
from django.views.static import serve as _serve_branding

urlpatterns += [
    re_path(r"^%sbranding/(?P<path>.*)$" % settings.MEDIA_URL.lstrip("/"), _serve_branding,
            {"document_root": os.path.join(settings.MEDIA_ROOT, "branding")}),
]

