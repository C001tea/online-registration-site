import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from notifications import email, services, telegram
from superadmin.models import mark as health_mark

logger = logging.getLogger(__name__)

SEND_INTERVAL = 2
REMINDERS_INTERVAL = 60


class Command(BaseCommand):
    help = 'Отправляет очередь сообщений Telegram и напоминания о записях (работает постоянно)'

    def handle(self, *args, **options):
        if not telegram.enabled():
            self.stdout.write('TELEGRAM_BOT_TOKEN не задан — ждём (сообщения не ставятся в очередь)')
        last_reminders = 0.0
        while True:
            close_old_connections()
            try:
                if telegram.enabled():
                    services.send_queued()
                if time.monotonic() - last_reminders >= REMINDERS_INTERVAL:
                    last_reminders = time.monotonic()
                    if telegram.enabled():
                        services.queue_reminders()
                        services.cleanup()
                    # Письма клиентам без Telegram: подтверждение и напоминание
                    email.process()
                    # Отметка для супер-админки: воркер жив (раз в минуту)
                    health_mark('worker', telegram=telegram.enabled(), email=email.enabled())
            except Exception:
                logger.exception('telegram worker iteration failed')
            time.sleep(SEND_INTERVAL)
