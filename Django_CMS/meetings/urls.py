"""Routes for online meetings.

Mounted at the site root by Django_CMS/urls.py only when the app is installed.
Staff pages live under /meetings/, client pages under the short /m/ (it ends
up in text messages), and the agents' internal API under
/meetings/internal/v1/.
"""
from django.urls import path

from .views import internal, public, staff, webhooks

app_name = "meetings"

urlpatterns = [
    # Staff
    path("meetings/<int:meeting_id>/room/", staff.staff_room, name="staff_room"),
    path("meetings/<int:meeting_id>/online/start/", staff.start, name="start"),
    path("meetings/<int:meeting_id>/online/end/", staff.end, name="end"),
    path("meetings/<int:meeting_id>/online/state/", staff.state, name="state"),
    path("meetings/<int:meeting_id>/online/lobby/<str:public_id>/<str:decision>/",
         staff.lobby_decide, name="lobby_decide"),
    path("meetings/<int:meeting_id>/online/auto-admit/", staff.auto_admit, name="auto_admit"),
    path("meetings/<int:meeting_id>/online/invite/", staff.invite, name="invite"),
    path("meetings/<int:meeting_id>/online/invite/send/", staff.invite_send, name="invite_send"),
    path("meetings/<int:meeting_id>/online/invite.ics", staff.invite_ics, name="invite_ics"),
    path("meetings/<int:meeting_id>/online/interview/", staff.interview_start, name="interview_start"),
    path("meetings/<int:meeting_id>/online/assistant/", staff.assistant_start, name="assistant_start"),
    path("meetings/<int:meeting_id>/online/runs/<int:run_id>/stop/", staff.run_stop, name="run_stop"),
    path("meetings/<int:meeting_id>/online/questions/", staff.questions, name="questions"),
    path("patients/<int:patient_id>/online/start-now/", staff.start_now, name="start_now"),

    # Clients (no login: the link is the credential)
    path("m/lobby/", public.lobby, name="lobby"),
    path("m/lobby/knock/", public.knock, name="knock"),
    path("m/lobby/status/", public.lobby_status, name="lobby_status"),
    path("m/lobby/leave/", public.leave, name="leave"),
    path("m/ended/", public.ended, name="ended"),
    path("m/<str:public_id>/<str:token>/calendar.ics", public.link_ics, name="link_ics"),
    path("m/<str:public_id>/<str:token>", public.link, name="link"),

    # LiveKit and the agent workers
    path("meetings/hooks/livekit/", webhooks.livekit_webhook, name="webhook"),
    path("meetings/internal/v1/gate/", internal.gate, name="internal_gate"),
    path("meetings/internal/v1/run/", internal.run_config, name="internal_run"),
    path("meetings/internal/v1/run/status/", internal.run_status, name="internal_status"),
    path("meetings/internal/v1/protocol/questions/", internal.protocol_questions,
         name="internal_questions"),
    path("meetings/internal/v1/protocol/answers/", internal.protocol_answer,
         name="internal_answer"),
    path("meetings/internal/v1/search/", internal.search, name="internal_search"),
    path("meetings/internal/v1/recording/segments/", internal.recording_segment,
         name="internal_segment"),
]
