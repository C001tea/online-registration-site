"""Отзывы после визита: бот (или письмо) спрашивает оценку, низкие сразу уходят владельцу."""
import secrets

from django.db import models
from django.utils import timezone

from bookings.models import Appointment, Company, Master


def new_token():
    return secrets.token_urlsafe(18)


class VisitReview(models.Model):
    """Запрос оценки по визиту и ответ клиента (rating пуст, пока клиент не ответил)."""
    CHANNEL_CHOICES = [('telegram', 'Telegram'), ('email', 'Email')]
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name='visit_review')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='visit_reviews')
    master = models.ForeignKey(Master, on_delete=models.SET_NULL, null=True, blank=True, related_name='visit_reviews')
    phone = models.CharField(max_length=20)
    # Ссылка из письма: /api/review/<token>/
    token = models.CharField(max_length=32, unique=True, default=new_token)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    chat_id = models.BigIntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(default=timezone.now)
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    comment = models.TextField(blank=True)
    # Клиент разрешил показать отзыв на сайте записи (имя — только первое слово)
    publish = models.BooleanField(default=False)
    rated_at = models.DateTimeField(null=True, blank=True)
    # Ждём комментарий ответным сообщением в Telegram
    awaiting_comment = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=['company', 'phone']), models.Index(fields=['master', 'rating'])]

    def __str__(self):
        return f'{self.appointment_id}: {self.rating or "—"}'

    @property
    def is_low(self):
        return self.rating is not None and self.rating <= 3
