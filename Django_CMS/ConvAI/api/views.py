# ConvAI/api/views.py
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Value
from django.db.models.functions import Concat
from django.utils import timezone
from django.shortcuts import get_object_or_404

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status


# If you built the custom Bearer auth:
try:
    from .authentication import BearerTokenAuthentication
    AUTH_CLASSES = [BearerTokenAuthentication]
except Exception:
    # fallback to DRF Token if you didn't add the custom one
    from rest_framework.authentication import TokenAuthentication
    AUTH_CLASSES = [TokenAuthentication]

from .serializers import (
    MessageInSerializer, 
    MessageOutSerializer, 
    SelfRegistrationInSerializer, 
    SelfRegistrationOutSerializer, 
    AlertInSerializer, 
    AlertOutSerializer,
    PatientOutSerializer,
    MeetingCreateInSerializer,
    MeetingOutSerializer,
    AnswerUpsertItemSerializer,
    MeetingAnswersUpsertInSerializer,
    PatientDetailsAppendInSerializer,
    PatientDetailsAppendOutSerializer,
    ConversationVisibilityInSerializer,
    ConversationVisibilityOutSerializer,
    ConversationSummaryInSerializer,
    ConversationSummaryOutSerializer,
    RunOutSerializer,
)
from ..run_tokens import RunTokenAuthentication
from ..models import (
    Conversation, 
    Message, 
    SelfRegistration, 
    Meeting, 
    Protocol, 
    Question, 
    Answer, 
    Alert, 
    Patient
)
from django.contrib.auth import get_user_model

User = get_user_model()

logger = logging.getLogger(__name__)


STALE_THREAD_HOURS = 2  # change to 3 if you prefer

