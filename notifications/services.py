"""Уведомления о записях: очередь сообщений, обработка команд бота, напоминания."""
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from bookings.i18n import tr
from bookings.models import AdminProfile, Appointment
from bookings.rules import cancel_rule_text, client_can_cancel
from bookings.slots import ACTIVE_STATUSES
from dashboard.permissions import MANAGER_ROLES, get_staff
from finance import penalties
from finance.models import Penalty
from . import login, telegram, texts
from .models import AppointmentTelegram, ClientTelegram, OutgoingMessage, StaffLinkToken, StaffTelegram

logger = logging.getLogger(__name__)

STAFF_LINK_TTL = timedelta(minutes=15)
# Напоминание за сутки не шлём, если записались меньше чем за REMINDER_24H_MIN_LEAD до визита
# (подтверждение пришло только что), за час — если меньше чем за REMINDER_1H_MIN_LEAD
REMINDER_24H_MIN_LEAD = timedelta(hours=25)
REMINDER_1H_MIN_LEAD = timedelta(hours=2)


def starts_at(appointment):
    return timezone.make_aware(datetime.combine(appointment.date, appointment.start_time))


def enqueue(chat_id, text, buttons=None):
    if telegram.enabled() and chat_id:
        OutgoingMessage.objects.create(chat_id=chat_id, text=text, buttons=buttons or [])


# --- Кнопки под сообщением клиенту (callback: действие:id привязки) ---

def client_buttons(link):
    """«Отменить» — только пока правило салона разрешает отмену клиентом."""
    appointment = link.appointment
    lang = texts.lang_of(appointment)
    cancel = client_can_cancel(appointment)
    if appointment.status == 'pending':
        row = [(tr('Подтверждаю ✅', lang), f'ok:{link.id}')]
        return [row + [(tr('Отменить', lang), f'cx:{link.id}')]] if cancel else [row]
    if appointment.status == 'confirmed' and cancel:
        return [[(tr('Отменить запись', lang), f'cx:{link.id}')]]
    return []


def client_url(appointment):
    """Ссылка «Подтвердить в Telegram» для экрана после записи (None, если бот не настроен)."""
    link = getattr(appointment, 'telegram', None)
    username = telegram.bot_username() if link and telegram.enabled() else None
    return f'https://t.me/{username}?start=a_{link.token}' if username else None


# --- Салону ---

def staff_chats(appointment):
    """Чаты владельца и админов салона и мастера этой записи."""
    user_ids = set(AdminProfile.objects.filter(
        company=appointment.company, role__in=MANAGER_ROLES
    ).values_list('user_id', flat=True))
    master = appointment.master
    if master and master.user_id and master.is_active:
        user_ids.add(master.user_id)
    return sorted(set(StaffTelegram.objects.filter(user_id__in=user_ids).values_list('chat_id', flat=True)))


def notify_staff(appointment, text):
    for chat_id in staff_chats(appointment):
        enqueue(chat_id, text)


# --- События записи ---

def appointment_created(appointment, user=None):
    """Онлайн-запись клиента: ссылка на бота, сообщение клиенту (если Telegram уже привязан), салону."""
    link = AppointmentTelegram.objects.create(appointment=appointment)
    if user is not None and user.is_authenticated:
        client_chat = ClientTelegram.objects.filter(user=user).first()
        if client_chat:
            link.chat_id = client_chat.chat_id
            link.save(update_fields=['chat_id'])
            enqueue(link.chat_id, card_text(appointment), client_buttons(link))
    notify_staff(appointment, texts.staff_new(appointment, blacklist_of(appointment)))


def blacklist_of(appointment):
    """Штрафы клиента в этом салоне (чёрный список: не оплаченные и оплаченные), кроме этой записи."""
    return list(Penalty.objects.filter(
        company=appointment.company, phone=appointment.client_phone, status__in=penalties.ACTIVE,
    ).exclude(appointment=appointment).select_related('company', 'appointment__service', 'appointment__company')
        .order_by('appointment__date', 'appointment__start_time'))


