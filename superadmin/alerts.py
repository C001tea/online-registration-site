"""Тревоги и события платформы в Telegram супер-админа.

Тревоги (сломалось что-то в работе) уходят сразу через Bot API, мимо очереди — чтобы дойти, даже если
встала сама очередь или воркер. Проверка — не чаще раза в минуту на всю платформу: из воркера и из
публичной /api/health/ (её раз в 15 минут дёргает Uptime — так замечается и остановка воркера).
Пока проблема не ушла — напоминание раз в ALERT_REPEAT; ушла — «✅ восстановлено».

События (новый салон, подтверждён email) идут через обычную очередь сообщений.
"""
import logging
from datetime import timedelta
from html import escape

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from notifications import services, telegram
from notifications.models import StaffLinkToken

from .auth import log
from .models import AlertState, HealthMark, SuperAdminLog, SuperAdminTelegram
from .views import SuperView

logger = logging.getLogger(__name__)

RUN_EVERY = timedelta(minutes=1)
ALERT_REPEAT = timedelta(hours=6)
LINK_TTL = timedelta(minutes=15)
LOCK_KEY = 'alerts_run'


def chats():
    return list(SuperAdminTelegram.objects.filter(user__is_active=True, user__is_superuser=True)
                .values_list('chat_id', flat=True))


def send_now(text, chat_ids=None):
    """Отправить сразу (без очереди). → сколько дошло."""
    sent = 0
    for chat_id in chats() if chat_ids is None else chat_ids:
        try:
            telegram.send_message(chat_id, text)
            sent += 1
        except telegram.TelegramError as exc:
            logger.warning('alert send failed: %s', exc)
    return sent


def claim(now):
    """Одна проверка в минуту на всю платформу (воркер и веб не дублируют друг друга)."""
    if HealthMark.objects.filter(key=LOCK_KEY, at__lt=now - RUN_EVERY).update(at=now):
        return True
    _, created = HealthMark.objects.get_or_create(key=LOCK_KEY, defaults={'at': now})
    return created


def duration(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f'{minutes} мин'
    hours = minutes // 60
    return f'{hours} ч' if hours < 48 else f'{hours // 24} дн.'


def run(now=None):
    """Проверить состояние и написать о новых, длящихся и ушедших проблемах."""
    from .health import collect

    if not telegram.enabled():
        return
    now = now or timezone.now()
    targets = chats()
    if not targets or not claim(now):
        return
    _, problems = collect(now, full=False)
    current = {key: text for key, text in problems if not key.endswith('_missing')}
    states = {s.key: s for s in AlertState.objects.all()}

    for key, text in current.items():
        state = states.get(key)
        if state is None:
            AlertState.objects.create(key=key, text=text, since=now, notified_at=now)
            send_now(f'🔴 <b>TimePoint: проблема</b>\n{escape(text)}\n\nПодробности: супер-админка → Техническое состояние', targets)
        elif now - state.notified_at >= ALERT_REPEAT:
            state.notified_at = now
            state.save(update_fields=['notified_at'])
            send_now(f'🔴 Всё ещё не исправлено ({duration(now - state.since)}):\n{escape(state.text)}', targets)
    for key, state in states.items():
        if key not in current:
            state.delete()
            send_now(f'✅ Исправилось: {escape(state.text)}\nДлилось {duration(now - state.since)}', targets)


# --- События ---

def event(text):
    for chat_id in chats():
        services.enqueue(chat_id, text)


def salon_signed_up(company, user):
    event(f'🆕 Новый салон: <b>{escape(company.name)}</b>\n'
          f'Адрес: {escape(company.slug)} · {escape(user.email)}\nЖдёт подтверждения email')


def salon_verified(company):
    event(f'✅ Салон подтвердил email и открыт для клиентов: <b>{escape(company.name)}</b>')


# --- Подключение Telegram ---

def link_chat(chat_id, token):
    """/start x_<токен> в боте: привязать чат к супер-админу."""
    link = (StaffLinkToken.objects.select_related('user')
            .filter(token=token, created_at__gte=timezone.now() - LINK_TTL).first())
    user = link.user if link else None
    if not user or not (user.is_active and user.is_superuser):
        services.reply(chat_id, 'Ссылка устарела. Откройте супер-админку → Безопасность и подключите Telegram заново.')
        return
    SuperAdminTelegram.objects.update_or_create(user=user, defaults={'chat_id': chat_id})
    StaffLinkToken.objects.filter(user=user).delete()
    SuperAdminLog.objects.create(user=user, action='telegram_linked')
    services.reply(chat_id, '🔔 Готово: сюда будут приходить тревоги TimePoint (сбои воркера, бэкапов, очереди) '
                            'и события — новые салоны.')


class TelegramView(SuperView):
    """GET — статус, POST — ссылка для подключения, DELETE — отключить."""

    def get(self, request):
        row = SuperAdminTelegram.objects.filter(user=request.user).first()
        return Response({'available': telegram.enabled(), 'connected': bool(row),
                         'linked_at': row.linked_at.isoformat() if row else None,
                         'open_alerts': [{'text': s.text, 'since': s.since.isoformat()}
                                         for s in AlertState.objects.order_by('since')]})

    def post(self, request):
        username = telegram.bot_username() if telegram.enabled() else None
        if not username:
            return Response({'error': 'Telegram-бот пока не настроен'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        StaffLinkToken.objects.filter(user=request.user).delete()
        token = StaffLinkToken.objects.create(user=request.user)
        return Response({'url': f'https://t.me/{username}?start=x_{token.token}'})

    def delete(self, request):
        if SuperAdminTelegram.objects.filter(user=request.user).delete()[0]:
            log(request, 'telegram_unlinked')
        return Response(status=status.HTTP_204_NO_CONTENT)


class TelegramTestView(SuperView):
    """Пробное сообщение в свой чат."""

    def post(self, request):
        row = SuperAdminTelegram.objects.filter(user=request.user).first()
        if not row:
            return Response({'error': 'Telegram не подключён'}, status=status.HTTP_400_BAD_REQUEST)
        if not send_now('🔔 Проверка: тревоги TimePoint приходят сюда.', [row.chat_id]):
            return Response({'error': 'Telegram не принял сообщение. Нажмите /start в боте и попробуйте ещё раз'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'sent': True})
