from django.apps import AppConfig


class LedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ledger"
    label = "ledger"
    verbose_name = "Ledger"

    def ready(self) -> None:  # pragma: no cover
        from . import signals  # noqa: F401
