"""Сотрудникам в Telegram: напоминание мастеру за час до клиента и вечерняя сводка на завтра."""
from datetime import datetime, time, timedelta

from django.contrib.auth.models import User
from django.utils import timezone

from bookings.models import AdminProfile, Appointment, Master
from dashboard.tokens import get_staff_tokens
from . import services
from .models import OutgoingMessage, StaffTelegram
from .tests import OWNER_CHAT, TelegramTestCase

MASTER_CHAT = 8001


class StaffTelegramCase(TelegramTestCase):
    def setUp(self):
        super().setUp()
        self.master_user = User.objects.create_user('anna', password='Str0ng-pass-123')
        self.master.user = self.master_user
        self.master.save()
        self.link = StaffTelegram.objects.create(user=self.master_user, chat_id=MASTER_CHAT)

    def visit(self, start, booked_before=timedelta(days=2), master=None, status='confirmed', name='Ирина'):
        start = timezone.localtime(start)
        appointment = Appointment.objects.create(
            company=self.company, master=master or self.master, service=self.service, client_name=name,
            client_phone='+37369123456', date=start.date(), start_time=start.time().replace(second=0, microsecond=0),
            end_time=time(23, 59), status=status)
        Appointment.objects.filter(pk=appointment.pk).update(created_at=start - booked_before)
        return appointment

    def buttons(self, chat_id):
        return OutgoingMessage.objects.filter(chat_id=chat_id).latest('id').buttons


class StaffReminderTests(StaffTelegramCase):
    def test_master_gets_reminder_once(self):
        self.visit(timezone.now() + timedelta(minutes=50))
        self.assertEqual(services.queue_staff_reminders(), 1)
        self.assertEqual(services.queue_staff_reminders(), 0)
        text = self.queued(MASTER_CHAT)[0]
        self.assertIn('Через час клиент', text)
        self.assertIn('+37369123456', text)
        self.assertEqual(self.queued(OWNER_CHAT), [])  # владельцу без профиля мастера — нет

    def test_skips_far_late_cancelled_and_other_masters(self):
        self.visit(timezone.now() + timedelta(hours=3))  # ещё рано
        self.visit(timezone.now() + timedelta(minutes=40), booked_before=timedelta(minutes=30))  # только записались
        self.visit(timezone.now() + timedelta(minutes=45), status='cancelled')
        other = Master.objects.create(company=self.company, name='Мария')
        self.visit(timezone.now() + timedelta(minutes=30), master=other)  # у мастера нет Telegram
        self.assertEqual(services.queue_staff_reminders(), 0)

    def test_rescheduled_visit_reminds_again(self):
        appointment = self.visit(timezone.now() + timedelta(minutes=50))
        services.queue_staff_reminders()
        later = timezone.localtime(timezone.now() + timedelta(minutes=55))
        Appointment.objects.filter(pk=appointment.pk).update(start_time=later.time().replace(second=0, microsecond=0),
                                                             date=later.date())
        self.assertEqual(services.queue_staff_reminders(), 1)

    def test_turned_off_in_bot(self):
        self.visit(timezone.now() + timedelta(minutes=50))
        services.queue_staff_reminders()
        self.assertEqual(self.buttons(MASTER_CHAT), [[['🔕 Не напоминать за час', 'soff:rem']]])
        self.press('soff:rem', chat_id=MASTER_CHAT)
        self.assertIn('отключены', self.fake.last('answerCallbackQuery')['text'])
        self.link.refresh_from_db()
        self.assertFalse(self.link.remind_before)
        self.visit(timezone.now() + timedelta(minutes=30), name='Ольга')
        self.assertEqual(services.queue_staff_reminders(), 0)


class DailySummaryTests(StaffTelegramCase):
    def evening(self):
        today = timezone.localdate()
        return timezone.make_aware(datetime.combine(today, time(20, 5)))

    def tomorrow_at(self, hour):
        return timezone.make_aware(datetime.combine(timezone.localdate() + timedelta(days=1), time(hour)))

    def test_master_and_owner_summaries(self):
        self.visit(self.tomorrow_at(11), name='Ольга')
        self.visit(self.tomorrow_at(10), name='Ирина')
        self.visit(self.tomorrow_at(12), status='cancelled', name='Отмена')
        self.assertEqual(services.queue_daily_summaries(self.evening()), 2)
        master_text = self.queued(MASTER_CHAT)[0]
        self.assertIn('2 записи', master_text)
        self.assertLess(master_text.index('Ирина'), master_text.index('Ольга'))
        self.assertNotIn('Отмена', master_text)
        owner_text = self.queued(OWNER_CHAT)[0]
        self.assertIn('Анна', owner_text)  # владельцу — весь салон с мастерами
        # Второй раз за вечер — не шлём
        self.assertEqual(services.queue_daily_summaries(self.evening() + timedelta(hours=1)), 0)

    def test_not_before_evening_and_not_when_empty(self):
        self.visit(self.tomorrow_at(10))
        morning = self.evening() - timedelta(hours=10)
        self.assertEqual(services.queue_daily_summaries(morning), 0)
        Appointment.objects.all().delete()
        self.assertEqual(services.queue_daily_summaries(self.evening()), 0)

    def test_turn_off_in_bot(self):
        self.visit(self.tomorrow_at(10))
        services.queue_daily_summaries(self.evening())
        self.assertEqual(self.buttons(MASTER_CHAT), [[['🔕 Не присылать сводку', 'soff:sum']]])
        self.press('soff:sum', chat_id=MASTER_CHAT)
        self.link.refresh_from_db()
        self.assertFalse(self.link.daily_summary)
        self.assertTrue(self.link.remind_before)

    def test_admin_who_is_master_gets_own_list(self):
        admin = User.objects.create_user('olga', password='Str0ng-pass-123')
        AdminProfile.objects.create(user=admin, company=self.company, role='admin')
        other = Master.objects.create(company=self.company, name='Ольга', user=admin)
        StaffTelegram.objects.create(user=admin, chat_id=9100)
        self.visit(self.tomorrow_at(10), master=other, name='Своя')
        self.visit(self.tomorrow_at(11), name='Чужая')
        services.queue_daily_summaries(self.evening())
        text = self.queued(9100)[0]
        self.assertIn('Своя', text)
        self.assertNotIn('Чужая', text)


class StaffTelegramSettingsTests(StaffTelegramCase):
    def auth(self, user):
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + get_staff_tokens(user)['access'])

    def test_get_and_patch(self):
        self.auth(self.master_user)
        data = self.api.get('/api/staff/telegram/').data
        self.assertEqual((data['connected'], data['remind_before'], data['daily_summary']), (True, True, True))
        response = self.api.patch('/api/staff/telegram/', {'daily_summary': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['daily_summary'])
        self.assertTrue(response.data['remind_before'])
        self.assertEqual(self.api.patch('/api/staff/telegram/', {'remind_before': 'no'}, format='json').status_code, 400)

    def test_patch_without_telegram(self):
        self.link.delete()
        self.auth(self.master_user)
        self.assertEqual(self.api.patch('/api/staff/telegram/', {'daily_summary': False}, format='json').status_code, 400)
