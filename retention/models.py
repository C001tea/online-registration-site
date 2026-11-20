"""Ритм клиента: кому и когда напомнили записаться, кто попросил не писать."""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from bookings.models import Company


class RhythmMessage(models.Model):
    """Клиента позвали записаться (сам бот или сотрудник вручную) в одном цикле ритма."""
    CHANNEL_CHOICES = [
        ('telegram', 'Telegram'),
        ('email', 'Email'),
        ('whatsapp', 'WhatsApp'),
        ('viber', 'Viber'),
        ('phone', 'Звонок'),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='rhythm_messages')
    phone = models.CharField(max_length=20)
    # Группа услуг: 'c<id категории>' или 's<id услуги>' (услуга без категории)
    group = models.CharField(max_length=20)
    # Дата последнего визита в группе: следующий визит начинает новый цикл
    cycle = models.DateField()
    step = models.PositiveSmallIntegerField(default=1)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    sent_at = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        indexes = [models.Index(fields=['company', 'phone'])]

    def __str__(self):
        return f'{self.phone} {self.group} {self.cycle} → {self.channel}'


class ClientOptOut(models.Model):
    """Клиент попросил не присылать такие сообщения (по телефону в салоне)."""
    KIND_CHOICES = [('rhythm', 'Напоминания «пора записаться»'), ('all', 'Все сообщения')]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='client_opt_outs')
    phone = models.CharField(max_length=20)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company', 'phone', 'kind'], name='unique_client_opt_out')]