def card_text(appointment):
    """Карточка записи клиенту; если есть долг — блок: за что штраф и что оплатить при визите."""
    text = texts.client_card(appointment)
    unpaid = [p for p in blacklist_of(appointment) if p.status == 'unpaid']
    if unpaid and appointment.status in ACTIVE_STATUSES:
        text += texts.debt_note(appointment, unpaid)
    return text


def penalty_created(penalty):
    """Штраф начислен: клиенту — если его Telegram привязан к записи или аккаунту; салону — если
    штраф начислен автоматически (клиент сам отменил поздно)."""
    appointment = penalty.appointment
    link = AppointmentTelegram.objects.filter(appointment=appointment, chat_id__isnull=False).first()
    chat_id = link.chat_id if link else None
    if chat_id is None and appointment.client_id:
        client_chat = ClientTelegram.objects.filter(user_id=appointment.client_id).first()
        chat_id = client_chat.chat_id if client_chat else None
    if chat_id:
        enqueue(chat_id, texts.penalty_client(penalty))
    if penalty.created_by_id is None:
        notify_staff(appointment, texts.penalty_staff(penalty))


def appointment_cancelled_by_client(appointment):
    notify_staff(appointment, texts.staff_cancelled(appointment))


def appointment_changed_by_staff(before, appointment):
    """Салон отменил или перенёс запись → сообщение клиенту, если его Telegram привязан."""
    link = AppointmentTelegram.objects.filter(appointment=appointment, chat_id__isnull=False).first()
    if not link:
        return
    if appointment.status == 'cancelled' and before['status'] != 'cancelled':
        enqueue(link.chat_id, texts.salon_cancelled(appointment))
    elif appointment.status in ACTIVE_STATUSES and (
            before['date'] != appointment.date or before['start_time'] != appointment.start_time
            or before['master_id'] != appointment.master_id):
        # Новое время — напоминания нужны заново
        AppointmentTelegram.objects.filter(pk=link.pk).update(reminder_24h_sent=False, reminder_1h_sent=False)
        enqueue(link.chat_id, texts.salon_moved(appointment), client_buttons(link))


def snapshot(appointment):
    return {'status': appointment.status, 'date': appointment.date,
            'start_time': appointment.start_time, 'master_id': appointment.master_id}


# --- Подключение Telegram сотрудником ---

def staff_link_url(user):
    """Одноразовая ссылка t.me для сотрудника (None, если имя бота сейчас не узнать)."""
    username = telegram.bot_username()
    if not username:
        return None
    StaffLinkToken.objects.filter(user=user).delete()
    token = StaffLinkToken.objects.create(user=user)
    return f'https://t.me/{username}?start=s_{token.token}'


# --- Входящие обновления (вебхук) ---

def reply(chat_id, text, buttons=None):
    try:
        telegram.send_message(chat_id, text, buttons)
    except telegram.TelegramError as exc:
        logger.warning('telegram reply failed: %s', exc)


def handle_update(update):
    if 'callback_query' in update:
        handle_callback(update['callback_query'])
        return
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    # Только личные чаты: в группах ссылки и кнопки записи не обрабатываем
    if chat.get('type') != 'private':
        return
    text = (message.get('text') or '').strip()
    chat_id = chat['id']
    if 'contact' in message:
        login.reply_to(chat_id, *login.bot_contact(message))
        return
    if text.startswith('/start'):
        parts = text.split(maxsplit=1)
        payload = parts[1] if len(parts) > 1 else ''
        if payload.startswith('l_'):
            login.reply_to(chat_id, *login.bot_start(chat_id, payload[2:]))
            return
        if payload.startswith('a_'):
            link_appointment(chat_id, payload[2:])
            return
        if payload.startswith('s_'):
            link_staff(chat_id, payload[2:])
            return
    reply(chat_id, texts.GREETING)


