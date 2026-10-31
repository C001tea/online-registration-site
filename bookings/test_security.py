import os
import subprocess
import sys
from datetime import time, timedelta

from django.contrib.auth.models import User
from django.conf import settings
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from dashboard.tokens import get_staff_tokens
from .models import AdminProfile, Appointment
from .tests import BookingTestCase


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SecurityTests(BookingTestCase):
    """Защита от перебора и спама, сроки и обновление токенов клиента."""

    def register(self, phone='+37369123400', password='Strong-pass-2026', ip='10.0.0.1'):
        return self.client.post('/api/auth/register/', {'phone': phone, 'password': password, 'name': 'Ана'},
                                format='json', HTTP_X_REAL_IP=ip)

    def test_register_requires_strong_password_and_is_rate_limited(self):
        for weak in ('1', '12345678', 'password'):
            response = self.register(password=weak)
            self.assertEqual(response.status_code, 400, weak)
        self.assertEqual(self.register().status_code, 201)
        for i in range(10):  # лимит — 10 регистраций в час с одного IP
            self.register(phone=f'+373691235{i:02d}', ip='10.0.0.2')
        self.assertEqual(self.register(phone='+37369123599', ip='10.0.0.2').status_code, 429)
        self.assertEqual(self.register(phone='+37369123598', ip='10.0.0.3').status_code, 201)  # другой IP

    def test_client_token_refresh_and_staff_token_rejected(self):
        tokens = self.register().data
        response = self.client.post('/api/auth/refresh/', {'refresh': tokens['refresh']}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('access', response.data)
        staff = User.objects.create_user('owner', password='Strong-pass-2026')
        AdminProfile.objects.create(user=staff, company=self.company, role='owner')
        staff_refresh = get_staff_tokens(staff)['refresh']
        self.assertEqual(self.client.post('/api/auth/refresh/', {'refresh': staff_refresh}, format='json').status_code, 401)

    def test_booking_honeypot_and_limits(self):
        self.assertEqual(self.book(website='http://spam').status_code, 400)
        self.assertFalse(Appointment.objects.exists())
        # Не больше трёх предстоящих онлайн-записей на один номер
        for hour in (9, 10, 11):
            self.assertEqual(self.book(start_time=f'{hour:02d}:00', date=(self.day + timedelta(days=hour)).isoformat()).status_code, 201)
        response = self.book(date=(self.day + timedelta(days=1)).isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertIn('3 предстоящие записи', str(response.data))
        # Отменённая запись место освобождает
        Appointment.objects.filter(start_time=time(9)).update(status='cancelled')
        self.assertEqual(self.book(date=(self.day + timedelta(days=1)).isoformat()).status_code, 201)

    def test_booking_ip_limit(self):
        api = APIClient()
        url = f'/api/companies/{self.company.slug}/appointments/'
        for _ in range(30):
            api.post(url, {}, format='json', HTTP_X_REAL_IP='10.9.9.9')
        self.assertEqual(api.post(url, {}, format='json', HTTP_X_REAL_IP='10.9.9.9').status_code, 429)
        self.assertNotEqual(api.post(url, {}, format='json', HTTP_X_REAL_IP='10.9.9.8').status_code, 429)

    # Страница входа админки подключает статику: в CI collectstatic не запускается
    @override_settings(STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    })
    def test_admin_login_rate_limited(self):
        for _ in range(5):
            self.client.post('/admin/login/', {'username': 'root', 'password': 'x'})
        self.assertEqual(self.client.post('/admin/login/', {'username': 'root', 'password': 'x'}).status_code, 429)

    def test_health_shows_own_ip(self):
        self.assertEqual(self.client.get('/api/health/', {'ip': '1'}, HTTP_X_REAL_IP='1.2.3.4').data['ip'], '1.2.3.4')
        self.assertNotIn('ip', self.client.get('/api/health/').data)


class ProductionDebugTests(SimpleTestCase):
    """На боевом сервере (задан API_DOMAIN) DEBUG выключен даже при DEBUG=True в .env."""

    def debug_with(self, **env):
        code = 'import config.settings as s; print(s.DEBUG)'
        base = {k: v for k, v in os.environ.items() if k not in ('API_DOMAIN', 'DEBUG')}
        environment = {**base, 'SECRET_KEY': 'test-secret', **env}
        result = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True,
                                cwd=settings.BASE_DIR, check=True)
        return result.stdout.strip().splitlines()[-1]

    def test_debug_forced_off_in_production(self):
        self.assertEqual(self.debug_with(DEBUG='True', API_DOMAIN='api.example.md'), 'False')
        self.assertEqual(self.debug_with(DEBUG='True'), 'True')  # локально можно
