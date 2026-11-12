from django.test import override_settings
from rest_framework.test import APIClient

from .models import StaffAppearance
from .tests import StaffTestCase


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AppearanceTests(StaffTestCase):
    """Оформление панели — у каждого сотрудника своё; цвет сайта записи — у владельца."""

    def test_default_dark(self):
        self.auth(self.admin)
        me = self.api.get('/api/staff/me/').data
        self.assertEqual(me['appearance'], {'theme': 'dark', 'accent': ''})
        self.assertEqual(me['company']['booking_accent'], '')

    def test_each_staff_has_own(self):
        for user, theme, accent in ((self.master_user, 'light', '#0d9488'), (self.admin, 'system', ''),
                                    (self.owner, 'light', '#4F46E5')):
            self.auth(user)
            response = self.api.patch('/api/staff/me/', {'appearance': {'theme': theme, 'accent': accent}},
                                      format='json')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['appearance'], {'theme': theme, 'accent': accent.upper()})
        self.auth(self.master_user)
        self.assertEqual(self.api.get('/api/staff/me/').data['appearance']['accent'], '#0D9488')
        self.assertEqual(StaffAppearance.objects.count(), 3)
        # Частичное изменение: только тема, цвет остаётся
        self.api.patch('/api/staff/me/', {'appearance': {'theme': 'dark'}}, format='json')
        self.assertEqual(self.api.get('/api/staff/me/').data['appearance'], {'theme': 'dark', 'accent': '#0D9488'})

    def test_bad_values_rejected(self):
        self.auth(self.admin)
        for bad in ({'theme': 'sea'}, {'accent': 'red'}, {'accent': '#12345'}, {'accent': '#1234567'},
                    {'accent': 'url(x)'}, {'accent': '#12345g'}):
            response = self.api.patch('/api/staff/me/', {'appearance': bad}, format='json')
            self.assertEqual(response.status_code, 400, bad)
        # Не объект — 400, а не ошибка сервера
        for body in (['x'], {'appearance': 'light'}, {'appearance': ['dark']}):
            self.assertEqual(self.api.patch('/api/staff/me/', body, format='json').status_code, 400, body)
        self.assertFalse(StaffAppearance.objects.exists())

    def test_booking_accent_only_owner(self):
        self.auth(self.admin)
        response = self.api.patch('/api/staff/company/', {'booking_accent': '#0D9488'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertIn('владелец', response.data['error'])
        self.auth(self.master_user)
        self.assertEqual(self.api.patch('/api/staff/company/', {'booking_accent': '#0D9488'},
                                        format='json').status_code, 403)
        self.auth(self.owner)
        response = self.api.patch('/api/staff/company/', {'booking_accent': '#0d9488'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['booking_accent'], '#0D9488')
        self.assertEqual(self.api.patch('/api/staff/company/', {'booking_accent': 'pink'},
                                        format='json').status_code, 400)
        # Клиенты видят цвет на сайте записи; сброс — пустая строка
        info = APIClient().get(f'/api/companies/{self.company.slug}/info/').data
        self.assertEqual(info['booking_accent'], '#0D9488')
        self.assertEqual(self.api.patch('/api/staff/company/', {'booking_accent': ''},
                                        format='json').data['booking_accent'], '')
        # Админ по-прежнему меняет остальные данные салона
        self.auth(self.admin)
        self.assertEqual(self.api.patch('/api/staff/company/', {'phone': '+37322111111'},
                                        format='json').status_code, 200)
