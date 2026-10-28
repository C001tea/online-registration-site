import secrets

from django.contrib.auth.models import User
from django.db import models

from bookings.models import Appointment


def new_token():
    # Параметр /start в Telegram: до 64 символов [A-Za-z0-9_-]
    return secrets.token_urlsafe(16)


class AppointmentTelegram(models.Model):
    """Привязка записи к чату клиента: ссылка-приглашение и отметки напоминаний."""
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name='telegram')
    token = models.CharField(max_length=32, unique=True, default=new_token)
    chat_id = models.BigIntegerField(null=True, blank=True)
    reminder_24h_sent = models.BooleanField(default=False)
    reminder_1h_sent = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.appointment} → {self.chat_id or "не привязан"}'


class ClientTelegram(models.Model):
    """Чат клиента с аккаунтом: следующие записи приходят в Telegram без ссылки."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='client_telegram')
    chat_id = models.BigIntegerField()
    linked_at = models.DateTimeField(auto_now=True)


class StaffTelegram(models.Model):
    """Чат сотрудника салона для уведомлений о записях."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staff_telegram')
    chat_id = models.BigIntegerField()
    linked_at = models.DateTimeField(auto_now=True)


class StaffLinkToken(models.Model):
    """Одноразовая ссылка для подключения Telegram сотрудником (живёт STAFF_LINK_TTL)."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='telegram_link_tokens')
    token = models.CharField(max_length=32, unique=True, default=new_token)
    created_at = models.DateTimeField(auto_now_add=True)


class OutgoingMessage(models.Model):
    """Очередь сообщений: пишется вместе с записью в одной транзакции,
    отправляет процесс telegram_worker (с повторами, если Telegram недоступен)."""
    chat_id = models.BigIntegerField()
    text = models.TextField()
    buttons = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=['sent_at', 'next_attempt_at'])]

    def __str__(self):
        return f'{self.chat_id}: {self.text[:40]}'


class TelegramLogin(models.Model):
    """Вход клиента на сайт через бота: ссылка → контакт в боте → код → вход на сайте."""
    token = models.CharField(max_length=32, unique=True, default=new_token)
    created_at = models.DateTimeField(auto_now_add=True)
    chat_id = models.BigIntegerField(null=True, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True, related_name='+')
    code = models.CharField(max_length=6, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    used = models.BooleanField(default=False)
    lang = models.CharField(max_length=2, default='ru')  # язык сайта: на нём отвечает бот
