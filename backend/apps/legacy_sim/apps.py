from django.apps import AppConfig


class LegacySimConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.legacy_sim"
    label = "legacy_sim"
    verbose_name = "Legacy ERP simulator"
