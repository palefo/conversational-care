"""REST API routes. Included from ConvAI/urls.py only when ENABLE_API is true,
so deployments that don't want a public API can leave it off."""
from django.urls import path

from .views import (
    MessageView, SelfRegistrationView, ProtocolDetailView,
    AlertListCreateView, AlertDetailView, AlertResolveView,
    PatientsListView, MeetingCreateView, PatientProtocolsFilledView,
    ProtocolQuestionsView, MeetingAnswersUpsertView, PatientMeetingsListView,
    PatientDetailsAppendView,
)

urlpatterns = [
    path("api/v1/messages/", MessageView.as_view(), name="api_messages"),
    path("api/v1/self-registrations/", SelfRegistrationView.as_view(), name="api_self_registrations"),
    path("api/v1/protocols/<int:meeting_id>/<int:protocol_num>/", ProtocolDetailView.as_view(), name="api_protocol_detail"),
    path("api/v1/alerts/", AlertListCreateView.as_view(), name="api_alerts"),
    path("api/v1/alerts/<int:pk>/", AlertDetailView.as_view(), name="api_alert_detail"),
    path("api/v1/alerts/<int:pk>/resolve/", AlertResolveView.as_view(), name="api_alert_resolve"),
    path("api/v1/patients/", PatientsListView.as_view(), name="api_patients_list"),
    path("api/v1/meetings/", MeetingCreateView.as_view(), name="api_meeting_create"),
    path("api/v1/patients/<int:patient_id>/protocols/filled/", PatientProtocolsFilledView.as_view(), name="patient-protocols-filled"),
    path("api/v1/protocols/<int:protocol_num>/questions/", ProtocolQuestionsView.as_view(), name="protocol_questions"),
    path("api/v1/meetings/<int:meeting_id>/answers/", MeetingAnswersUpsertView.as_view(), name="meeting_answers_upsert"),
    path("api/v1/patients/<int:patient_id>/meetings/", PatientMeetingsListView.as_view(), name="patient_meetings"),
    path("api/v1/patients/<int:patient_id>/details/append/", PatientDetailsAppendView.as_view(), name="api_patient_details_append"),
]
