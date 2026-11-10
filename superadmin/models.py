from django.contrib.auth.models import User
from django.db import models


class SuperAdminTOTP(models.Model):
    """Вторая ступень входа супер-админа: секрет приложения-аутентификатора и резервные коды."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='superadmin_totp')
    secret = models.CharField(max_length=64)
    # Пусто — QR-код ещё не отсканирован и первый код не введён (секрет можно выдать заново)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # Последний принятый шаг TOTP: один и тот же код дважды не принимается
    last_step = models.BigIntegerField(default=0)
    # Хеши неиспользованных резервных кодов
    backup_codes = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name = 'Код супер-админа'
        verbose_name_plural = 'Коды супер-админа'

    def __str__(self):
        return f'TOTP {self.user}'


class SuperAdminLog(models.Model):
    """Журнал действий в супер-админке: вход, блокировки, продления, вход в салон."""
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    action = models.CharField(max_length=40)
    company = models.ForeignKey('bookings.Company', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    details = models.JSONField(default=dict, blank=True)
    ip = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name = 'Действие супер-админа'
        verbose_name_plural = 'Журнал супер-админки'

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.action}'
