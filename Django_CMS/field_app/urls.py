from django.urls import path

from . import views

urlpatterns = [
    path("field-app/<int:pk>/connect/", views.connect_start, name="field_app_connect"),
    path("field-app/callback/", views.callback, name="field_app_callback"),
    path("field-app/<int:pk>/disconnect/", views.disconnect, name="field_app_disconnect"),
    path("patients/<int:patient_pk>/field-app/<int:pk>/link/", views.link, name="field_app_link"),
    path("patients/<int:patient_pk>/field-app/<int:pk>/unlink/", views.unlink, name="field_app_unlink"),
    path("field-app/<int:pk>/toggle/", views.installation_toggle, name="field_app_toggle"),
    path("field-app/<int:pk>/delete/", views.installation_delete, name="field_app_delete"),
]
