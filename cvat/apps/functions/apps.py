from django.apps import AppConfig


class FunctionsConfig(AppConfig):
    default_auto_field = "django.db.models.AutoField"
    name = "cvat.apps.functions"
    verbose_name = "Functions"

    def ready(self) -> None:  # pragma: no cover - import side effects
        from . import signals  # noqa: F401
