import hmac
import json
import logging

from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.auth_views import get_token_for_user
from bookings.i18n import client_lang
from bookings.security import client_ip, hit
from dashboard.permissions import IMPERSONATION_FORBIDDEN_MESSAGE, IsStaff, is_impersonation
from . import login, services, telegram
from .models import StaffTelegram

logger = logging.getLogger(__name__)


class TelegramWebhookView(APIView):
    """Обновления от Telegram. Подлинность — по секрету в заголовке (задаётся в setWebhook)."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not telegram.enabled() or not hmac.compare_digest(secret, telegram.webhook_secret()):
            return Response(status=status.HTTP_403_FORBIDDEN)
        try:
            update = json.loads(request.body)
            services.handle_update(update)
        except Exception:
            # 200, чтобы Telegram не повторял одно и то же обновление бесконечно
            logger.exception('telegram update failed')
        return Response({'ok': True})


def staff_telegram_data(link):
    return {
        'available': telegram.enabled(),
        'connected': link is not None,
        'remind_before': link.remind_before if link else True,
        'daily_summary': link.daily_summary if link else True,
    }


class StaffTelegramView(APIView):
    """Telegram сотрудника: статус (GET), ссылка для подключения (POST), отключение (DELETE)."""
    permission_classes = [IsStaff]

    def get(self, request):
        return Response(staff_telegram_data(StaffTelegram.objects.filter(user=request.user).first()))

    def patch(self, request):
        """{remind_before, daily_summary} — напоминание за час до клиента и вечерняя сводка на завтра."""
        link = StaffTelegram.objects.filter(user=request.user).first()
        if not link:
            return Response({'error': 'Сначала подключите Telegram'}, status=status.HTTP_400_BAD_REQUEST)
        fields = [key for key in ('remind_before', 'daily_summary') if key in request.data]
        for key in fields:
            if not isinstance(request.data[key], bool):
                return Response({'error': f'{key} — true или false'}, status=status.HTTP_400_BAD_REQUEST)
            setattr(link, key, request.data[key])
        if fields:
            link.save(update_fields=fields)
        return Response(staff_telegram_data(link))

    def post(self, request):
        if is_impersonation(request):
            raise PermissionDenied({'error': IMPERSONATION_FORBIDDEN_MESSAGE})
        url = services.staff_link_url(request.user) if telegram.enabled() else None
        if not url:
            return Response({'error': 'Telegram-бот пока не настроен'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'url': url})

    def delete(self, request):
        if is_impersonation(request):
            raise PermissionDenied({'error': IMPERSONATION_FORBIDDEN_MESSAGE})
        StaffTelegram.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# Не больше стольких начатых входов через Telegram в час с одного IP
LOGIN_STARTS_PER_HOUR = 20


class TelegramLoginStartView(APIView):
    """Начать вход клиента через бота: одноразовая ссылка t.me (живёт 10 минут)."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        username = telegram.bot_username() if telegram.enabled() else None
        if not username:
            return Response({'error': 'Вход через Telegram сейчас недоступен'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if hit(f'tglogin:{client_ip(request)}', LOGIN_STARTS_PER_HOUR, 60 * 60):
            return Response({'error': 'Слишком много попыток. Попробуйте через час'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        attempt = login.start(client_lang(request))
        return Response({'token': attempt.token, 'url': f'https://t.me/{username}?start=l_{attempt.token}',
                         'expires_in': int(login.LOGIN_TTL.total_seconds())})


class TelegramLoginVerifyView(APIView):
    """Код из бота → токены входа клиента (как у /api/auth/login/)."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            user = login.verify(request.data.get('token'), request.data.get('code'))
        except login.LoginError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(get_token_for_user(user))
