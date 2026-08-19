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
from django.conf.urls.static import static
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

if settings.DEBUG:
    # Serve ONLY public branding assets (the logo shown on the login page) from
    # /media/. All other media is private — care plans, voice notes, TTS, call
    # recordings — and is reachable exclusively through ownership-checked views
    # or short-lived signed tokens, never as a static /media/ URL. In production
    # the reverse proxy must apply the same rule: expose /media/branding/ only.
    # See file_storage.md.
    urlpatterns += static(
        settings.MEDIA_URL + "branding/",
        document_root=os.path.join(settings.MEDIA_ROOT, "branding"),
    )