class MessageView(APIView):
    """
    POST /api/v1/messages/
    Authorization: Bearer <token>  (or "Token <key>" if using DRF TokenAuthentication)
    Body: { "text": "..." }

    Uses the Agent configured on request.user.
    Maintains an active Conversation per user (rolls over when stale or when text == "/quit").
    Saves Message rows and updates Conversation timestamps.
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        ser = MessageInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        # AS-01 guardrail (DEFERRED efficacy): sanitise untrusted input before the model.
        from ..llm_guardrails import sanitize_user_text
        text: str = sanitize_user_text(ser.validated_data["text"])
        user: User = request.user

        restarted = (text.lower() in ("/quit", "/restart"))

        # 1) Ensure we can resolve (or create) the active conversation
        try:
            conv = _get_or_create_active_conversation_for_user(user, restart=restarted)
        except Exception as e:
            # If Conversation model lacks 'user'/'agent' FKs in your DB, comment those assignments in helper below.
            return Response(
                {"detail": f"Failed to prepare conversation: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # 2) Produce a reply
        if restarted:
            reply_text = "All set! Your conversation has been reset. How can I help you now?"
        else:
            # AS-02 guardrail (DEFERRED efficacy): bound/disclaim model output.
            from ..llm_guardrails import filter_model_output
            reply_text = filter_model_output(_invoke_langgraph_with_user_agent(user, text, str(conv.id)))

        # 3) Persist Message + update conv timestamps (never let failures crash the API)
        try:
            _persist_exchange(user, conv, user_text=text, bot_text=reply_text)
        except Exception:
            # We still return, but without raising — client still receives the bot text.
            pass

        out = MessageOutSerializer(
            {
                "conversation_id": str(conv.id),
                "user_message": text,
                "response_message": reply_text,
                "restarted": restarted,
            }
        )
        return Response(out.data, status=status.HTTP_200_OK)


def _get_or_create_active_conversation_for_user(user: User, restart: bool = False) -> Conversation:
    """
    Returns the active Conversation for this user; creates a new one if:
      • none exists,
      • restart=True (/quit),
      • or last_message_at is older than STALE_THREAD_HOURS.
    If your Conversation model doesn't have 'user' or 'agent' FKs, comment those lines.
    """
    now = timezone.now()

    # Try to find the most recent conversation for this user
    conv_qs = Conversation.objects.all()
    if hasattr(Conversation, "user"):
        conv_qs = conv_qs.filter(user=user)
    conv = conv_qs.order_by("-last_message_at").first()

    stale = False
    if conv and conv.last_message_at:
        stale = (now - conv.last_message_at) >= timedelta(hours=STALE_THREAD_HOURS)

    if not conv or restart or stale:
        conv = Conversation(
            id=uuid.uuid4(),
            started_at=now,
            last_message_at=now,
        )
        # Optional: tie to user & the user's agent if these fields exist
        if hasattr(conv, "user"):
            conv.user = user
        if hasattr(conv, "agent"):
            conv.agent = getattr(user, "agent", None)
        conv.save()
        return conv

    # Keep using existing conversation
    return conv


def _invoke_langgraph_with_user_agent(user: User, user_message: str, thread_id: str) -> str:
    """
    Directly calls LangGraph using the Agent assigned to the user.
    Returns a user-friendly error string instead of raising.
    """
    agent = getattr(user, "agent", None)
    if not agent:
        return "Sorry, no agent is configured for this user."

    # AS-06/F8 fix: deny SSRF to non-allow-listed agent hosts.
    from ..utils import agent_host_allowed
    if not agent_host_allowed(getattr(agent, "host", "")):
        return "Sorry, the configured agent host is not permitted."

    try:
        from langgraph_sdk import get_client, get_sync_client
        from langgraph.pregel.remote import RemoteGraph
    except Exception:
        # If the SDK isn't installed in this environment
        return "Sorry, the LangGraph client is not available on the server."

    url = f"http://{agent.host}:{agent.port}"
    graph_name = agent.langgraph_name

    try:
        client = get_client(url=url)
        sync = get_sync_client(url=url)
        rg = RemoteGraph(graph_name, client=client, sync_client=sync)
    except Exception:
        return "Sorry, could not connect to the configured agent."

    try:
        result = rg.invoke(
            {"messages": [{"role": "user", "content": user_message}]},
            config={"configurable": {"thread_id": thread_id}},
        )
        # Defensive parsing
        msgs = result.get("messages") if isinstance(result, dict) else None
        if isinstance(msgs, list) and msgs:
            last = msgs[-1]
            if isinstance(last, dict):
                content = last.get("content")
                if isinstance(content, str) and content.strip():
                    return content
        # Fallback
        return "Sorry, something went wrong generating the response."
    except Exception:
        return "Sorry, something went wrong generating the response."


@transaction.atomic
def _persist_exchange(user: User, conv: Conversation, user_text: str, bot_text: str) -> None:
    """
    Writes one Message row and updates Conversation timestamps & agent link.
    Does not raise outward (wrapped by caller).
    """
    # Message.user expects a string; prefer phone if present, else username/email.
    # WB-06 fix: guard None so identity never becomes the literal string "None".
    _phone = getattr(user, "phone_number", None)
    identity = (
        (str(_phone).strip() if _phone else "")
        or (user.email or "").strip()
        or (user.username or "").strip()
        or "api-user"
    )

    from ..message_attribution import create_message
    # Owned by the API account that sent it. The identity string above is only
    # the address it is recorded under; the account is what it belongs to.
    create_message(
        user=identity,
        conversation_id=str(conv.id),
        user_message=user_text,
        response_message=bot_text,
        patient=getattr(conv, "patient", None),
        account=user,
        sender_role=Message.SenderRole.API,
    )

    # Keep Conversation fresh & ensure agent is set from user if field exists
    updates = ["last_message_at"]
    conv.last_message_at = timezone.now()

    if hasattr(conv, "agent") and not conv.agent:
        conv.agent = getattr(user, "agent", None)
        updates.append("agent")
    if hasattr(conv, "user") and not conv.user_id:
        conv.user = user
        updates.append("user")

    conv.save(update_fields=list(set(updates)))

# ---- SelfRegistrationView (use AUTH_CLASSES, not CustomTokenAuthentication) ----
class SelfRegistrationView(APIView):
    authentication_classes = AUTH_CLASSES        # <-- fix here
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Only staff (or above) can create self-registrations via API
        if not request.user.is_staff:
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        ser = SelfRegistrationInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        sr = SelfRegistration.objects.create(
            name=data["name"],
            lastname=data["lastname"],
            phone_number=data.get("phone_number") or None,
            email=data.get("email") or None,
            details=data.get("details") or {},
            state=SelfRegistration.State.REGISTERED,
        )

        out = SelfRegistrationOutSerializer({
            "id": sr.id,
            "name": sr.name,
            "lastname": sr.lastname,
            "phone_number": str(sr.phone_number) if sr.phone_number else None,
            "email": sr.email,
            "details": sr.details,
            "state": sr.state,
            "created_at": sr.created_at,
        })
        return Response(out.data, status=status.HTTP_201_CREATED)

class ProtocolDetailView(APIView):
    """
    GET /api/v1/protocols/<meeting_id>/<protocol_num>/
    Auth: same as the other endpoints (Token/Bearer).
    Returns all questions with a 'response' (empty string if no answer).
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, meeting_id: int, protocol_num: int, *args, **kwargs):
        meeting = get_object_or_404(
            Meeting.objects.select_related("patient__navigator"),
            pk=meeting_id
        )
        # staff or CTN assigned to patient
        patient = meeting.patient
        if not (request.user.is_staff or (patient and patient.navigator_id == request.user.id)):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        protocol = get_object_or_404(Protocol, number=protocol_num)

        # Answers indexed by question id
        ans_map = {
            a.question_id: (a.response or "")
            for a in Answer.objects.filter(meeting=meeting, question__protocol=protocol)
        }

        # Keep a stable ordering; prefer an explicit field if you have one
        qs = protocol.questions.all().order_by("id")

        questions = [{
            "id": q.id,
            "prompt_md": getattr(q, "prompt_md", "") or "",
            "response": ans_map.get(q.id, ""),  # empty when not answered
        } for q in qs]

        data = {
            "meeting_id": meeting.id,
            "patient": {
                "id": patient.id if patient else None,
                "name": str(patient) if patient else None,
            },
            "protocol": {
                "number": protocol.number,
                "title": getattr(protocol, "title", "") or "",
                "description": getattr(protocol, "description", "") or "",
            },
            "questions": questions,
        }
        return Response(data, status=status.HTTP_200_OK)

