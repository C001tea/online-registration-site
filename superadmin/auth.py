"""Токены и права супер-админки.

Токен супер-админа — отдельный тип ('super'), без refresh: живёт 4 часа, потом вход заново
с кодом из приложения. Обычная аутентификация API (клиенты, панель салона) принимает только
token_type='access', поэтому токен супер-админа нигде, кроме /api/super/, не действует.
И наоборот: токены салона и клиентов в /api/super/ не принимаются.
"""
from datetime import timedelta

from django.core import signing
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import Token

from bookings.security import client_ip
from dashboard.tokens import PASSWORD_CLAIM, password_fingerprint, token_matches_password

from .models import SuperAdminLog

SESSION_EXPIRED = 'Сессия супер-админки устарела, войдите снова'
NO_ACCESS = 'Нет доступа'
TICKET_SALT = 'superadmin-login-ticket'
TICKET_MAX_AGE = 5 * 60


class SuperAccessToken(Token):
    token_type = 'super'
    lifetime = timedelta(hours=4)


def issue_token(user):
    token = SuperAccessToken.for_user(user)
    token[PASSWORD_CLAIM] = password_fingerprint(user)
    return str(token)


def make_ticket(user, stage):
    """Пропуск между шагами входа: пароль уже проверен, ждём код (5 минут)."""
    return signing.dumps({'u': user.pk, 's': stage, 'p': password_fingerprint(user)}, salt=TICKET_SALT)


def read_ticket(ticket):
    """-> (user_id, stage, pwd) или None, если пропуск подделан или просрочен."""
    try:
        data = signing.loads(ticket or '', salt=TICKET_SALT, max_age=TICKET_MAX_AGE)
    except signing.BadSignature:
        return None
    return data.get('u'), data.get('s'), data.get('p')


class SuperJWTAuthentication(JWTAuthentication):
    """Принимает только токены супер-админа."""

    def get_validated_token(self, raw_token):
        try:
            return SuperAccessToken(raw_token)
        except Exception:
            raise AuthenticationFailed({'error': SESSION_EXPIRED})


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if not token_matches_password(request.auth, user):
            raise AuthenticationFailed({'error': SESSION_EXPIRED})
        if not (user.is_active and user.is_superuser):
            raise PermissionDenied({'error': NO_ACCESS})
        totp = getattr(user, 'superadmin_totp', None)
        if totp is None or totp.confirmed_at is None:
            raise AuthenticationFailed({'error': SESSION_EXPIRED})
        return True


def log(request, action, user=None, company=None, details=None):
    return SuperAdminLog.objects.create(
        user=user if user is not None else (request.user if request.user.is_authenticated else None),
        action=action, company=company, details=details or {}, ip=client_ip(request))
