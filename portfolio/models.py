"""Портфолио из визитов: фото работы в карточке записи и старые работы мастера без визита."""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from bookings.models import Appointment, Company, Master, Service


class WorkPhoto(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='work_photos')
    master = models.ForeignKey(Master, on_delete=models.CASCADE, related_name='work_photos')
    # Пусто — старая работа, загруженная мастером без визита
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, null=True, blank=True,
                                    related_name='work_photos')
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True, related_name='work_photos')
    image = models.ImageField(upload_to='works/')
    # Мастер: показывать на сайте записи (если на фото лицо — только с разрешения клиента)
    public = models.BooleanField(default=True)
    # Владелец или администратор скрыл фото с сайта
    hidden = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['master', 'public', 'hidden'])]

    def __str__(self):
        return f'{self.master} · {self.image.name}'

    @property
    def on_site(self):
        return self.public and not self.hidden
