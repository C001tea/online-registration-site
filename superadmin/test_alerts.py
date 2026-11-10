from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from notifications import services, telegram
from notifications.models import OutgoingMessage, StaffLinkToken

from . import alerts
from .auth import issue_token
from .models import AlertState, HealthMark, SuperAdminLog, SuperAdminTelegram, SuperAdminTOTP, mark


def start(chat_id, payload):
    services.handle_update({'message': {'chat': {'id': chat_id, 'type': 'private'}, 'text': f'/start {payload}'}})


@mock.patch.object(telegram, 'bot_username', return_value='tp_bot')
@mock.patch.object(telegram, 'enabled', return_value=True)
@mock.patch.object(telegram, 'send_message')
class AlertsTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser('boss', 'boss@example.com', 'Super-secret-2026!')
        SuperAdminTOTP.objects.create(user=self.boss, secret='A' * 32, confirmed_at=timezone.now())
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {issue_token(self.boss)}')
        # Всё в порядке: воркер жив, бэкап свежий
        mark('worker')
        mark('backup')

    def link(self, chat_id=777):
        url = self.api.post('/api/super/telegram/').data['url']
        self.assertTrue(url.startswith('https://t.me/tp_bot?start=x_'))
        start(chat_id, url.split('start=')[1])

    def texts(self, send):
        return [c.args[1] for c in send.call_args_list]

    def break_worker(self, minutes=10):
        HealthMark.objects.filter(key='worker').update(at=timezone.now() - timedelta(minutes=minutes))

    def test_link_and_status(self, send, *_):
        self.assertFalse(self.api.get('/api/super/telegram/').data['connected'])
        self.link()
        self.assertEqual(SuperAdminTelegram.objects.get(user=self.boss).chat_id, 777)
        self.assertIn('Готово', self.texts(send)[-1])
        self.assertTrue(self.api.get('/api/super/telegram/').data['connected'])
        self.assertTrue(SuperAdminLog.objects.filter(action='telegram_linked').exists())
        self.assertEqual(self.api.delete('/api/super/telegram/').status_code, 204)
        self.assertFalse(SuperAdminTelegram.objects.exists())

    def test_link_rejected_for_old_or_foreign_token(self, send, *_):
        token = StaffLinkToken.objects.create(user=self.boss)
        StaffLinkToken.objects.filter(pk=token.pk).update(created_at=timezone.now() - timedelta(minutes=20))
        start(1, f'x_{token.token}')
        staff = User.objects.create_user('staff', 'staff@example.com', 'x')
        start(2, f'x_{StaffLinkToken.objects.create(user=staff).token}')
        self.assertFalse(SuperAdminTelegram.objects.exists())
        self.assertIn('устарела', self.texts(send)[-1])

    def test_alert_repeat_and_recovery(self, send, *_):
        self.link()
        send.reset_mock()
        now = timezone.now()
        self.break_worker()
        alerts.run(now)
        self.assertEqual(len(self.texts(send)), 1)
        self.assertIn('Воркер сообщений', self.texts(send)[0])
        self.assertEqual(self.api.get('/api/super/telegram/').data['open_alerts'][0]['text'][:6], 'Воркер')
        alerts.run(now + timedelta(seconds=30))   # раньше минуты — проверка не идёт
        alerts.run(now + timedelta(minutes=5))    # проблема та же — молчим
        self.assertEqual(send.call_count, 1)
        alerts.run(now + timedelta(hours=6, minutes=1))  # напоминание
        self.assertIn('Всё ещё', self.texts(send)[-1])
        mark('worker')
        HealthMark.objects.filter(key=alerts.LOCK_KEY).delete()  # следующая проверка — сейчас
        alerts.run()
        self.assertIn('Исправилось', self.texts(send)[-1])
        self.assertFalse(AlertState.objects.exists())

    def test_no_alert_without_marks_or_chats(self, send, *_):
        self.break_worker()
        alerts.run()                      # никто не подключил Telegram
        self.assertEqual(send.call_count, 0)
        self.link()
        send.reset_mock()
        HealthMark.objects.all().delete()  # сразу после установки отметок нет — не тревога
        alerts.run(timezone.now() + timedelta(minutes=2))
        self.assertEqual(send.call_count, 0)

    def test_failed_backup_alerts(self, send, *_):
        self.link()
        send.reset_mock()
        mark('backup', ok=False)
        alerts.run(timezone.now() + timedelta(minutes=2))
        self.assertIn('бэкап базы не удался', self.texts(send)[0])

    def test_public_health_runs_check(self, send, *_):
        self.link()
        send.reset_mock()
        self.break_worker()
        self.assertEqual(APIClient().get('/api/health/').status_code, 200)
        self.assertIn('Воркер', self.texts(send)[0])

    def test_events_go_to_queue(self, send, *_):
        self.link()
        from bookings.models import Company
        salon = Company.objects.create(name='<Альфа>', slug='alfa')
        alerts.salon_signed_up(salon, User(email='a@example.com'))
        msg = OutgoingMessage.objects.get()
        self.assertEqual(msg.chat_id, 777)
        self.assertIn('&lt;Альфа&gt;', msg.text)

    def test_test_message(self, send, *_):
        self.assertEqual(self.api.post('/api/super/telegram/test/').status_code, 400)
        self.link()
        self.assertEqual(self.api.post('/api/super/telegram/test/').status_code, 200)
        send.side_effect = telegram.TelegramError('Forbidden')
        self.assertEqual(self.api.post('/api/super/telegram/test/').status_code, 400)

    def test_requires_super(self, *_):
        self.assertEqual(APIClient().get('/api/super/telegram/').status_code, 401)
