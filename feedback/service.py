"""Отзывы после визита: запрос оценки, ответ клиента, тревога владельцу о низкой оценке.

Выключено по умолчанию (Company.reviews_enabled). Правила:
- спрашиваем через ASK_AFTER после конца визита со статусом «состоялся», только с SEND_FROM_HOUR до
  SEND_UNTIL_HOUR (позже — на следующий день, но не позже ASK_WITHIN_DAYS);
- один запрос на визит, одному клиенту — не чаще раза в CLIENT_GAP_DAYS (кто ходит каждую неделю, не устанет);
- Telegram — 5 кнопок; без Telegram — письмо со ссылками-звёздами (страница /api/review/<token>/);
- ссылку на Google показываем всем, кто оценил (Google запрещает звать только довольных), но при низкой оценке
  сначала просим рассказать, что пошло не так, и сразу пишем владельцу и администраторам.
"""
import html
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from bookings.i18n import tr
from bookings.models import AdminProfile, Appointment, Company
from dashboard.permissions import MANAGER_ROLES
from notifications import telegram
from notifications.models import ClientTelegram, OutgoingMessage, StaffTelegram
from retention.models import ClientOptOut
from retention.rhythm import first_name, salon_url

logger = logging.getLogger(__name__)

ASK_AFTER = timedelta(hours=2)
ASK_WITHIN_DAYS = 2
SEND_FROM_HOUR = 10
SEND_UNTIL_HOUR = 20
CLIENT_GAP_DAYS = 30
COMMENT_WINDOW = timedelta(hours=24)
CHANGE_WINDOW = timedelta(days=7)
MAX_COMMENT = 1000
CHECK_EVERY = 10 * 60


def email_enabled():
    return bool(getattr(settings, 'EMAIL_CONFIGURED', False))


def lang_of(appointment):
    return appointment.lang if appointment.lang in ('ru', 'ro') else 'ru'


def instagram_url(company):
    value = (company.review_instagram or '').strip()
    if not value:
        return ''
    if value.startswith('https://'):
        return value
    return f'https://instagram.com/{value.lstrip("@")}'


def review_page_url(review, rating=None):
    url = f'{salon_url(review.company)}api/review/{review.token}/'
    return f'{url}?r={rating}' if rating else url


def ask_text(appointment):
    lang = lang_of(appointment)
    name = first_name(appointment.client_name)
    hello = tr('Здравствуйте, {name}!', lang, name=name) if name else tr('Здравствуйте!', lang)
    if appointment.master:
        question = tr('Как вам визит к мастеру {master}?', lang, master=appointment.master.name)
    else:
        question = tr('Как вам визит?', lang)
    return f'{hello} {question} {tr("Оцените, пожалуйста, — это займёт секунду.", lang)}\n{appointment.company.name}'


def rating_buttons(review):
    return [[(f'{n} ⭐', f'rv{n}:{review.id}') for n in range(1, 6)]]


def chat_of(appointment):
    link = getattr(appointment, 'telegram', None)
    if link and link.chat_id:
        return link.chat_id
    if appointment.client_id:
        return ClientTelegram.objects.filter(user_id=appointment.client_id).values_list('chat_id', flat=True).first()
    return None


def due_appointments(now):
    """Визиты «состоялся», закончившиеся не меньше ASK_AFTER назад, без запроса оценки."""
    today = timezone.localdate(now)
    rows = (Appointment.objects.filter(
        company__reviews_enabled=True, company__status=Company.STATUS_ACTIVE, status='completed',
        date__gte=today - timedelta(days=ASK_WITHIN_DAYS), date__lte=today, visit_review__isnull=True,
    ).exclude(client_phone='').select_related('company', 'master', 'service', 'telegram').order_by('date', 'end_time'))
    for appointment in rows:
        end = timezone.make_aware(datetime.combine(appointment.date, appointment.end_time))
        if now - end >= ASK_AFTER:
            yield appointment


