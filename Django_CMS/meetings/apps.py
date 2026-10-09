from django.apps import AppConfig


class MeetingsAppConfig(AppConfig):
    name = "meetings"
    verbose_name = "Online meetings"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Cheap by construction: the provider and the signal receivers import
        # nothing heavier than Django itself. LiveKit is spoken to over plain
        # HTTP with requests + PyJWT, both already loaded by the core, so an
        # installation with the feature switched off pays for a few modules
        # and nothing else.
        from ConvAI import extensions

        from . import lifecycle, provider

        extensions.register_online_provider(provider.OnlineMeetingsProvider())
        lifecycle.connect()
