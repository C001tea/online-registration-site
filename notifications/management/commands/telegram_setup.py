from django.conf import settings
from django.core.management.base import BaseCommand

from notifications import telegram


class Command(BaseCommand):
    help = 'Регистрирует вебхук бота на https://API_DOMAIN/api/telegram/webhook/ (при каждом деплое)'

    def handle(self, *args, **options):
        if not telegram.enabled():
            self.stdout.write('TELEGRAM_BOT_TOKEN не задан — пропускаем')
            return
        if not settings.API_DOMAIN:
            self.stdout.write('API_DOMAIN не задан — пропускаем')
            return
        url = f'https://{settings.API_DOMAIN}/api/telegram/webhook/'
        telegram.call('setWebhook', url=url, secret_token=telegram.webhook_secret(),
                      allowed_updates=['message', 'callback_query'])
        me = telegram.call('getMe')
        self.stdout.write(f'Вебхук бота @{me.get("username")} → {url}')
