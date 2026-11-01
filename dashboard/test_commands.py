import io
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from bookings.models import AdminProfile, Appointment, Company, Master

PASSWORD = 'Salon-pass-2026'


def run_create_salon(data, *args):
    out = io.StringIO()
    with mock.patch('sys.stdin', io.StringIO(json.dumps(data))):
        call_command('create_salon', *args, stdout=out)
    return out.getvalue()


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CreateSalonTests(TestCase):
    data = {'name': 'Nail Bar', 'slug': 'nail-bar', 'owner': 'nailbar', 'password': PASSWORD,
            'address': 'str. Test 1', 'phone': '+37322000001'}

    def test_dry_run_creates_nothing(self):
        out = run_create_salon(self.data)
        self.assertIn('Пробный запуск', out)
        self.assertFalse(Company.objects.filter(slug='nail-bar').exists())
        self.assertFalse(User.objects.filter(username='nailbar').exists())

    def test_creates_salon_and_owner_who_can_log_in(self):
        out = run_create_salon(self.data, '--yes')
        self.assertIn('?salon=nail-bar', out)
        company = Company.objects.get(slug='nail-bar')
        self.assertEqual(company.name, 'Nail Bar')
        self.assertEqual(AdminProfile.objects.get(user__username='nailbar').company, company)
        self.assertNotIn(PASSWORD, out)
        response = APIClient().post('/api/staff/auth/login/', {'username': 'nailbar', 'password': PASSWORD}, format='json')
        self.assertEqual(response.status_code, 200)

    def test_rejects_bad_input(self):
        Company.objects.create(name='Есть', slug='taken')
        User.objects.create_user('busy', password=PASSWORD)
        cases = [
            ({'slug': 'Bad Slug'}, 'slug'),
            ({'slug': 'api'}, 'служебный'),
            ({'slug': 'taken'}, 'уже есть'),
            ({'owner': 'busy'}, 'уже занят'),
            ({'owner': 'BUSY'}, 'уже занят'),
            ({'password': '12345678'}, 'Пароль владельца'),
            ({'name': ''}, 'Название'),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                with self.assertRaisesMessage(CommandError, message):
                    run_create_salon({**self.data, **change}, '--yes')
        self.assertFalse(Company.objects.filter(slug='nail-bar').exists())


class E2ESeedTests(TestCase):
    def test_seed_is_repeatable(self):
        for _ in range(2):
            call_command('e2e_seed', '--password', PASSWORD, stdout=io.StringIO())
        company = Company.objects.get(slug='e2e')
        self.assertEqual(Master.objects.filter(company=company).count(), 2)
        self.assertEqual(Appointment.objects.filter(company=company).count(), 2)
        self.assertEqual(User.objects.filter(username__startswith='e2e_').count(), 3)

    @override_settings(API_DOMAIN='api.example.md')
    def test_refuses_on_production(self):
        with self.assertRaisesMessage(CommandError, 'боевой сервер'):
            call_command('e2e_seed', '--password', PASSWORD, stdout=io.StringIO())
        self.assertFalse(Company.objects.filter(slug='e2e').exists())
