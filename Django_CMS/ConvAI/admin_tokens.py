# ConvAI/admin_tokens.py  (or put inside your existing admin.py)
from django.contrib import admin
from rest_framework.authtoken.models import Token
from .models import ConvAIUser

@admin.action(description="Regenerate API token")
def regenerate_api_token(modeladmin, request, queryset):
    for user in queryset:
        Token.objects.filter(user=user).delete()
        Token.objects.create(user=user)

@admin.action(description="Create API token if missing")
def create_api_token_if_missing(modeladmin, request, queryset):
    for user in queryset:
        Token.objects.get_or_create(user=user)

@admin.register(ConvAIUser)
class ConvAIUserAdmin(admin.ModelAdmin):
    list_display = ("username", "email", "is_staff", "phone_number", "agent")
    actions = [create_api_token_if_missing, regenerate_api_token]