def send_requests(now=None):
    """Отправляет запросы оценки; возвращает число отправленных."""
    from .models import VisitReview
    now = now or timezone.now()
    local = timezone.localtime(now)
    if not SEND_FROM_HOUR <= local.hour < SEND_UNTIL_HOUR:
        return 0
    sent = 0
    for appointment in due_appointments(now):
        company = appointment.company
        phone = appointment.client_phone
        if ClientOptOut.objects.filter(company=company, phone=phone, kind='all').exists():
            continue
        if VisitReview.objects.filter(company=company, phone=phone,
                                      sent_at__gte=now - timedelta(days=CLIENT_GAP_DAYS)).exists():
            continue
        chat = chat_of(appointment) if telegram.enabled() else None
        channel = 'telegram' if chat else ('email' if appointment.client_email and email_enabled() else None)
        if not channel:
            continue
        review = VisitReview.objects.create(
            appointment=appointment, company=company, master=appointment.master, phone=phone,
            channel=channel, chat_id=chat, sent_at=now)
        if channel == 'telegram':
            OutgoingMessage.objects.create(chat_id=chat, text=html.escape(ask_text(appointment)),
                                           buttons=rating_buttons(review))
        elif not send_email(review):
            review.delete()
            continue
        sent += 1
    return sent


def send_email(review):
    appointment = review.appointment
    lang = lang_of(appointment)
    stars = '\n'.join(f'{"★" * n}{"☆" * (5 - n)}  {review_page_url(review, n)}' for n in range(5, 0, -1))
    body = f'{ask_text(appointment)}\n\n{stars}'
    try:
        send_mail(tr('Как вам визит? {salon}', lang, salon=appointment.company.name), body, None,
                  [appointment.client_email])
        return True
    except Exception:  # noqa: BLE001 — сбой SMTP: попробуем в следующий раз
        logger.exception('Не удалось отправить запрос оценки (запись %s)', appointment.pk)
        return False


def run(now=None):
    """Вызывает фоновый процесс каждую минуту; проверяем раз в CHECK_EVERY секунд."""
    if not cache.add('feedback-requests', 1, CHECK_EVERY):
        return 0
    return send_requests(now)


# --- Ответ клиента ---

def save_rating(review, rating, comment=None, publish=None, now=None):
    """Сохраняет оценку (можно поменять в течение CHANGE_WINDOW). True — если оценка новая или стала низкой."""
    now = now or timezone.now()
    was_low = review.is_low
    first = review.rating is None
    review.rating = rating
    review.rated_at = now
    fields = ['rating', 'rated_at']
    if comment is not None:
        review.comment = comment[:MAX_COMMENT]
        fields.append('comment')
    if publish is not None:
        review.publish = publish
        fields.append('publish')
    review.save(update_fields=fields)
    if review.is_low and (first or not was_low or comment):
        alert_low(review)
    return first


def thanks_text(review):
    lang = lang_of(review.appointment)
    if review.is_low:
        return tr('Нам очень жаль, что визит вас огорчил. Что пошло не так? Напишите ответным сообщением — '
                  'владелец салона прочитает и свяжется с вами.', lang)
    return tr('Спасибо за оценку! Если не сложно, напишите пару слов ответным сообщением. '
              'Можно показать ваш отзыв на сайте салона?', lang)


def after_rating_buttons(review):
    lang = lang_of(review.appointment)
    company = review.company
    rows = []
    if not review.is_low:
        rows.append([(tr('Да, показать', lang), f'rvp:{review.id}'), (tr('Нет', lang), f'rvn:{review.id}')])
    links = []
    if company.review_google_url.startswith('https://'):
        links.append((tr('Отзыв в Google', lang), company.review_google_url))
    insta = instagram_url(company)
    if insta and not review.is_low:
        links.append(('Instagram', insta))
    if links:
        rows.append(links)
    return rows


def stars(rating):
    return '⭐' * rating


