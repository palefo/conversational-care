from django.contrib import admin

from .models import AgentDataSource, FieldAppConnection, FieldAppInstallation, PatientFieldAppLink


@admin.register(FieldAppInstallation)
class FieldAppInstallationAdmin(admin.ModelAdmin):
    list_display = ("name", "base_url", "api_url", "is_active")


@admin.register(FieldAppConnection)
class FieldAppConnectionAdmin(admin.ModelAdmin):
    list_display = ("user", "installation", "remote_username", "connected_at", "broken_at")
    exclude = ("token",)


@admin.register(PatientFieldAppLink)
class PatientFieldAppLinkAdmin(admin.ModelAdmin):
    list_display = ("patient", "installation", "athlete_id", "athlete_name", "linked_by", "linked_at")


@admin.register(AgentDataSource)
class AgentDataSourceAdmin(admin.ModelAdmin):
    list_display = ("agent", "installation", "enabled", "rounds")
