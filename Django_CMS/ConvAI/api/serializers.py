# ConvAI/api/serializers.py
from rest_framework import serializers
from ..models import Alert, Patient, Meeting
from django.contrib.auth import get_user_model

User = get_user_model()

class MessageInSerializer(serializers.Serializer):
    text = serializers.CharField(max_length=5000, allow_blank=False)

class MessageOutSerializer(serializers.Serializer):
    conversation_id = serializers.CharField()
    user_message = serializers.CharField()
    response_message = serializers.CharField()
    restarted = serializers.BooleanField()

class SelfRegistrationInSerializer(serializers.Serializer):
    name         = serializers.CharField(max_length=120)
    lastname     = serializers.CharField(max_length=120)
    phone_number = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    email        = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    details      = serializers.DictField(required=False)

class SelfRegistrationOutSerializer(serializers.Serializer):
    id           = serializers.IntegerField()
    name         = serializers.CharField()
    lastname     = serializers.CharField()
    phone_number = serializers.CharField(allow_null=True)
    email        = serializers.EmailField(allow_null=True)
    details      = serializers.DictField()
    state        = serializers.IntegerField()
    created_at   = serializers.DateTimeField()

class AlertInSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    data = serializers.JSONField(required=False)
    alert_type = serializers.ChoiceField(choices=Alert.AlertType.choices, required=False, default=Alert.AlertType.DEFAULT)
    priority = serializers.ChoiceField(choices=Alert.Priority.choices, required=False, default=Alert.Priority.MEDIUM)
    user_id = serializers.IntegerField(required=False, help_text="Assignee user id (defaults to requester)")
    patient_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        user_id = attrs.get("user_id")
        patient_id = attrs.get("patient_id")
        if not user_id and not patient_id:
            raise serializers.ValidationError("Provide at least one of user_id or patient_id.")
        return attrs


class AlertOutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    description = serializers.CharField()
    data = serializers.JSONField()
    alert_type = serializers.CharField()
    priority = serializers.CharField()
    status = serializers.CharField()
    # ownership / subject
    user = serializers.CharField(allow_blank=True)
    patient = serializers.CharField(allow_blank=True)

    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    acted_at = serializers.DateTimeField(allow_null=True)
    last_action_ok = serializers.BooleanField()
    last_action_status = serializers.IntegerField(allow_null=True)

class PatientOutSerializer(serializers.ModelSerializer):
    phone_number = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = ["id", "name", "lastname", "phone_number"]

    def get_phone_number(self, obj):
        return str(obj.phone_number) if obj.phone_number else None


class MeetingCreateInSerializer(serializers.Serializer):
    patient_id = serializers.IntegerField()
    scheduled_time = serializers.DateTimeField()
    type = serializers.IntegerField(required=False)
    scheduled_protocol = serializers.IntegerField(required=False, allow_null=True)


class MeetingOutSerializer(serializers.ModelSerializer):
    patient = PatientOutSerializer(read_only=True)
    type_display = serializers.CharField(source="get_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    scheduled_protocol_display = serializers.SerializerMethodField()

    class Meta:
        model = Meeting
        fields = [
            "id",
            "scheduled_time",
            "created_time",
            "type", "type_display",
            "status", "status_display",
            "scheduled_protocol", "scheduled_protocol_display",
            "patient",
        ]

    def get_scheduled_protocol_display(self, obj):
        return obj.get_scheduled_protocol_display() if obj.scheduled_protocol else None

class AnswerUpsertItemSerializer(serializers.Serializer):
    question_id = serializers.IntegerField()
    response = serializers.CharField(allow_blank=True)

class MeetingAnswersUpsertInSerializer(serializers.Serializer):
    answers = AnswerUpsertItemSerializer(many=True)
    protocol_number = serializers.IntegerField(required=False)
    clear_others = serializers.BooleanField(required=False, default=False)

class PatientDetailsAppendInSerializer(serializers.Serializer):
    text = serializers.CharField(allow_blank=False, max_length=8000)
    # Optional switch; default to details_editable since it sounds like the “working” field
    target = serializers.ChoiceField(
        choices=["details", "details_editable"],
        required=False,
        default="details_editable",
    )

class PatientDetailsAppendOutSerializer(serializers.Serializer):
    patient_id = serializers.IntegerField()
    target = serializers.CharField()
    appended_at = serializers.DateTimeField()
    details = serializers.CharField()  # the updated markdown blob