def telegram_action(action, review_id, chat_id, message_id):
    """Кнопки под запросом оценки: rv1…rv5 — оценка, rvp / rvn — показывать ли отзыв на сайте."""
    from .models import VisitReview
    if not review_id.isdecimal() or not chat_id:
        return ''
    now = timezone.now()
    with transaction.atomic():
        review = (VisitReview.objects.select_for_update(of=('self',))
                  .select_related('appointment__company', 'appointment__master', 'appointment__service', 'company')
                  .filter(pk=int(review_id), chat_id=chat_id).first())
        if not review:
            return ''
        lang = lang_of(review.appointment)
        if action in ('rvp', 'rvn'):
            if review.rating is None:
                return ''
            review.publish = action == 'rvp'
            review.save(update_fields=['publish'])
            text = f'{stars(review.rating)}\n' + (
                tr('Спасибо! Отзыв появится на сайте салона.', lang) if review.publish
                else tr('Хорошо, отзыв видит только салон.', lang))
            buttons = [row for row in after_rating_buttons(review) if not row[0][1].startswith('rv')]
            popup = tr('Готово', lang)
        else:
            rating = int(action[2:])
            if review.rated_at and now - review.rated_at > CHANGE_WINDOW:
                return tr('Оценку уже не изменить', lang)
            save_rating(review, rating, now=now)
            review.awaiting_comment = True
            review.save(update_fields=['awaiting_comment'])
            text = f'{stars(rating)}\n{thanks_text(review)}'
            buttons = after_rating_buttons(review)
            popup = tr('Спасибо!', lang)
    try:
        telegram.edit_message(chat_id, message_id, html.escape(text), buttons)
    except telegram.TelegramError as exc:
        logger.info('editMessageText: %s', exc)
    return popup


def take_comment(chat_id, text):
    """Текст клиента после оценки — комментарий к отзыву. True — если приняли."""
    from .models import VisitReview
    if not text or text.startswith('/'):
        return False
    review = (VisitReview.objects.select_related('appointment__company', 'appointment__master',
                                                 'appointment__service', 'company')
              .filter(chat_id=chat_id, awaiting_comment=True, rated_at__gte=timezone.now() - COMMENT_WINDOW)
              .order_by('-rated_at').first())
    if not review:
        return False
    review.comment = text[:MAX_COMMENT]
    review.awaiting_comment = False
    review.save(update_fields=['comment', 'awaiting_comment'])
    if review.is_low:
        alert_low(review)
    lang = lang_of(review.appointment)
    reply = (tr('Спасибо, что рассказали. Владелец салона уже знает и свяжется с вами.', lang) if review.is_low
             else tr('Спасибо за отзыв!', lang))
    OutgoingMessage.objects.create(chat_id=chat_id, text=html.escape(reply), buttons=[])
    return True


# --- Тревога владельцу ---

def manager_chats(company):
    user_ids = AdminProfile.objects.filter(company=company, role__in=MANAGER_ROLES).values_list('user_id', flat=True)
    return sorted(set(StaffTelegram.objects.filter(user_id__in=user_ids).values_list('chat_id', flat=True)))


def alert_text(review):
    appointment = review.appointment
    parts = [f'⚠️ Низкая оценка: {stars(review.rating)} ({review.rating} из 5)',
             f'{appointment.client_name} {appointment.client_phone}'.strip()]
    visit = ', '.join(filter(None, [appointment.service.name if appointment.service else '',
                                    appointment.master.name if appointment.master else '',
                                    f'{appointment.date:%d.%m} {appointment.start_time:%H:%M}']))
    parts.append(visit)
    if review.comment:
        parts.append(f'Комментарий: {review.comment}')
    else:
        parts.append('Клиента попросили рассказать, что пошло не так. Лучше позвонить сегодня.')
    return html.escape('\n'.join(parts))


def alert_low(review):
    text = alert_text(review)
    for chat_id in manager_chats(review.company):
        OutgoingMessage.objects.create(chat_id=chat_id, text=text, buttons=[])
