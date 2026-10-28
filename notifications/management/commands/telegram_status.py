import re

from django.core.management.base import BaseCommand
from django.utils.timezone import localtime

from bookings.models import Appointment
from notifications.models import AppointmentTelegram, OutgoingMessage, StaffTelegram


def kind(text):
    """Первая строка сообщения без разметки (тип события; имён и телефонов в ней нет)."""
    return re.sub(r'<[^>]+>', '', text.splitlines()[0])[:40]


class Command(BaseCommand):
    help = 'Состояние Telegram-уведомлений без персональных данных (для логов Actions)'

    def handle(self, *args, **options):
        self.stdout.write(f'Сотрудников с Telegram: {StaffTelegram.objects.count()}')
        self.stdout.write('\nПоследние записи: id | статус | создана | панелью | Telegram клиента | напомин. 24ч/1ч')
        for a in Appointment.objects.order_by('-id')[:10]:
            link = AppointmentTelegram.objects.filter(appointment=a).first()
            linked = 'нет ссылки' if not link else ('привязан' if link.chat_id else 'не привязан')
            flags = f'{link.reminder_24h_sent}/{link.reminder_1h_sent}' if link else '-'
            self.stdout.write(f'{a.id} | {a.status} | {localtime(a.created_at):%d.%m %H:%M} | {a.created_by_staff} | {linked} | {flags}')
        self.stdout.write('\nПоследние сообщения: id | создано | отправлено | попыток | ошибка | тип')
        for m in OutgoingMessage.objects.order_by('-id')[:15]:
            sent = f'{localtime(m.sent_at):%H:%M:%S}' if m.sent_at else '—'
            self.stdout.write(f'{m.id} | {localtime(m.created_at):%d.%m %H:%M:%S} | {sent} | {m.attempts} | {m.error[:60] or "—"} | {kind(m.text)}')
