"""Правила салона для клиента: до какого момента запись можно отменить самому."""
from datetime import datetime, timedelta

from django.utils import timezone

from .i18n import tr


def starts_at(appointment):
    return timezone.make_aware(datetime.combine(appointment.date, appointment.start_time))


def client_can_cancel(appointment, now=None):
    """Запись предстоящая и до начала больше, чем company.cancel_hours."""
    now = now or timezone.now()
    if appointment.status not in ('pending', 'confirmed'):
        return False
    return starts_at(appointment) - now > timedelta(hours=appointment.company.cancel_hours)


def cancel_rule_text(company, lang='ru'):
    text = tr('Отменить запись можно не позже чем за {hours} ч до визита.', lang, hours=company.cancel_hours)
    if company.phone:
        text += tr(' Позвоните в салон: {phone}', lang, phone=company.phone)
    return text
