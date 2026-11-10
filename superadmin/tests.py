import io
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from bookings.models import AdminProfile, Company
from dashboard.tokens import get_staff_tokens

from . import totp
from .models import SuperAdminLog, SuperAdminTOTP

PASSWORD = 'Super-secret-2026!'


class TotpTests(TestCase):
    def test_rfc6238_vector(self):
        # Тестовый вектор RFC 6238 (SHA-1, секрет '12345678901234567890'), последние 6 цифр
        secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        self.assertEqual(totp.code_at(secret, 59 // 30), '287082')
        self.assertEqual(totp.code_at(secret, 1111111109 // 30), '081804')

    def test_window(self):
        secret = totp.new_secret()
        step = totp.current_step(1_000_000)
        self.assertEqual(totp.match_step(secret, totp.code_at(secret, step - 1), now=1_000_000), step - 1)
        self.assertIsNone(totp.match_step(secret, totp.code_at(secret, step - 3), now=1_000_000))
        self.assertIsNone(totp.match_step(secret, '12a', now=1_000_000))


class SuperLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.user = User.objects.create_superuser('boss', 'boss@example.com', PASSWORD)

    def login(self, username='boss', password=PASSWORD):
        return self.api.post('/api/super/auth/login/', {'username': username, 'password': password}, format='json')

    def verify(self, ticket, code):
        return self.api.post('/api/super/auth/verify/', {'ticket': ticket, 'code': code}, format='json')

    def setup_totp(self):
        """Первый вход: QR → код → токен и резервные коды."""
        first = self.login().data
        self.assertEqual(first['step'], 'setup')
        code = totp.code_at(first['secret'], totp.current_step())
        response = self.verify(first['ticket'], code)
        self.assertEqual(response.status_code, 200, response.data)
        return first['secret'], response.data

    def test_first_login_shows_qr_then_issues_token(self):
        secret, data = self.setup_totp()
        self.assertTrue(data['token'])
        self.assertEqual(len(data['backup_codes']), 10)
        self.assertEqual(self.login().data['step'], 'code')  # второй вход уже без QR
        record = SuperAdminTOTP.objects.get(user=self.user)
        self.assertIsNotNone(record.confirmed_at)
        self.assertNotIn(data['backup_codes'][0], record.backup_codes)  # хранятся только хеши
        actions = list(SuperAdminLog.objects.values_list('action', flat=True))
        self.assertIn('totp_setup', actions)
        self.assertIn('login', actions)

    def test_second_login_requires_code_and_rejects_replay(self):
        secret, _ = self.setup_totp()
        second = self.login().data
        self.assertEqual(second['step'], 'code')
        self.assertNotIn('secret', second)
        # Тот же код, что при настройке, второй раз не принимается
        same = totp.code_at(secret, totp.current_step())
        self.assertEqual(self.verify(second['ticket'], same).status_code, 400)
        with mock.patch('superadmin.totp.time.time', return_value=(totp.current_step() + 1) * 30 + 1):
            fresh = totp.code_at(secret, totp.current_step())
            self.assertEqual(self.verify(second['ticket'], fresh).status_code, 200)

    def test_backup_code_works_once(self):
        _, data = self.setup_totp()
        code = data['backup_codes'][0]
        ticket = self.login().data['ticket']
        response = self.verify(ticket, code.upper())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['me']['backup_codes_left'], 9)
        self.assertEqual(self.verify(self.login().data['ticket'], code).status_code, 400)

    def test_backup_code_not_accepted_during_setup(self):
        first = self.login().data
        self.assertEqual(self.verify(first['ticket'], 'abcd-efgh').status_code, 400)

    def test_wrong_password_and_non_superuser(self):
        self.assertEqual(self.login(password='nope').status_code, 400)
        User.objects.create_user('owner', password=PASSWORD)
        self.assertEqual(self.login(username='owner').status_code, 400)

    def test_password_attempts_limited(self):
        for _ in range(5):
            self.login(password='nope')
        self.assertEqual(self.login().status_code, 429)

    def test_code_attempts_limited(self):
        ticket = self.login().data['ticket']
        for _ in range(5):
            self.verify(ticket, '000000')
        self.assertEqual(self.verify(ticket, '000000').status_code, 429)

    def test_forged_or_stale_ticket(self):
        self.assertEqual(self.verify('garbage', '123456').status_code, 400)
        ticket = self.login().data['ticket']
        self.user.set_password('Another-pass-2026!')
        self.user.save()
        self.assertEqual(self.verify(ticket, '123456').status_code, 400)

    def test_token_only_for_super_api(self):
        _, data = self.setup_totp()
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {data["token"]}')
        self.assertEqual(self.api.get('/api/super/me/').data['username'], 'boss')
        self.assertEqual(self.api.get('/api/super/log/').status_code, 200)
        # В панели салона и клиентском API токен супер-админа не действует
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 401)

    def test_staff_token_rejected(self):
        company = Company.objects.create(name='Салон', slug='salon')
        owner = User.objects.create_user('owner', password=PASSWORD)
        AdminProfile.objects.create(user=owner, company=company, role='owner')
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {get_staff_tokens(owner)["access"]}')
        self.assertEqual(self.api.get('/api/super/me/').status_code, 401)
        # Даже если владелец салона — суперпользователь, нужен именно токен супер-админа
        owner.is_superuser = True
        owner.save()
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {get_staff_tokens(owner)["access"]}')
        self.assertEqual(self.api.get('/api/super/me/').status_code, 401)

    def test_token_dies_with_password_or_superuser_flag(self):
        _, data = self.setup_totp()
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {data["token"]}')
        self.user.is_superuser = False
        self.user.save()
        self.assertEqual(self.api.get('/api/super/me/').status_code, 403)
        self.user.is_superuser = True
        self.user.set_password('Another-pass-2026!')
        self.user.save()
        self.assertEqual(self.api.get('/api/super/me/').status_code, 401)

    def test_regenerate_backup_codes(self):
        secret, data = self.setup_totp()
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {data["token"]}')
        self.assertEqual(self.api.post('/api/super/auth/backup-codes/', {'code': '000000'}).status_code, 400)
        with mock.patch('superadmin.totp.time.time', return_value=(totp.current_step() + 1) * 30 + 1):
            code = totp.code_at(secret, totp.current_step())
            response = self.api.post('/api/super/auth/backup-codes/', {'code': code})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotEqual(response.data['backup_codes'], data['backup_codes'])


class SuperadminCommandTests(TestCase):
    def run_command(self, *args, stdin=''):
        out = io.StringIO()
        with mock.patch('sys.stdin', io.StringIO(stdin)):
            call_command('superadmin', *args, stdout=out)
        return out.getvalue()

    def test_create_status_reset(self):
        self.assertIn('Суперпользователей нет', self.run_command('--status'))
        self.assertIn('Создан', self.run_command('--username', 'boss', '--password-stdin', stdin=PASSWORD + '\n'))
        user = User.objects.get(username='boss')
        self.assertTrue(user.is_superuser and user.check_password(PASSWORD))
        SuperAdminTOTP.objects.create(user=user, secret=totp.new_secret())
        self.assertIn('boss', self.run_command('--status'))
        self.assertIn('сброшен', self.run_command('--username', 'boss', '--reset-totp'))
        self.assertFalse(SuperAdminTOTP.objects.filter(user=user).exists())
