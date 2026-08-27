# ConvAI/api/views.py
from __future__ import annotations

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
    PatientDetailsAppendOutSerializer
)
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

    Message.objects.create(
        user=identity,
        conversation_id=str(conv.id),
        user_message=user_text,
        response_message=bot_text,
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