def link_appointment(chat_id, token):
    with transaction.atomic():
        link = (AppointmentTelegram.objects.select_for_update(of=('self',))
                .select_related('appointment__company', 'appointment__master', 'appointment__service')
                .filter(token=token).first())
        if not link:
            reply(chat_id, texts.LINK_NOT_FOUND)
            return
        if link.chat_id and link.chat_id != chat_id:
            reply(chat_id, tr(texts.LINK_USED, texts.lang_of(link.appointment)))
            return
        link.chat_id = chat_id
        link.save(update_fields=['chat_id'])
        client = link.appointment.client
        if client:
            ClientTelegram.objects.update_or_create(user=client, defaults={'chat_id': chat_id})
    reply(chat_id, card_text(link.appointment), client_buttons(link))


def link_staff(chat_id, token):
    fresh_after = timezone.now() - STAFF_LINK_TTL
    link = StaffLinkToken.objects.select_related('user').filter(token=token, created_at__gte=fresh_after).first()
    staff = get_staff(link.user) if link else None
    if not staff:
        reply(chat_id, texts.STAFF_LINK_EXPIRED)
        return
    StaffTelegram.objects.update_or_create(user=link.user, defaults={'chat_id': chat_id})
    StaffLinkToken.objects.filter(user=link.user).delete()
    reply(chat_id, texts.staff_linked(staff.company.name))


def handle_callback(callback):
    callback_id = callback.get('id')
    message = callback.get('message') or {}
    chat_id = (message.get('chat') or {}).get('id')
    action, _, link_id = (callback.get('data') or '').partition(':')
    answer = ''
    try:
        answer = apply_action(action, link_id, chat_id, message.get('message_id'))
    finally:
        try:
            telegram.answer_callback(callback_id, answer)
        except telegram.TelegramError as exc:
            logger.warning('answerCallbackQuery failed: %s', exc)


def apply_action(action, link_id, chat_id, message_id):
    """ok — подтвердить, cx — спросить про отмену, cy — отменить, back — назад. Возвращает всплывающий текст."""
    if not link_id.isdecimal() or not chat_id:
        return ''
    with transaction.atomic():
        link = (AppointmentTelegram.objects.select_for_update(of=('self',))
                .select_related('appointment__company', 'appointment__master', 'appointment__service')
                .filter(pk=int(link_id), chat_id=chat_id).first())
        if not link:
            return texts.LINK_NOT_FOUND
        appointment = Appointment.objects.select_for_update().get(pk=link.appointment_id)
        link.appointment.status = appointment.status
        lang = texts.lang_of(appointment)
        editable = appointment.status in ACTIVE_STATUSES and starts_at(appointment) > timezone.now()
        popup = ''
        if action in ('ok', 'cx', 'cy') and not editable:
            popup = tr(texts.TOO_LATE, lang)
        elif action in ('cx', 'cy') and not client_can_cancel(appointment):
            popup = cancel_rule_text(link.appointment.company, lang)
            editable = False
        elif action == 'ok' and appointment.status == 'pending':
            appointment.status = 'confirmed'
            appointment.save(update_fields=['status'])
            link.appointment.status = 'confirmed'
            popup = tr('Спасибо, ждём вас!', lang)
        elif action == 'cy':
            late = penalties.is_late_cancel(link.appointment)
            appointment.status = 'cancelled'
            appointment.save(update_fields=['status'])
            link.appointment.status = 'cancelled'
            appointment_cancelled_by_client(link.appointment)
            popup = tr('Запись отменена', lang)
            if late:  # позже срока салона — чёрный список
                penalty = penalties.create(link.appointment, 'late_cancel')
                if penalty:
                    transaction.on_commit(lambda: penalty_created(penalty))
    text = texts.client_card(link.appointment)
    buttons = client_buttons(link)
    if action == 'cx' and editable:
        fee = penalties.late_cancel_amount(link.appointment)
        if fee:
            text += texts.late_cancel_warning(link.appointment, fee)
        text += tr(texts.CONFIRM_CANCEL, lang)
        buttons = [[(tr('Да, отменить', lang), f'cy:{link.id}'), (tr('Нет', lang), f'back:{link.id}')]]
    try:
        telegram.edit_message(chat_id, message_id, text, buttons)
    except telegram.TelegramError as exc:
        # «message is not modified» и подобное — не ошибка для клиента
        logger.info('editMessageText: %s', exc)
    return popup


