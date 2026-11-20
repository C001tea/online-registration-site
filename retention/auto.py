"""Автоматические напоминания «пора записаться»: бот в Telegram или письмо на email.

Выключены по умолчанию: включает владелец (Company.rhythm_auto, работает только вместе с rhythm_enabled).
Чтобы включение не превратилось в рассылку всем сразу и клиентам не надоедало:
- первое напоминание — только когда срок совсем рядом (FIRST_FROM…FIRST_UNTIL дней до срока);
  тем, у кого срок давно прошёл, бот не пишет — их зовут вручную из раздела «Пора записаться»;
- второе — не раньше чем через SECOND_AFTER дней, если клиент так и не записался, и только если
  первое тоже отправил бот (после личного сообщения сотрудника бот не вмешивается);
- не больше MAX_STEPS напоминаний за цикл, «потерянным» — никогда;
- одному клиенту — не чаще раза в CLIENT_GAP дней (даже по разным услугам);
- только днём (SEND_FROM_HOUR…SEND_UNTIL_HOUR по времени салона), не больше DAILY_LIMIT в день на салон;
- в каждом сообщении — «Не напоминать» (Telegram) или ссылка «Отписаться» (email).
"""
import html
import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.utils import timezone

from bookings.i18n import tr
from bookings.models import Appointment, Company
from notifications import telegram
from notifications.models import OutgoingMessage
from .models import ClientOptOut, RhythmMessage
from .rhythm import client_rhythms, invite_text, salon_url

logger = logging.getLogger(__name__)

SEND_FROM_HOUR = 10
SEND_UNTIL_HOUR = 20
FIRST_FROM = -3
FIRST_UNTIL = 1
SECOND_AFTER = 7
MAX_STEPS = 2
CLIENT_GAP = 7
DAILY_LIMIT = 30
AUTO_CHANNELS = ('telegram', 'email')
OPT_OUT_SALT = 'rhythm-opt-out'


def email_enabled():
    return bool(getattr(settings, 'EMAIL_CONFIGURED', False))


def next_step(item, now):
    """Какое по счёту напоминание пора отправить клиенту (0 — никакое)."""
    if item.opted_out or item.stage not in ('soon', 'leaving'):
        return 0
    messages = item.messages
    if not messages:
        days_left = (item.due - timezone.localdate(now)).days
        return 1 if FIRST_FROM <= days_left <= FIRST_UNTIL else 0
    if len(messages) >= MAX_STEPS or any(m.channel not in AUTO_CHANNELS for m in messages):
        return 0
    if now - messages[-1].sent_at < timedelta(days=SECOND_AFTER):
        return 0
    return len(messages) + 1


def channel_for(item):
    if item.telegram_chat and telegram.enabled():
        return 'telegram'
    if item.email and email_enabled():
        return 'email'
    return None


def opt_out_token(company, phone, lang):
    return signing.dumps({'c': company.id, 'p': phone, 'l': lang}, salt=OPT_OUT_SALT, compress=True)


def read_opt_out_token(token):
    try:
        data = signing.loads(token, salt=OPT_OUT_SALT)
    except signing.BadSignature:
        return None
    return data if isinstance(data, dict) and {'c', 'p'} <= data.keys() else None


def opt_out_url(company, phone, lang):
    # Сайт салона проксирует /api на бэкенд — ссылка с адресом салона, клиенту знакомым
    return f'{salon_url(company)}api/rhythm/off/{opt_out_token(company, phone, lang)}/'


def send_telegram(company, item, message, today):
    lang = item.lang if item.lang in ('ru', 'ro') else 'ru'
    text = html.escape(invite_text(item, company, today))
    text += '\n\n' + tr('Не хотите таких напоминаний — нажмите «Не напоминать».', lang)
    url = salon_url(company)
    buttons = []
    if url.startswith('https://'):  # Telegram принимает в кнопках только настоящие адреса
        buttons.append([(tr('Выбрать время', lang), url)])
    buttons.append([(tr('Не напоминать', lang), f'roff:{message.id}')])
    OutgoingMessage.objects.create(chat_id=item.telegram_chat, text=text, buttons=buttons)
    return True


