"""JWT персонала салона.

Отличаются от клиентских claim'ом scope='staff' и короткими сроками жизни.
Клиентский токен (без scope) в /api/staff/* не принимается (см. permissions.IsStaff),
а клиентские эндпоинты staff-токены принимают как обычные.
"""
from datetime import timedelta

from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

STAFF_SCOPE = 'staff'


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
        return token


def get_staff_tokens(user):
    refresh = StaffRefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }
