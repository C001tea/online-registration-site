"""Вход клиента на сайт через Telegram, без пароля.

1. Сайт: POST /api/auth/telegram/start/ → одноразовая ссылка t.me/<бот>?start=l_<токен>.
2. Бот: просит поделиться номером (кнопка request_contact). Принимается только собственный
   контакт пользователя — не пересланный чужой.
3. Бот присылает 6-значный код; клиент вводит его на сайте: POST /api/auth/telegram/verify/.
   Код нужен, чтобы перехваченная ссылка сама по себе не давала входа.
Новому номеру создаётся аккаунт клиента (имя — из Telegram).
"""
import secrets
from datetime import timedelta
from html import escape

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from bookings.i18n import tr
from bookings.models import Appointment, ClientProfile
from bookings.phone import normalize_phone
from . import telegram
from .models import ClientTelegram, TelegramLogin

LOGIN_TTL = timedelta(minutes=10)
MAX_CODE_ATTEMPTS = 5



def share_keyboard(lang):
    return {
        'keyboard': [[{'text': tr('📱 Поделиться номером', lang), 'request_contact': True}]],
        'resize_keyboard': True,
        'one_time_keyboard': True,
    }

REMOVE_KEYBOARD = {'remove_keyboard': True}

ASK_CONTACT = 'Вход на сайт записи.\n\nНажмите кнопку «📱 Поделиться номером» внизу — так мы убедимся, что номер ваш.'
LOGIN_EXPIRED = 'Ссылка для входа устарела. Нажмите «Войти через Telegram» на сайте ещё раз.'
NOT_OWN_CONTACT = 'Отправьте, пожалуйста, свой номер кнопкой «📱 Поделиться номером».'
BAD_PHONE = 'Не получилось распознать номер. Попробуйте войти на сайте по номеру и паролю.'
PHONE_TAKEN = 'Этот номер занят служебным аккаунтом. Войдите на сайте по номеру и паролю.'


class LoginError(Exception):
    pass


def fresh(queryset):
    return queryset.filter(used=False, created_at__gte=timezone.now() - LOGIN_TTL)


def start(lang='ru'):
    return TelegramLogin.objects.create(lang=lang)


def bot_start(chat_id, token):
    """/start l_<токен>: привязываем вход к чату и просим номер."""
    with transaction.atomic():
        login = fresh(TelegramLogin.objects.select_for_update()).filter(token=token).first()
        if not login or (login.chat_id and login.chat_id != chat_id):
            expired = TelegramLogin.objects.filter(token=token).only('lang').first()
            return tr(LOGIN_EXPIRED, expired.lang if expired else 'ru'), REMOVE_KEYBOARD
        login.chat_id = chat_id
        login.save(update_fields=['chat_id'])
    return tr(ASK_CONTACT, login.lang), share_keyboard(login.lang)


def client_for_phone(phone, first_name, last_name):
    """Аккаунт клиента по номеру; новому номеру — создаём (без пароля)."""
    profile = ClientProfile.objects.filter(phone=phone).select_related('user').first()
    if profile:
        return profile.user
    if User.objects.filter(username=phone).exists():
        return None  # логин занят аккаунтом без профиля клиента (например, персонал)
    try:
        with transaction.atomic():
            user = User.objects.create_user(username=phone)
            user.set_unusable_password()
            user.save(update_fields=['password'])
            ClientProfile.objects.create(user=user, phone=phone, name=(first_name or 'Клиент')[:200],
                                         surname=(last_name or '')[:200])
    except IntegrityError:
        return ClientProfile.objects.get(phone=phone).user
    return user


def bot_contact(message):
    """Контакт из бота → код для входа. Возвращает (текст ответа, клавиатура)."""
    chat_id = message['chat']['id']
    contact = message.get('contact') or {}
    sender = message.get('from') or {}
    # Язык — из последнего начатого входа в этом чате (его ещё нет — русский)
    pending = fresh(TelegramLogin.objects.filter(chat_id=chat_id)).order_by('-created_at').first()
    lang = pending.lang if pending else 'ru'
    if not contact.get('user_id') or contact.get('user_id') != sender.get('id'):
        return tr(NOT_OWN_CONTACT, lang), share_keyboard(lang)
    digits = ''.join(ch for ch in str(contact.get('phone_number', '')) if ch.isdigit())
    phone = normalize_phone(f'+{digits}')
    if not phone:
        return tr(BAD_PHONE, lang), REMOVE_KEYBOARD
    with transaction.atomic():
        login = (fresh(TelegramLogin.objects.select_for_update())
                 .filter(chat_id=chat_id).order_by('-created_at').first())
        if not login:
            return LOGIN_EXPIRED, REMOVE_KEYBOARD
        user = client_for_phone(phone, contact.get('first_name'), contact.get('last_name'))
        if user is None:
            return tr(PHONE_TAKEN, lang), REMOVE_KEYBOARD
        login.user = user
        login.code = f'{secrets.randbelow(10 ** 6):06d}'
        login.attempts = 0
        login.save(update_fields=['user', 'code', 'attempts'])
        # Подтверждения и напоминания о следующих записях — сразу в этот чат
        ClientTelegram.objects.update_or_create(user=user, defaults={'chat_id': chat_id})
        # Номер подтверждён Telegram — записи без входа на этот номер становятся записями аккаунта
        Appointment.objects.filter(client_phone=phone, client__isnull=True).update(client=user)
    return tr('Код для входа: <b>{code}</b>\n\nВведите его на сайте. Никому не сообщайте этот код.',
              login.lang, code=escape(login.code)), REMOVE_KEYBOARD


def verify(token, code):
    """Код с сайта → пользователь (одноразово). Ошибка — LoginError с текстом для сайта."""
    # Ошибку бросаем после транзакции: иначе откат стёр бы счётчик неверных попыток
    with transaction.atomic():
        login = fresh(TelegramLogin.objects.select_for_update()).filter(token=token or '').first()
        if not login:
            error = 'Ссылка для входа устарела. Начните вход заново'
        elif not login.code:
            error = 'Сначала откройте бота и поделитесь номером'
        elif login.attempts >= MAX_CODE_ATTEMPTS:
            error = 'Слишком много попыток. Начните вход заново'
        elif not secrets.compare_digest(login.code, (code or '').strip()):
            login.attempts += 1
            login.save(update_fields=['attempts'])
            error = 'Неверный код'
        else:
            login.used = True
            login.save(update_fields=['used'])
            return login.user
    raise LoginError(error)


def reply_to(chat_id, text, keyboard):
    try:
        telegram.send_message(chat_id, text, reply_markup=keyboard)
    except telegram.TelegramError:
        pass


def cleanup(now=None):
    TelegramLogin.objects.filter(created_at__lt=(now or timezone.now()) - timedelta(days=1)).delete()