class AlertListCreateView(APIView):
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Alert.objects.all().order_by("-created_at")
        if not request.user.is_staff:
            qs = qs.filter(user=request.user)
        payload = []
        for a in qs[:100]:
            payload.append(AlertOutSerializer({
                "id": a.id,
                "title": a.title or "",
                "description": a.description or "",
                "data": a.data,
                "alert_type": a.get_alert_type_display(),
                "priority": a.get_priority_display(),
                "status": a.get_status_display(),
                "user": a.user.username if a.user_id else "",
                "patient": str(a.patient) if a.patient_id else "",
                "created_at": a.created_at,
                "updated_at": a.updated_at,
                "acted_at": a.acted_at,
                "last_action_ok": a.last_action_ok,
                "last_action_status": a.last_action_status,
            }).data)
        return Response(payload)

    def post(self, request):
        ser = AlertInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data

        assignee = None
        if v.get("user_id"):
            assignee = User.objects.filter(id=v["user_id"]).first()

        subject_patient = None
        if v.get("patient_id"):
            subject_patient = Patient.objects.filter(id=v["patient_id"]).first()

        # AS-10/F3 fix: a non-staff caller may only reference a patient they own and
        # may only assign the alert to themselves (no cross-tenant / superuser targeting).
        if not request.user.is_staff:
            if subject_patient and subject_patient.navigator_id != request.user.id:
                return Response({"detail": "Forbidden: patient not owned by requester."},
                                status=status.HTTP_403_FORBIDDEN)
            if v.get("user_id") and v["user_id"] != request.user.id:
                return Response({"detail": "Forbidden: cannot assign to another user."},
                                status=status.HTTP_403_FORBIDDEN)

        # Default: if user_id missing, assign to creator (so there's always an owner)
        if not assignee:
            assignee = request.user

        if not assignee and not subject_patient:
            return Response({"detail": "Provide at least one of user_id or patient_id."}, status=400)

        a = Alert.objects.create(
            title=v.get("title", "") or "",
            description=v.get("description", "") or "",
            data=v.get("data") or {},
            alert_type=v.get("alert_type", Alert.AlertType.DEFAULT),
            priority=v.get("priority", Alert.Priority.MEDIUM),
            user=assignee,
            patient=subject_patient,
            created_by=request.user,
        )

        out = AlertOutSerializer({
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "data": a.data,
            "alert_type": a.get_alert_type_display(),
            "priority": a.get_priority_display(),
            "status": a.get_status_display(),
            "user": a.user.username if a.user_id else "",
            "patient": str(a.patient) if a.patient_id else "",
            "created_at": a.created_at,
            "updated_at": a.updated_at,
            "acted_at": a.acted_at,
            "last_action_ok": a.last_action_ok,
            "last_action_status": a.last_action_status,
        })
        return Response(out.data, status=status.HTTP_201_CREATED)



class AlertDetailView(APIView):
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        a = Alert.objects.filter(pk=pk).first()
        if not a:
            return Response({"detail": "Not found"}, status=404)

        if not request.user.is_staff:
            allowed = (a.user_id == request.user.id) or (a.patient_id and getattr(a.patient, "navigator_id", None) == request.user.id)
            if not allowed:
                return Response({"detail": "Forbidden"}, status=403)

        out = AlertOutSerializer({
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "data": a.data,
            "alert_type": a.get_alert_type_display(),
            "priority": a.get_priority_display(),
            "status": a.get_status_display(),
            "user": a.user.username if a.user_id else "",
            "patient": str(a.patient) if a.patient_id else "",
            "created_at": a.created_at,
            "updated_at": a.updated_at,
            "acted_at": a.acted_at,
            "last_action_ok": a.last_action_ok,
            "last_action_status": a.last_action_status,
        })
        return Response(out.data, status=200)

# class AlertResolveView(APIView):
#     authentication_classes = AUTH_CLASSES
#     permission_classes = [IsAuthenticated]

#     def post(self, request, pk: int):
#         a = get_object_or_404(Alert, pk=pk)

#         # Only staff or the alert owner can resolve
#         if not (request.user.is_staff or a.user_id == request.user.id):
#             return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

#         if a.status != Alert.AlertStatus.RESOLVED:
#             a.status = Alert.AlertStatus.RESOLVED
#             updates = ["status", "updated_at"]
#             # Optional timestamps if your model has them
#             if hasattr(a, "acted_at"):
#                 a.acted_at = timezone.now()
#                 updates.append("acted_at")
#             a.save(update_fields=updates)

