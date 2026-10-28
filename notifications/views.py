import hmac
import json
import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from dashboard.permissions import IsStaff
from . import services, telegram
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
