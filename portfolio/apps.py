from django.apps import AppConfig


class PortfolioConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'portfolio'
    verbose_name = 'Портфолио работ'

    def ready(self):
        from . import signals  # noqa: F401 — удаление файла вместе с фото