#         out = AlertOutSerializer({
#             "id": a.id,
#             "title": a.title or "",
#             "description": a.description or "",
#             "data": a.data,
#             "alert_type": a.get_alert_type_display(),
#             "priority": a.get_priority_display(),
#             "status": a.get_status_display(),
#             "user": a.user.username,
#             "created_at": a.created_at,
#             "updated_at": a.updated_at,
#             "acted_at": getattr(a, "acted_at", None),
#             "last_action_ok": getattr(a, "last_action_ok", None),
#             "last_action_status": getattr(a, "last_action_status", None),
#         })
#         return Response(out.data, status=status.HTTP_200_OK)
class AlertResolveView(APIView):
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def post(self, request, pk: int):
        a = get_object_or_404(Alert, pk=pk)

        # Only staff or the alert owner can resolve
        if not (request.user.is_staff or a.user_id == request.user.id):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        if a.status != Alert.AlertStatus.RESOLVED:
            a.status = Alert.AlertStatus.RESOLVED
            updates = ["status", "updated_at"]
            # Optional timestamps if your model has them
            if hasattr(a, "acted_at"):
                a.acted_at = timezone.now()
                updates.append("acted_at")
            a.save(update_fields=updates)

        # Safely extract user and patient values
        user_name = a.user.username if a.user else ""
        patient_name = str(a.patient) if a.patient else ""

        out = AlertOutSerializer({
            "id": a.id,
            "title": a.title or "",
            "description": a.description or "",
            "data": a.data,
            "alert_type": a.get_alert_type_display(),
            "priority": a.get_priority_display(),
            "status": a.get_status_display(),
            "user": user_name,            # <--- Safely passes the value
            "patient": patient_name,      # <--- Key always exists now!
            "created_at": a.created_at,
            "updated_at": a.updated_at,
            "acted_at": getattr(a, "acted_at", None),
            "last_action_ok": getattr(a, "last_action_ok", None),
            "last_action_status": getattr(a, "last_action_status", None),
        })
        return Response(out.data, status=status.HTTP_200_OK)

