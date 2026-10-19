"""JWT персонала салона.

Отличаются от клиентских claim'ом scope='staff' и короткими сроками жизни.
Клиентский токен (без scope) в /api/staff/* не принимается (см. permissions.IsStaff),
а клиентские эндпоинты staff-токены принимают как обычные.

Claim pwd — отпечаток хеша пароля: после смены или сброса пароля старые токены
(и refresh, и access) перестают действовать (проверка в IsStaff и при refresh).
"""
from datetime import timedelta

from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

STAFF_SCOPE = 'staff'
PASSWORD_CLAIM = 'pwd'


def password_fingerprint(user):
    """Короткий HMAC от хеша пароля (сам хеш в токен не попадает)."""
    return salted_hmac('staff-token', user.password).hexdigest()[:20]


def token_matches_password(token, user):
    """Токен выдан при текущем пароле пользователя."""
    claim = token.get(PASSWORD_CLAIM)
    return isinstance(claim, str) and constant_time_compare(claim, password_fingerprint(user))


class StaffAccessToken(AccessToken):
    lifetime = timedelta(hours=12)


class StaffRefreshToken(RefreshToken):
    lifetime = timedelta(days=7)
    access_token_class = StaffAccessToken

    @classmethod
    def for_user(cls, user):
        token = super().for_user(user)
        # simplejwt копирует claim'ы refresh-токена в access, scope окажется и там
        token['scope'] = STAFF_SCOPE
        token[PASSWORD_CLAIM] = password_fingerprint(user)
        return token


def get_staff_tokens(user):
    refresh = StaffRefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }
