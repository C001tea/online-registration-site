from django.conf import settings
from django.db import models

from bookings.models import HEX_COLOR_VALIDATOR, Company


class ClientNote(models.Model):
    """Заметка салона о клиенте (клиент — номер телефона из записей; аккаунт не обязателен)."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='client_notes')
    phone = models.CharField(max_length=20)
    text = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company', 'phone'], name='unique_client_note')]


class StaffAppearance(models.Model):
    """Оформление панели у сотрудника: тема и цвет акцента. У каждого своё (мастер, админ, владелец)."""
    THEMES = [('dark', 'Тёмная'), ('light', 'Светлая'), ('system', 'Как в системе')]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='staff_appearance')
    theme = models.CharField(max_length=10, choices=THEMES, default='dark')
    # Пусто — фирменный розовый
    accent = models.CharField(max_length=7, blank=True, default='', validators=[HEX_COLOR_VALIDATOR])
    updated_at = models.DateTimeField(auto_now=True)
