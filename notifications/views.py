import hmac
import json
import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.auth_views import get_token_for_user
from bookings.i18n import client_lang
from bookings.login_limit import increment_counter
from dashboard.permissions import IsStaff
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


class StaffTelegramView(APIView):
    """Telegram сотрудника: статус (GET), ссылка для подключения (POST), отключение (DELETE)."""
    permission_classes = [IsStaff]

    def get(self, request):
        return Response({
            'available': telegram.enabled(),
            'connected': StaffTelegram.objects.filter(user=request.user).exists(),
        })

    def post(self, request):
        url = services.staff_link_url(request.user) if telegram.enabled() else None
        if not url:
            return Response({'error': 'Telegram-бот пока не настроен'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'url': url})

    def delete(self, request):
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
        ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
        if increment_counter(f'tglogin:{ip}', 60 * 60) > LOGIN_STARTS_PER_HOUR:
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
