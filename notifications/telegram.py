"""Минимальный клиент Telegram Bot API (requests, без сторонних библиотек)."""
import hashlib

import requests
from django.conf import settings
from django.core.cache import cache

API_URL = 'https://api.telegram.org/bot{token}/{method}'
TIMEOUT = 10


class TelegramError(Exception):
    def __init__(self, description, retry_after=None, permanent=False):
        super().__init__(description)
        self.retry_after = retry_after
        # Пользователь заблокировал бота / чат не найден — повторять бесполезно
        self.permanent = permanent


def enabled():
    return bool(settings.TELEGRAM_BOT_TOKEN)


def bot_username():
    """Username бота для ссылок t.me: из настроек или getMe (кешируется на час)."""
    if settings.TELEGRAM_BOT_USERNAME:
        return settings.TELEGRAM_BOT_USERNAME
    username = cache.get('telegram_bot_username')
    if username is None:
        try:
            username = call('getMe')['username']
        except (TelegramError, requests.RequestException, KeyError, TypeError):
            return None
        cache.set('telegram_bot_username', username, 60 * 60)
    return username


def webhook_secret():
    """Секрет вебхука (заголовок X-Telegram-Bot-Api-Secret-Token), выводится из SECRET_KEY."""
    return hashlib.sha256(f'telegram-webhook:{settings.SECRET_KEY}'.encode()).hexdigest()[:48]


def call(method, **params):
    response = requests.post(
        API_URL.format(token=settings.TELEGRAM_BOT_TOKEN, method=method), json=params, timeout=TIMEOUT
    )
    try:
        data = response.json()
    except ValueError:
        raise TelegramError(f'HTTP {response.status_code}')
    if not data.get('ok'):
        description = data.get('description', f'HTTP {response.status_code}')
        retry_after = (data.get('parameters') or {}).get('retry_after')
        permanent = response.status_code in (400, 403) and retry_after is None
        raise TelegramError(description, retry_after=retry_after, permanent=permanent)
    return data.get('result')


def keyboard(buttons):
    """[[('Текст', 'callback'), ...], ...] → inline_keyboard."""
    return {'inline_keyboard': [[{'text': text, 'callback_data': data} for text, data in row] for row in buttons]}


def send_message(chat_id, text, buttons=None):
    params = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    if buttons:
        params['reply_markup'] = keyboard(buttons)
    return call('sendMessage', **params)


def edit_message(chat_id, message_id, text, buttons=None):
    params = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML',
              'disable_web_page_preview': True, 'reply_markup': keyboard(buttons or [])}
    return call('editMessageText', **params)


def answer_callback(callback_id, text=''):
    return call('answerCallbackQuery', callback_query_id=callback_id, text=text)