# --- Фоновый процесс: отправка очереди и напоминания ---

def send_queued(limit=20):
    now = timezone.now()
    sent = 0
    with transaction.atomic():
        batch = list(OutgoingMessage.objects.select_for_update(skip_locked=True).filter(
            sent_at__isnull=True, attempts__lt=settings.TELEGRAM_MAX_ATTEMPTS,
        ).exclude(next_attempt_at__gt=now).order_by('id')[:limit])
        for message in batch:
            try:
                telegram.send_message(message.chat_id, message.text, message.buttons)
            except telegram.TelegramError as exc:
                message.attempts = settings.TELEGRAM_MAX_ATTEMPTS if exc.permanent else message.attempts + 1
                delay = exc.retry_after or 5 * 2 ** message.attempts
                message.next_attempt_at = now + timedelta(seconds=delay)
                message.error = str(exc)[:500]
            except Exception as exc:  # сеть недоступна и т.п.
                message.attempts += 1
                message.next_attempt_at = now + timedelta(seconds=5 * 2 ** message.attempts)
                message.error = f'{type(exc).__name__}: {exc}'[:500]
            else:
                message.sent_at = timezone.now()
                message.error = ''
                sent += 1
            message.save(update_fields=['attempts', 'next_attempt_at', 'error', 'sent_at'])
    return sent


def mark_once(link, field):
    """Ставит флаг напоминания; True — если поставили мы (защита от двойной отправки)."""
    return AppointmentTelegram.objects.filter(pk=link.pk, **{field: False}).update(**{field: True}) == 1


def queue_reminders(now=None):
    now = now or timezone.now()
    today = timezone.localdate(now)
    links = AppointmentTelegram.objects.filter(
        chat_id__isnull=False,
        appointment__status__in=ACTIVE_STATUSES,
        appointment__date__gte=today, appointment__date__lte=today + timedelta(days=2),
    ).select_related('appointment__company', 'appointment__master', 'appointment__service')
    queued = 0
    for link in links:
        appointment = link.appointment
        start = starts_at(appointment)
        left = start - now
        lead = start - appointment.created_at
        if left <= timedelta(0):
            continue
        with transaction.atomic():
            if left <= timedelta(hours=1):
                if not link.reminder_1h_sent and lead > REMINDER_1H_MIN_LEAD and mark_once(link, 'reminder_1h_sent'):
                    enqueue(link.chat_id, texts.reminder_1h(appointment))
                    queued += 1
            elif left <= timedelta(hours=24):
                if not link.reminder_24h_sent and lead > REMINDER_24H_MIN_LEAD and mark_once(link, 'reminder_24h_sent'):
                    enqueue(link.chat_id, texts.reminder_24h(appointment), client_buttons(link))
                    queued += 1
    return queued


def cleanup(now=None):
    """Отправленные сообщения храним 30 дней (в них имена и телефоны), ссылки сотрудников — до истечения."""
    now = now or timezone.now()
    OutgoingMessage.objects.filter(sent_at__lt=now - timedelta(days=30)).delete()
    OutgoingMessage.objects.filter(sent_at__isnull=True, created_at__lt=now - timedelta(days=7)).delete()
    StaffLinkToken.objects.filter(created_at__lt=now - STAFF_LINK_TTL).delete()
    login.cleanup(now)
