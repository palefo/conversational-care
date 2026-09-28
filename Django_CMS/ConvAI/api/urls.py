"""REST API routes. Included from ConvAI/urls.py only when ENABLE_API is true,
so deployments that don't want a public API can leave it off."""
from django.urls import path

from .views import (
    MessageView, SelfRegistrationView, ProtocolDetailView,
    AlertListCreateView, AlertDetailView, AlertResolveView,
    PatientsListView, MeetingCreateView, PatientProtocolsFilledView,
    ProtocolQuestionsView, MeetingAnswersUpsertView, PatientMeetingsListView,
    PatientDetailsAppendView, ConversationVisibilityView,
    ConversationSummaryView, RunView, RunSummaryView, RunVisibilityView,
    EnrolmentLookupView,
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
    # Whether the client's link worker may read this conversation. 404s while
    # CONVERSATION_PRIVACY_ENABLED is off — see conversation_privacy.md.
    path("api/v1/conversations/<str:conversation_id>/visibility/",
         ConversationVisibilityView.as_view(), name="api_conversation_visibility"),
    # What the agent says the conversation was about. Not behind a switch — see
    # the view's docstring and agent_tools.md.
    path("api/v1/conversations/<str:conversation_id>/summary/",
         ConversationSummaryView.as_view(), name="api_conversation_summary"),
    # For remote agents, authenticated by the per-run token in their run config
    # and nothing else. No conversation id: the token says which. See
    # ConvAI.run_tokens and agent_tools.md.
    path("api/v1/run/", RunView.as_view(), name="api_run"),
    path("api/v1/run/summary/", RunSummaryView.as_view(), name="api_run_summary"),
    path("api/v1/run/visibility/", RunVisibilityView.as_view(), name="api_run_visibility"),
    # Is this person enrolled in a study? 404s while STUDY_ENROLMENT_ENABLED is
    # off. Token-authenticated, and never returns the access code itself.
    path("api/v1/enrolments/", EnrolmentLookupView.as_view(), name="api_enrolments"),
]
