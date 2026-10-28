"""Правила салона для клиента: до какого момента запись можно отменить самому."""
from datetime import datetime, timedelta

from django.utils import timezone


def starts_at(appointment):
    return timezone.make_aware(datetime.combine(appointment.date, appointment.start_time))


def client_can_cancel(appointment, now=None):
    """Запись предстоящая и до начала больше, чем company.cancel_hours."""
    now = now or timezone.now()
    if appointment.status not in ('pending', 'confirmed'):
        return False
    return starts_at(appointment) - now > timedelta(hours=appointment.company.cancel_hours)


def cancel_rule_text(company):
    text = f'Отменить запись можно не позже чем за {company.cancel_hours} ч до визита.'
    if company.phone:
        text += f' Позвоните в салон: {company.phone}'
    return text