def send_email(company, item, message, today):
    lang = item.lang if item.lang in ('ru', 'ro') else 'ru'
    off = opt_out_url(company, item.phone, lang)
    body = invite_text(item, company, today) + '\n\n' + tr(
        'Не хотите получать такие письма? Отписаться: {url}', lang, url=off)
    email = EmailMessage(
        tr('Пора записаться: {salon}', lang, salon=company.name), body, None, [item.email],
        headers={'List-Unsubscribe': f'<{off}>', 'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'})
    try:
        email.send()
        return True
    except Exception:  # noqa: BLE001 — сбой SMTP: в лог, попробуем в следующий час
        logger.exception('Не удалось отправить напоминание «пора записаться» (салон %s)', company.pk)
        return False


def process_company(company, now=None):
    """Отправляет салону напоминания, которым пора; возвращает число отправленных."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    day_start = timezone.make_aware(datetime.combine(today, time.min))
    sent_today = RhythmMessage.objects.filter(company=company, channel__in=AUTO_CHANNELS,
                                              sent_at__gte=day_start).count()
    if sent_today >= DAILY_LIMIT:
        return 0
    recent = set(RhythmMessage.objects.filter(company=company, sent_at__gte=now - timedelta(days=CLIENT_GAP))
                 .values_list('phone', flat=True))
    sent = 0
    for item in sorted(client_rhythms(company, today=today), key=lambda i: i.due):
        if sent_today + sent >= DAILY_LIMIT:
            break
        if item.phone in recent:
            continue
        step = next_step(item, now)
        channel = channel_for(item) if step else None
        if not channel:
            continue
        message = RhythmMessage.objects.create(
            company=company, phone=item.phone, group=item.group, cycle=item.last_visit, step=step,
            channel=channel, sent_at=now, chat_id=item.telegram_chat if channel == 'telegram' else None)
        delivered = (send_telegram if channel == 'telegram' else send_email)(company, item, message, today)
        if not delivered:
            message.delete()
            continue
        recent.add(item.phone)
        sent += 1
    return sent


def run(now=None):
    """Раз в час днём (вызывает фоновый процесс каждую минуту)."""
    now = now or timezone.now()
    local = timezone.localtime(now)
    if not SEND_FROM_HOUR <= local.hour < SEND_UNTIL_HOUR:
        return 0
    if not cache.add(f'rhythm-auto:{local:%Y%m%d%H}', 1, 2 * 60 * 60):
        return 0
    sent = 0
    for company in Company.objects.filter(status=Company.STATUS_ACTIVE, rhythm_enabled=True, rhythm_auto=True):
        try:
            sent += process_company(company, now)
        except Exception:  # noqa: BLE001 — один салон не должен останавливать остальные
            logger.exception('Напоминания «пора записаться»: ошибка в салоне %s', company.pk)
    return sent


def opt_out(company_id, phone):
    ClientOptOut.objects.get_or_create(company_id=company_id, phone=phone, kind='rhythm')


def telegram_opt_out(chat_id, message_id, callback_message_id):
    """Кнопка «Не напоминать» под сообщением бота. Возвращает всплывающий текст."""
    if not message_id.isdecimal() or not chat_id:
        return ''
    message = RhythmMessage.objects.filter(pk=int(message_id), chat_id=chat_id).select_related('company').first()
    if not message:
        return ''
    opt_out(message.company_id, message.phone)
    last = (Appointment.objects.filter(company_id=message.company_id, client_phone=message.phone)
            .order_by('-date').values_list('lang', flat=True).first())
    lang = last if last in ('ru', 'ro') else 'ru'
    url = salon_url(message.company)
    buttons = [[{'text': tr('Выбрать время', lang), 'url': url}]] if url.startswith('https://') else []
    try:
        telegram.call('editMessageReplyMarkup', chat_id=chat_id, message_id=callback_message_id,
                      reply_markup={'inline_keyboard': buttons})
    except telegram.TelegramError as exc:
        logger.info('editMessageReplyMarkup: %s', exc)
    return tr('Хорошо, больше не будем напоминать', lang)
