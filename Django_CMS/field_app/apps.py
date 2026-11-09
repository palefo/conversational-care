from django.apps import AppConfig


class FieldAppConfig(AppConfig):
    name = "field_app"
    verbose_name = "Field App integration"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from ConvAI import extensions

        from .addon import FieldAppAddon

        extensions.register_addon(FieldAppAddon())
