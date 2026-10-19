"""Ограничение перебора паролей при входе (клиенты и персонал салона).

Счётчик неудачных попыток хранится в django cache. По умолчанию это LocMem —
отдельный счётчик в каждом воркере, сбрасывается при рестарте (см. docs/STAFF.md).
"""
from django.core.cache import cache

from .phone import normalize_phone

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_BLOCKED_MESSAGE = 'Слишком много попыток входа. Попробуйте через 15 минут'


def login_limit_key(login, region=None):
    """Ключ счётчика по аккаунту, в который ведёт логин, а не по тому, как его написали.

    Если логин — номер телефона (069123456, +373 69 123 456, ...), ключ — E.164 и общий
    для входа клиента (/api/auth/login/) и персонала (/api/staff/auth/login/).
    """
    phone = normalize_phone(login, region)
    if phone:
        return f'login:{phone}'
    return f'login:user:{(login or "").strip().lower()}'


def is_login_blocked(key):
    """True, если за окно уже было LOGIN_MAX_FAILURES неудачных попыток."""
    return (cache.get(key) or 0) >= LOGIN_MAX_FAILURES


def increment_counter(key, window_seconds):
    """+1 к счётчику в кеше, возвращает новое значение.

    Окно отсчитывается от первого увеличения: incr срок жизни ключа не продлевает.
    """
    if cache.add(key, 1, window_seconds):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        # Ключ успел истечь между add и incr
        cache.set(key, 1, window_seconds)
        return 1


def register_login_failure(key):
    increment_counter(key, LOGIN_WINDOW_SECONDS)


def reset_login_failures(key):
    cache.delete(key)