class PatientsListView(APIView):
    """
    GET /api/v1/patients/?q=...
    - staff: all patients
    - CTN: only patients where navigator == request.user
    Optional ?q= filters by name/lastname/phone (icontains).
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        qs = Patient.objects.all().order_by("lastname", "name")
        if not request.user.is_staff:
            qs = qs.filter(navigator=request.user)

        q_raw = (request.query_params.get("q") or "").strip()
        # normalize: collapse whitespace, replace commas with space
        q = " ".join(q_raw.replace(",", " ").split())

        if q:
            tokens = q.split(" ")
            # Always allow simple matches + phone
            base_q = (
                Q(name__icontains=q) |
                Q(lastname__icontains=q) |
                Q(phone_number__icontains=q)
            )

            if len(tokens) >= 2:
                first = tokens[0]
                last  = tokens[-1]
                # full-name concatenation for substring search
                qs = qs.annotate(full_name=Concat("name", Value(" "), "lastname")).filter(
                    base_q |
                    Q(full_name__icontains=q) |
                    (Q(name__icontains=first) & Q(lastname__icontains=last)) |
                    (Q(name__icontains=last)  & Q(lastname__icontains=first))
                )
            else:
                qs = qs.filter(base_q)

        data = PatientOutSerializer(qs[:200], many=True).data
        return Response(data, status=status.HTTP_200_OK)


class MeetingCreateView(APIView):
    """
    POST /api/v1/meetings/
    Body: {
      "patient_id": int,
      "scheduled_time": ISO8601,
      "type": int (optional),
      "scheduled_protocols": [int, ...] (optional, protocol numbers),
      "scheduled_protocol": int|null (optional, deprecated — one protocol number)
    }

    Rules:
      - staff can schedule for any patient (meeting.navigator = request.user)
      - non-staff can schedule only for their own patients
      - conflict window: ±59 minutes for meetings where patient__navigator == request.user
    Returns:
      201 {"ok": true, "meeting": {...}}
      409 {"ok": false, "detail": "..."}
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        ser = MeetingCreateInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data

        patient = get_object_or_404(Patient, pk=v["patient_id"])

        # permission: non-staff can only act on their own patients
        if not request.user.is_staff and patient.navigator_id != request.user.id:
            return Response({"ok": False, "detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        # Prepare meeting (but don't save yet)
        meeting = Meeting(
            patient=patient,
            scheduled_time=v["scheduled_time"],
        )
        if "type" in v:
            meeting.type = v["type"]

        # Numbers in, protocols out. Both spellings are accepted; the singular
        # one is the shape this endpoint shipped with and means a list of one.
        # A number nothing answers to is an error rather than a silent no-op —
        # the old field took any integer 1..10 and most of them were nobody's
        # protocol, which is how calls ended up booked against nothing.
        wanted = list(v.get("scheduled_protocols") or [])
        if v.get("scheduled_protocol"):
            wanted.append(v["scheduled_protocol"])
        booked = list(Protocol.objects.filter(number__in=set(wanted)))
        missing = sorted(set(wanted) - {p.number for p in booked})
        if missing:
            return Response(
                {"ok": False,
                 "detail": "No protocol with number(s): %s"
                           % ", ".join(str(n) for n in missing)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Conflict check: ±59 minutes for THIS navigator's patient list
        mt = meeting.scheduled_time
        window_start = mt - timedelta(minutes=59)
        window_end   = mt + timedelta(minutes=59)

        conflict_qs = Meeting.objects.filter(
            patient__navigator=request.user,
            scheduled_time__gte=window_start,
            scheduled_time__lt=window_end,
        )
        if conflict_qs.exists():
            return Response(
                {"ok": False, "detail": "You already have a meeting within ±59 minutes."},
                status=status.HTTP_409_CONFLICT,
            )

        meeting.save()
        if booked:
            meeting.scheduled_protocols.set(booked)
        out = MeetingOutSerializer(meeting).data
        return Response({"ok": True, "meeting": out}, status=status.HTTP_201_CREATED)

class PatientProtocolsFilledView(APIView):
    """
    GET /api/v1/patients/<patient_id>/protocols/filled/
    Returns meetings for the patient where at least one answer exists:
    [{meeting_id, scheduled_time, protocol_numbers, protocol_number,
      answered_count}, ...]

    `protocol_number` is the first of `protocol_numbers` and is kept for
    clients written before a call could cover more than one.
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, patient_id: int, *args, **kwargs):
        patient = get_object_or_404(
            Patient.objects.select_related("navigator"),
            pk=patient_id
        )

        # permission: staff OR the patient's navigator
        if not request.user.is_staff and patient.navigator_id != request.user.id:
            return Response({"detail": "Forbidden"}, status=403)

        meetings = (
            Meeting.objects
            .filter(patient_id=patient.id)
            .prefetch_related("executed_protocols", "scheduled_protocols")
            .order_by("-scheduled_time")
        )

        payload = []
        for m in meetings:
            covered = (list(m.executed_protocols.all())
                       or list(m.scheduled_protocols.all()))
            if not covered:
                continue

            answered_count = Answer.objects.filter(meeting=m).count()
            if answered_count == 0:
                continue

            numbers = sorted(p.number for p in covered)
            payload.append({
                "meeting_id": m.id,
                "scheduled_time": m.scheduled_time,
                "protocol_numbers": numbers,
                "protocol_number": numbers[0],
                "answered_count": answered_count,
            })

        return Response(payload, status=200)


class ProtocolQuestionsView(APIView):
    """
    GET /api/v1/protocols/<protocol_num>/questions/
    Returns only the questions (id, order, prompt_md) for a protocol.
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, protocol_num: int, *args, **kwargs):
        protocol = get_object_or_404(Protocol, number=protocol_num)
        qs = protocol.questions.all().order_by("order", "id")
        data = [{
            "id": q.id,
            "order": q.order,
            "prompt_md": q.prompt_md or "",
        } for q in qs]
        return Response({
            "protocol": {
                "number": protocol.number,
                "title": protocol.title,
                "description": protocol.description or "",
            },
            "questions": data,
        }, status=status.HTTP_200_OK)

class MeetingAnswersUpsertView(APIView):
    """
    POST /api/v1/meetings/<meeting_id>/answers/
    Upsert (create/update/delete) answers in bulk for a meeting.
    - If response == "" (blank), the answer is deleted.
    - If protocol_number is provided, all questions must belong to that protocol.
    - If clear_others == true and protocol_number provided, remove any other
      answers for that protocol not present in 'answers'.
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, meeting_id: int, *args, **kwargs):
        meeting = get_object_or_404(
            Meeting.objects.select_related("patient__navigator"),
            pk=meeting_id
        )
        # Permission: staff or navigator of the patient
        if not (request.user.is_staff or meeting.patient.navigator_id == request.user.id):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        ser = MeetingAnswersUpsertInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data

        answers_in = v["answers"]
        protocol_number = v.get("protocol_number")
        clear_others = v.get("clear_others", False)

        # Optional protocol validation
        allowed_qids = None
        if protocol_number is not None:
            proto = get_object_or_404(Protocol, number=protocol_number)
            allowed_qids = set(proto.questions.values_list("id", flat=True))

        # Validate questions exist (+ protocol if provided)
        qids = [item["question_id"] for item in answers_in]
        q_map = {q.id: q for q in Question.objects.filter(id__in=qids)}
        missing = [qid for qid in qids if qid not in q_map]
        if missing:
            return Response({"detail": f"Unknown question_id(s): {missing}"}, status=400)

        if allowed_qids is not None:
            bad = [qid for qid in qids if qid not in allowed_qids]
            if bad:
                return Response({"detail": f"Questions not in protocol {protocol_number}: {bad}"}, status=400)

        # Upsert
        existing = {
            a.question_id: a
            for a in Answer.objects.filter(meeting=meeting, question_id__in=qids)
        }

        created = updated = deleted = 0
        kept_qids = set()

        for item in answers_in:
            qid = item["question_id"]
            text = (item["response"] or "").strip()
            kept_qids.add(qid)

            curr = existing.get(qid)
            if text:
                if curr:
                    if curr.response != text:
                        curr.response = text
                        curr.save(update_fields=["response"])
                        updated += 1
                else:
                    Answer.objects.create(meeting=meeting, question_id=qid, response=text)
                    created += 1
            else:
                if curr:
                    curr.delete()
                    deleted += 1

        # Optionally clear others in that protocol not present in this payload
        if clear_others and protocol_number is not None:
            to_clear = Answer.objects.filter(
                meeting=meeting,
                question__protocol__number=protocol_number
            ).exclude(question_id__in=kept_qids)
            deleted += to_clear.count()
            to_clear.delete()

        # Return all answers for this meeting (+protocol filter if present)
        final_qs = Answer.objects.filter(meeting=meeting)
        if protocol_number is not None:
            final_qs = final_qs.filter(question__protocol__number=protocol_number)

        final = [{
            "question_id": a.question_id,
            "response": a.response
        } for a in final_qs.order_by("question__order", "question_id")]

        return Response({
            "ok": True,
            "meeting_id": meeting.id,
            "protocol_number": protocol_number,
            "counts": {"created": created, "updated": updated, "deleted": deleted},
            "answers": final,
        }, status=status.HTTP_200_OK)


class PatientMeetingsListView(APIView):
    """
    GET /api/v1/patients/<patient_id>/meetings/
    Returns all meetings for a patient (most recent first).
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, patient_id: int, *args, **kwargs):
        patient = get_object_or_404(Patient.objects.select_related("navigator"), pk=patient_id)

        if not (request.user.is_staff or patient.navigator_id == request.user.id):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        meetings = (
            Meeting.objects
            .filter(patient_id=patient.id)
            .select_related("patient")
            .order_by("-scheduled_time")
        )
        data = MeetingOutSerializer(meetings, many=True).data
        return Response(data, status=status.HTTP_200_OK)

class PatientDetailsAppendView(APIView):
    """
    POST /api/v1/patients/<patient_id>/details/append/
    Body: { "text": "...", "target": "details_editable" | "details" }

    Append-only: does NOT allow deletion or overwrite.
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, patient_id: int, *args, **kwargs):
        ser = PatientDetailsAppendInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data

        # Lock row so concurrent appends don't overwrite each other
        patient = (
            Patient.objects
            .select_for_update()
            .select_related("navigator")
            .filter(pk=patient_id)
            .first()
        )
        if not patient:
            return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        # Permission: staff OR patient's navigator
        if not (request.user.is_staff or (patient.navigator_id == request.user.id)):
            return Response({"detail": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        target = v.get("target") or "details_editable"
        text = v["text"]

        now = timezone.now()

        # Append using the helper
        try:
            if target == "details":
                updated = patient.append_details_markdown(text, editable=False)
                patient.save(update_fields=["details"])
            else:
                updated = patient.append_details_markdown(text, editable=True)
                patient.save(update_fields=["details_editable"])
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        out = PatientDetailsAppendOutSerializer({
            "patient_id": patient.id,
            "target": target,
            "appended_at": now,
            "details": updated,
        })
        return Response(out.data, status=status.HTTP_200_OK)

class ConversationVisibilityView(APIView):
    """
    GET  /api/v1/conversations/<conversation_id>/visibility/
    POST /api/v1/conversations/<conversation_id>/visibility/
    Authorization: Bearer <token>  (or "Token <key>" under DRF TokenAuthentication)
    Body (POST): { "hidden": true }

    Whether this conversation's content is readable by the client's link
    worker. This is the contract behind the agent tool that asks the client
    the question — see ConvAI/native_agents/privacy_tool.py and
    conversation_privacy.md.

    404 rather than 403 in every refusal, the feature being switched off
    included. A caller who may not touch this conversation should not learn
    from the status code whether it exists, and an installation that never
    turned the feature on has no endpoint to find.

    Idempotent on purpose: an agent whose client says "hide it" twice should
    not have to care, and the response always describes the state the
    conversation is now in rather than what changed.
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        conv = self._conversation(request, conversation_id)
        return Response(self._out(conv), status=status.HTTP_200_OK)

    def post(self, request, conversation_id):
        from .. import conversation_privacy

        conv = self._conversation(request, conversation_id)
        ser = ConversationVisibilityInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        hidden = ser.validated_data["hidden"]
        conversation_privacy.set_hidden(conv, hidden)
        # Logged with the acting account because a service token can write to
        # any conversation (see ConvAI.conversation_actors). The log is what
        # makes that traceable after the fact.
        logger.info("Conversation %s visibility set to hidden=%s by %s",
                    conv.id, hidden, request.user.get_username())
        return Response(self._out(conv), status=status.HTTP_200_OK)

    @staticmethod
    def _conversation(request, conversation_id):
        from django.http import Http404
        from .. import conversation_actors, conversation_privacy

        if not conversation_privacy.enabled():
            raise Http404
        try:
            conv_uuid = uuid.UUID(str(conversation_id))
        except (TypeError, ValueError):
            raise Http404
        conv = (Conversation.objects
                .select_related("patient__tester_account")
                .filter(id=conv_uuid).first())
        if conv is None:
            raise Http404

        # Whose conversation it is. One rule, in conversation_actors, shared
        # with the summary endpoint — and bound to the *patient* rather than to
        # a user account, because on WhatsApp and SMS there is no account to
        # bind to: save_message has never set Conversation.user. A navigator is
        # deliberately not on the list; the switch is the client's own answer
        # about their own privacy, and a link worker setting it on their behalf
        # would make it worth nothing.
        if not conversation_actors.may_act_on(conv, request.user):
            raise Http404
        return conv

    @staticmethod
    def _out(conv):
        # Counted over the conversation key, which is what a navigator sees on
        # the divider — so the number the agent reads back to the client is the
        # same number the link worker is left with.
        count = Message.objects.filter(conversation_id=str(conv.id)).count()
        return ConversationVisibilityOutSerializer({
            "conversation_id": str(conv.id),
            "hidden": conv.hidden,
            "hidden_at": conv.hidden_at,
            "message_count": count,
        }).data


class ConversationSummaryView(APIView):
    """
    GET  /api/v1/conversations/<conversation_id>/summary/
    POST /api/v1/conversations/<conversation_id>/summary/
    Authorization: Bearer <token>  (or "Token <key>" under DRF TokenAuthentication)
    Body (POST): { "summary": "..." }

    What the agent that held this conversation says it was about. The contract
    behind the `report_summary` tool, for remote agents that cannot reach the
    ORM — see ConvAI/native_agents/summary_tool.py and agent_tools.md.

    Deliberately **not** behind a feature switch, unlike the sibling visibility
    endpoint. Gating that one is load-bearing: it decides whether the platform
    may make a promise to a client. A summary is the same kind of thing the
    classifier already writes unasked on every conversation, so a switch would
    only mean a fresh installation's agents fail silently.

    404 rather than 403 in every refusal, matching the visibility endpoint: a
    caller who may not touch this conversation should not learn from the status
    code whether it exists.

    Idempotent. Each report replaces the last, and the response describes the
    state the conversation is now in rather than what changed — a conversation
    grows while it is happening, so the agent's latest reading of it is the one
    worth keeping.
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        conv = self._conversation(request, conversation_id)
        return Response(self._out(conv), status=status.HTTP_200_OK)

    def post(self, request, conversation_id):
        from .. import conversation_summary

        conv = self._conversation(request, conversation_id)
        ser = ConversationSummaryInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        text = ser.validated_data["summary"]
        conversation_summary.set_agent_summary(conv, text)
        # Logged with the acting account because a service token can write to
        # any conversation (see ConvAI.conversation_actors). The length, not the
        # text: a summary is content, and the application log is not where
        # content belongs.
        logger.info("Agent summary recorded for conversation %s (%d chars) by %s",
                    conv.id, len(text), request.user.get_username())
        return Response(self._out(conv), status=status.HTTP_200_OK)

    @staticmethod
    def _conversation(request, conversation_id):
        from django.http import Http404
        from .. import conversation_actors

        try:
            conv_uuid = uuid.UUID(str(conversation_id))
        except (TypeError, ValueError):
            raise Http404
        conv = (Conversation.objects
                .select_related("patient__tester_account")
                .filter(id=conv_uuid).first())
        if conv is None:
            raise Http404
        if not conversation_actors.may_act_on(conv, request.user):
            raise Http404
        return conv

    @staticmethod
    def _out(conv):
        from .. import conversation_summary

        text, source, _when = conversation_summary.machine_summary(conv)
        return ConversationSummaryOutSerializer({
            "conversation_id": str(conv.id),
            "summary": text,
            "source": source,
            "agent_summary": conv.agent_summary or "",
            "agent_summary_at": conv.agent_summary_at,
            "hidden": conv.hidden,
        }).data


class _RunView(APIView):
    """Base for the endpoints a remote agent calls about the run it is in.

    Authenticated **only** by a run token (ConvAI.run_tokens), and the token
    decides the conversation: none of these take an id. That is the difference
    from ``/conversations/<id>/…``, which exist for people with personal tokens:
    here there is no id for the agent, or a model inside it, to forge or get
    wrong, and a token issued for one conversation cannot reach another.

    Every refusal of a valid token is a 404, as elsewhere in this API: the
    conversation missing, or a scope the token was not given.
    """
    authentication_classes = [RunTokenAuthentication]
    permission_classes = [IsAuthenticated]
    scope = None

    def _conversation(self, request):
        from django.http import Http404

        claims = request.auth
        if self.scope and not claims.allows(self.scope):
            raise Http404
        try:
            conv_uuid = uuid.UUID(claims.conversation_id)
        except (TypeError, ValueError):
            raise Http404
        conv = Conversation.objects.filter(id=conv_uuid).first()
        if conv is None:
            raise Http404
        # The token names the client it was issued for. A conversation now on a
        # different client's file is not the one this token is about.
        if claims.patient_id and conv.patient_id and conv.patient_id != claims.patient_id:
            raise Http404
        return conv

    @staticmethod
    def _out(conv, claims):
        from .. import conversation_privacy, conversation_summary

        text, source, _when = conversation_summary.machine_summary(conv)
        return RunOutSerializer({
            "conversation_id": str(conv.id),
            "patient_id": conv.patient_id,
            "summary": text,
            "source": source,
            "hidden": conv.hidden,
            "privacy_available": conversation_privacy.enabled(),
            "scopes": list(claims.scopes),
            "expires_at": claims.expires,
        }).data


class RunView(_RunView):
    """
    GET /api/v1/run/
    Authorization: RunToken <cc_run_token from the run config>

    The conversation this run is about, as the agent is allowed to see it.
    """

    def get(self, request):
        conv = self._conversation(request)
        return Response(self._out(conv, request.auth), status=status.HTTP_200_OK)


class RunSummaryView(_RunView):
    """
    POST /api/v1/run/summary/   {"summary": "..."}

    The agent's summary of this run's conversation. Same rules as the
    per-conversation endpoint: replaces the last, blank and over-long refused.
    """
    scope = "summary"

    def post(self, request):
        from .. import conversation_summary

        conv = self._conversation(request)
        ser = ConversationSummaryInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        text = ser.validated_data["summary"]
        conversation_summary.set_agent_summary(conv, text)
        logger.info("Agent summary recorded for conversation %s (%d chars) by %s",
                    conv.id, len(text), request.user.get_username())
        return Response(self._out(conv, request.auth), status=status.HTTP_200_OK)


class RunVisibilityView(_RunView):
    """
    POST /api/v1/run/visibility/   {"hidden": true}

    The client's answer about whether their link worker may read this
    conversation. 404 while CONVERSATION_PRIVACY_ENABLED is off, as the
    per-conversation endpoint is.
    """
    scope = "visibility"

    def _conversation(self, request):
        from django.http import Http404
        from .. import conversation_privacy

        if not conversation_privacy.enabled():
            raise Http404
        return super()._conversation(request)

    def post(self, request):
        from .. import conversation_privacy

        conv = self._conversation(request)
        ser = ConversationVisibilityInSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        hidden = ser.validated_data["hidden"]
        conversation_privacy.set_hidden(conv, hidden)
        logger.info("Conversation %s visibility set to hidden=%s by %s",
                    conv.id, hidden, request.user.get_username())
        return Response(self._out(conv, request.auth), status=status.HTTP_200_OK)


class EnrolmentLookupView(APIView):
    """
    GET /api/v1/enrolments/?phone=+447700900000
    GET /api/v1/enrolments/?code=maple-crane-frost

    Whether somebody is enrolled in a study, for an external agent that needs to
    know who it is talking to before it answers.

    This replaces an endpoint in the implementation it came from that was
    unauthenticated and CSRF-exempt, and returned a participant's name for any
    phone number posted to it — a lookup oracle for whether a given person is in
    a study. Here it needs the same bearer token as everything else, and 404s
    entirely while study enrolment is switched off.

    It deliberately does not return the access code: the code is a credential,
    and reading it back out over the API would make a token that can list
    participants into a token that can impersonate them.
    """
    authentication_classes = AUTH_CLASSES
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        from django.http import Http404

        from .. import enrolment as enrolment_service
        from ..enrolment.codes import normalise_code
        from ..models import Enrolment

        if not enrolment_service.enabled():
            raise Http404

        phone = (request.query_params.get("phone") or "").strip()
        code = normalise_code(request.query_params.get("code") or "")
        if not phone and not code:
            return Response(
                {"detail": "Provide either ?phone= or ?code=."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Newest first: one number can have been enrolled more than once (a
        # withdrawal and a later re-enrolment), and the latest is the live one.
        qs = Enrolment.objects.select_related("study", "patient").order_by("-created_at")
        row = qs.filter(access_code=code).first() if code else qs.filter(phone_number=phone).first()

        if row is None:
            return Response({"enrolled": False}, status=status.HTTP_200_OK)

        latest = row.latest_consent
        return Response({
            "enrolled": True,
            "name": row.name,
            "lastname": row.lastname,
            "study": row.study.slug,
            "study_name": row.study.display_name,
            "status": row.status,
            "patient_id": row.patient_id,
            "consent": {
                "given": latest is not None,
                "version": latest.consent_version if latest else None,
                "current": row.consent_is_current,
                "at": latest.agreed_at.isoformat() if latest else None,
            },
        }, status=status.HTTP_200_OK)
