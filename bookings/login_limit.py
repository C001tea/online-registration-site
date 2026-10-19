"""Ограничение перебора паролей при входе (клиенты и персонал салона).

Счётчик неудачных попыток хранится в django cache. По умолчанию это LocMem —
отдельный счётчик в каждом воркере, сбрасывается при рестарте (см. docs/STAFF.md).
"""
from django.core.cache import cache

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_BLOCKED_MESSAGE = 'Слишком много попыток входа. Попробуйте через 15 минут'


def is_login_blocked(key):
    """True, если за окно уже было LOGIN_MAX_FAILURES неудачных попыток."""
    return (cache.get(key) or 0) >= LOGIN_MAX_FAILURES


def register_login_failure(key):
    # Окно отсчитывается от первой неудачной попытки: incr срок жизни ключа не продлевает
    if cache.add(key, 1, LOGIN_WINDOW_SECONDS):
        return
    try:
        cache.incr(key)
    except ValueError:
        # Ключ успел истечь между add и incr
        cache.set(key, 1, LOGIN_WINDOW_SECONDS)


def reset_login_failures(key):
    cache.delete(key)
