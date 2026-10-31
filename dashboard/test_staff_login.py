from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from .tests import PASSWORD, StaffTestCase


def run(*args, stdin=None):
    out = StringIO()
    with mock.patch('sys.stdin', StringIO(stdin or '')):
        call_command('staff_login', *args, stdout=out)
    return out.getvalue()


class StaffLoginTests(StaffTestCase):
    def test_list_shows_logins_without_passwords(self):
        text = run('--company', 'a')
        for name in ('owner', 'admin', 'anna'):
            self.assertIn(name, text)
        self.assertNotIn('owner_b', text)  # чужой салон
        self.assertNotIn('pbkdf2', text)

    def test_reset_password_and_login(self):
        run('--company', 'a', '--username', 'owner', '--password-stdin', stdin='Brand-new-pass-2026\n')
        response = self.api.post('/api/staff/auth/login/', {'username': 'owner', 'password': 'Brand-new-pass-2026'},
                                 format='json')
        self.assertEqual(response.status_code, 200, response.data)
        old = self.api.post('/api/staff/auth/login/', {'username': 'owner', 'password': PASSWORD}, format='json')
        self.assertEqual(old.status_code, 400)

    def test_refuses(self):
        with self.assertRaises(CommandError):  # логин чужого салона
            run('--company', 'a', '--username', 'owner_b', '--password-stdin', stdin='Brand-new-pass-2026')
        with self.assertRaises(CommandError):  # слабый пароль
            run('--company', 'a', '--username', 'owner', '--password-stdin', stdin='12345678')
        with self.assertRaises(CommandError):  # пустой пароль
            run('--company', 'a', '--username', 'owner', '--password-stdin', stdin='\n')
        admin = User.objects.get(username='admin')
        admin.is_superuser = True
        admin.save()
        with self.assertRaises(CommandError):  # суперпользователь — только через админку Django
            run('--company', 'a', '--username', 'admin', '--password-stdin', stdin='Brand-new-pass-2026')
