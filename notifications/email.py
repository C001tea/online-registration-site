"""Письма клиенту о записи: подтверждение (со ссылкой «Перенести или отменить») и напоминание.

Для клиентов без Telegram: письмо уходит, если при записи указан email и почта настроена.
Отправляет фоновый процесс (telegram_worker) раз в минуту — запрос записи не ждёт SMTP.
"""
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from bookings.i18n import MONTHS, WEEKDAYS, tr
from bookings.models import Appointment
from bookings.slots import ACTIVE_STATUSES

logger = logging.getLogger(__name__)

# Напоминание по умолчанию (клиент не выбирал) — за сутки
DEFAULT_REMINDER_MINUTES = 24 * 60
# Записался позже, чем за (напоминание + это время) до визита — напоминание не нужно
REMINDER_MIN_GAP = timedelta(minutes=30)
# Подтверждение шлём только по свежим записям (не разбираем старые после простоя)
CONFIRM_MAX_AGE = timedelta(days=1)
BATCH = 50


def enabled():
    return bool(getattr(settings, 'EMAIL_CONFIGURED', False))


def starts_at(appointment):
    return timezone.make_aware(datetime.combine(appointment.date, appointment.start_time))


def when_text(appointment, lang):
    d = appointment.date
    return f'{WEEKDAYS[lang][d.weekday()]}, {d.day} {MONTHS[lang][d.month - 1]}, {appointment.start_time:%H:%M}'


def body(appointment, intro):
    from .services import manage_url  # services импортирует этот модуль
    lang = appointment.lang or 'ru'
    company = appointment.company
    lines = [tr(intro, lang), '', company.name]
    if appointment.service:
        lines.append(tr('Услуга: {name}', lang, name=appointment.service.name))
    if appointment.master:
        lines.append(tr('Мастер: {name}', lang, name=appointment.master.name))
    lines.append(when_text(appointment, lang))
    if company.address:
        lines.append(company.address)
    url = manage_url(appointment)
    if url:
        lines += ['', tr('Перенести или отменить: {url}', lang, url=url)]
    if company.phone:
        lines += ['', tr('Телефон салона: {phone}', lang, phone=company.phone)]
    return '\n'.join(lines)


def send(appointment, subject, text):
    try:
        send_mail(subject, text, None, [appointment.client_email])
        return True
    except Exception:  # noqa: BLE001 — сбой SMTP уходит в лог и Sentry, попробуем в следующий раз
        logger.exception('Не удалось отправить письмо клиенту (запись %s)', appointment.pk)
        return False


def reminder_minutes(appointment):
    return DEFAULT_REMINDER_MINUTES if appointment.reminder_minutes is None else appointment.reminder_minutes


def has_telegram(appointment):
    link = getattr(appointment, 'telegram', None)
    return bool(link and link.chat_id)


def process(now=None):
    """Подтверждения и напоминания по email; возвращает число отправленных писем."""
    if not enabled():
        return 0
    now = now or timezone.now()
    sent = 0
    base = Appointment.objects.exclude(client_email='').filter(status__in=ACTIVE_STATUSES).select_related(
        'company', 'master', 'service', 'telegram')

    for appointment in base.filter(email_confirm_sent=False, created_by_staff=False,
                                   created_at__gte=now - CONFIRM_MAX_AGE)[:BATCH]:
        # Флаг — до отправки: двойного письма не будет даже при падении после send
        if not Appointment.objects.filter(pk=appointment.pk, email_confirm_sent=False).update(email_confirm_sent=True):
            continue
        if starts_at(appointment) <= now:
            continue
        lang = appointment.lang or 'ru'
        subject = tr('Вы записаны: {salon}', lang, salon=appointment.company.name)
        sent += send(appointment, subject, body(appointment, 'Вы записаны. Ждём вас!'))

    today = timezone.localdate(now)
    upcoming = base.filter(email_reminder_sent=False, date__gte=today, date__lte=today + timedelta(days=8))
    for appointment in upcoming[:BATCH * 4]:
        minutes = reminder_minutes(appointment)
        start = starts_at(appointment)
        lead = timedelta(minutes=minutes)
        if minutes == 0 or has_telegram(appointment) or start <= now or start - now > lead:
            continue
        if not Appointment.objects.filter(pk=appointment.pk, email_reminder_sent=False).update(email_reminder_sent=True):
            continue
        if start - appointment.created_at < lead + REMINDER_MIN_GAP:
            continue  # записался совсем недавно — подтверждение только что пришло
        lang = appointment.lang or 'ru'
        subject = tr('Напоминание о записи: {salon}', lang, salon=appointment.company.name)
        sent += send(appointment, subject, body(appointment, 'Напоминаем о вашей записи.'))
    return sent
