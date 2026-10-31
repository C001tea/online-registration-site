from datetime import time, timedelta

from django.contrib.auth.models import User
from django.test import override_settings
from rest_framework.test import APIClient

from bookings.models import Appointment, ClientProfile
from finance.models import CashOperation, Penalty
from notifications.models import AppointmentTelegram, ClientTelegram
from .models import ClientNote
from .tests import StaffTestCase

PHONE = '+37369000001'


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ForgetClientTests(StaffTestCase):
    """Удаление данных клиента по его обращению: в салоне и самим клиентом на сайте."""

    def test_salon_anonymizes_client(self):
        past = self.appointment(day=self.today - timedelta(days=2), status='completed', notes='любит кофе')
        self.appointment(day=self.day)
        other_salon = self.appointment(master=self.master_b, company=self.company_b, service=self.service_b)
        AppointmentTelegram.objects.create(appointment=past, chat_id=5001)
        ClientNote.objects.create(company=self.company, phone=PHONE, text='аллергия')
        penalty = Penalty.objects.create(company=self.company, appointment=past, phone=PHONE, kind='no_show', amount=100)
        penalty.operation = CashOperation.objects.create(company=self.company, date=self.today, kind='income', amount=100,
                                                         category='Штраф за неявку', comment='Ирина, запись')
        penalty.save()
        self.auth(self.master_user)
        self.assertEqual(self.api.post('/api/staff/clients/forget/', {'phone': PHONE}, format='json').status_code, 403)
        self.auth(self.admin)
        response = self.api.post('/api/staff/clients/forget/', {'phone': '069000001'}, format='json')
        self.assertEqual(response.data, {'anonymized': 2})
        mine = Appointment.objects.filter(company=self.company)
        self.assertEqual(set(mine.values_list('client_name', 'client_phone', 'notes')), {('Клиент (данные удалены)', '', '')})
        self.assertFalse(ClientNote.objects.exists() or Penalty.objects.exists() or AppointmentTelegram.objects.exists())
        self.assertEqual(CashOperation.objects.get().comment, 'Штраф (данные клиента удалены)')  # деньги в кассе остались
        other_salon.refresh_from_db()
        self.assertEqual(other_salon.client_phone, PHONE)  # чужой салон не тронут
        self.assertEqual(self.api.get('/api/staff/clients/card/', {'phone': PHONE}).status_code, 404)
        self.assertEqual(self.api.post('/api/staff/clients/forget/', {'phone': PHONE}, format='json').status_code, 404)

    def test_client_deletes_own_account(self):
        user = User.objects.create_user(PHONE, password='Strong-pass-2026')
        ClientProfile.objects.create(user=user, phone=PHONE, name='Ирина')
        ClientTelegram.objects.create(user=user, chat_id=5001)
        visit = self.appointment(client=user)
        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.delete('/api/auth/me/').status_code, 204)
        self.assertFalse(User.objects.filter(username=PHONE).exists())
        self.assertFalse(ClientTelegram.objects.exists())
        visit.refresh_from_db()
        self.assertIsNone(visit.client)  # запись в салоне осталась, без аккаунта
        # Персонал так удалить себя не может
        api.force_authenticate(self.owner)
        self.assertEqual(api.delete('/api/auth/me/').status_code, 404)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AutoConfirmTests(StaffTestCase):
    def book(self):
        return APIClient().post(f'/api/companies/{self.company.slug}/appointments/', {
            'master': self.master.id, 'service': self.service.id, 'client_name': 'Ирина', 'client_phone': '+37369123456',
            'date': self.day.isoformat(), 'start_time': '10:00', 'consent': True}, format='json')

    def test_auto_confirm_setting(self):
        response = self.book()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['status'], 'pending')
        self.auth(self.owner)
        self.assertTrue(self.api.patch('/api/staff/company/', {'auto_confirm': True}, format='json').data['auto_confirm'])
        Appointment.objects.all().delete()
        self.assertEqual(self.book().data['status'], 'confirmed')
        self.assertTrue(APIClient().get(f'/api/companies/{self.company.slug}/info/').data['auto_confirm'])
