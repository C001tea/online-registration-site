import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from notifications import services, telegram

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
                        services.queue_reminders()
                        services.cleanup()
                        last_reminders = time.monotonic()
            except Exception:
                logger.exception('telegram worker iteration failed')
            time.sleep(SEND_INTERVAL)
