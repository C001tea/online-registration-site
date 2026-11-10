import io
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from notifications.models import OutgoingMessage

from .auth import issue_token
from .models import HealthMark, SuperAdminTOTP, mark


class HealthTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        boss = User.objects.create_superuser('boss', 'boss@example.com', 'Super-secret-2026!')
        SuperAdminTOTP.objects.create(user=boss, secret='A' * 32, confirmed_at=timezone.now())
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {issue_token(boss)}')

    def test_empty_state_reports_problems(self):
        data = self.api.get('/api/super/health/').data
        self.assertEqual(data['checks']['worker']['state'], 'missing')
        self.assertEqual(data['checks']['backup']['state'], 'missing')
        self.assertIn('Нет свежего бэкапа базы', data['problems'])
        self.assertEqual(data['server']['pending_migrations'], 0)

    def test_marks_and_queue(self):
        mark('worker', telegram=True)
        call_command('health_mark', 'backup', '--detail', 'size=123', stdout=io.StringIO())
        call_command('health_mark', 'restore_check', '--fail', stdout=io.StringIO())
        old = OutgoingMessage.objects.create(chat_id=1, text='секрет клиента', attempts=2, error='Forbidden: bot was blocked')
        OutgoingMessage.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        OutgoingMessage.objects.create(chat_id=2, text='x', sent_at=timezone.now())
        data = self.api.get('/api/super/health/').data
        self.assertEqual(data['checks']['worker']['state'], 'ok')
        self.assertEqual(data['checks']['backup']['details'], {'size': '123'})
        self.assertEqual(data['checks']['restore_check']['state'], 'failed')
        self.assertEqual((data['queue']['pending'], data['queue']['failing'], data['queue']['sent_24h']), (1, 1, 1))
        self.assertTrue(data['queue']['stuck'])
        self.assertEqual(data['queue']['errors'][0]['error'], 'Forbidden: bot was blocked')
        self.assertNotIn('секрет клиента', str(data))  # тексты сообщений не показываем

    def test_stale_worker(self):
        mark('worker')
        HealthMark.objects.filter(key='worker').update(at=timezone.now() - timedelta(minutes=10))
        data = self.api.get('/api/super/health/').data
        self.assertEqual(data['checks']['worker']['state'], 'stale')
        self.assertTrue(any('Воркер' in p for p in data['problems']))

    def test_requires_super(self):
        self.assertEqual(APIClient().get('/api/super/health/').status_code, 401)
