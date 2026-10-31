"""Защита от перебора и спама: IP клиента за прокси и простые лимиты «не больше N за окно».

Счётчики — в общем кеше (база данных, см. CACHES): один на все процессы gunicorn и бота,
не сбрасываются при деплое.
"""
from functools import wraps

from django.core.cache import cache
from django.http import HttpResponse

from .login_limit import (
    LOGIN_BLOCKED_MESSAGE, increment_counter, is_login_blocked, login_limit_key, register_login_failure,
    reset_login_failures,
)

TOO_MANY_MESSAGE = 'Слишком много запросов. Попробуйте позже'


def client_ip(request):
    """IP посетителя. Сайт ходит в API через прокси Vercel, он передаёт адрес клиента в
    X-Real-IP; Caddy заменяет X-Forwarded-For адресом того, кто к нему подключился
    (сам Vercel или клиент напрямую). Заголовок можно подделать при обращении к API напрямую,
    поэтому IP — только один из ключей лимитов, рядом с телефоном и общим лимитом салона."""
    meta = request.META
    for header in ('HTTP_X_REAL_IP', 'HTTP_X_FORWARDED_FOR', 'REMOTE_ADDR'):
        value = (meta.get(header) or '').split(',')[0].strip()
        if value:
            return value[:64]
    return 'unknown'


def hit(key, limit, window_seconds):
    """+1 к счётчику; True, если лимит превышен (этот запрос уже лишний)."""
    return increment_counter(f'rl:{key}', window_seconds) > limit


def peek(key, limit):
    """Лимит уже исчерпан (без увеличения счётчика)."""
    return (cache.get(f'rl:{key}') or 0) >= limit


def limit_admin_login(view):
    """Вход в админку Django: 5 неудачных попыток за 15 минут на логин (как в панели)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method != 'POST':
            return view(request, *args, **kwargs)
        key = login_limit_key(request.POST.get('username', ''))
        if is_login_blocked(key):
            return HttpResponse(LOGIN_BLOCKED_MESSAGE, status=429, content_type='text/plain; charset=utf-8')
        response = view(request, *args, **kwargs)
        if response.status_code == 302:  # вошёл — перенаправление в админку
            reset_login_failures(key)
        else:
            register_login_failure(key)
        return response
    return wrapper
