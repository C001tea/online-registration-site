"""Своя цена и длительность услуги у мастера."""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from dashboard.tokens import get_staff_tokens
from finance import penalties
from .models import AdminProfile, Appointment, Company, Master, MasterSchedule, MasterServiceTerms, Service


class TermsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company = Company.objects.create(name='Салон', slug='salon', penalty_percent=50)
        self.service = Service.objects.create(company=self.company, name='Наращивание', duration=120, price=450)
        self.anna = Master.objects.create(company=self.company, name='Анна')
        self.maria = Master.objects.create(company=self.company, name='Мария')
        for master in (self.anna, self.maria):
            master.services.add(self.service)
            for day in range(7):
                MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(9), end_time=time(18))
        # Мария — топ-мастер: дороже и дольше
        MasterServiceTerms.objects.create(master=self.maria, service=self.service, price=600, duration=150)
        self.day = timezone.localdate() + timedelta(days=3)

    def test_service_list_shows_range(self):
        data = self.api.get('/api/companies/salon/services/').data[0]
        self.assertEqual((data['price_min'], data['price_max']), ('450.00', '600.00'))
        self.assertEqual((data['duration_min'], data['duration_max']), (120, 150))

    def test_service_list_for_master_shows_own_terms(self):
        data = self.api.get('/api/companies/salon/services/', {'master_id': self.maria.id}).data[0]
        self.assertEqual((data['price'], data['duration']), ('600.00', 150))

    def test_booking_uses_master_duration_and_price(self):
        response = self.api.post('/api/companies/salon/appointments/', {
            'master': self.maria.id, 'service': self.service.id, 'client_name': 'Ирина',
            'client_phone': '+37369123456', 'date': self.day.isoformat(), 'start_time': '10:00', 'consent': True,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.end_time, time(12, 30))
        self.assertEqual(appointment.price, Decimal('600'))
        # Штраф считается от цены визита
        self.assertEqual(penalties.amount_for(appointment), Decimal('300.00'))

    def test_slots_respect_master_duration(self):
        slots = self.api.get('/api/companies/salon/slots/', {
            'master': self.maria.id, 'service': self.service.id, 'date': self.day.isoformat()}).data['slots']
        self.assertEqual(slots[-1], '15:30')  # 15:30 + 2,5 ч = 18:00


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class StaffTermsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company = Company.objects.create(name='Салон', slug='salon')
        self.service = Service.objects.create(company=self.company, name='Маникюр', duration=60, price=300)
        self.master = Master.objects.create(company=self.company, name='Анна')
        self.master.services.add(self.service)
        for day in range(7):
            MasterSchedule.objects.create(master=self.master, day_of_week=day, start_time=time(9), end_time=time(18))
        user = User.objects.create_user(username='owner', password='x-Strong-Pass-123')
        AdminProfile.objects.create(user=user, company=self.company, role='owner')
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + get_staff_tokens(user)['access'])

    def test_save_and_read_terms(self):
        response = self.api.patch(f'/api/staff/masters/{self.master.id}/', {
            'service_terms': [{'service': self.service.id, 'price': '350', 'duration': 75}]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['service_terms'], [{'service': self.service.id, 'price': '350.00', 'duration': 75}])
        # Пустые значения (как у услуги) не хранятся
        self.api.patch(f'/api/staff/masters/{self.master.id}/', {
            'service_terms': [{'service': self.service.id, 'price': None, 'duration': None}]}, format='json')
        self.assertFalse(MasterServiceTerms.objects.exists())

    def test_foreign_service_refused(self):
        other = Company.objects.create(name='Другой', slug='other')
        foreign = Service.objects.create(company=other, name='X', duration=30, price=1)
        response = self.api.patch(f'/api/staff/masters/{self.master.id}/', {
            'service_terms': [{'service': foreign.id, 'price': '1'}]}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_staff_booking_takes_master_terms(self):
        MasterServiceTerms.objects.create(master=self.master, service=self.service, price=350, duration=90)
        day = timezone.localdate() + timedelta(days=2)
        response = self.api.post('/api/staff/appointments/', {
            'master': self.master.id, 'service': self.service.id, 'date': day.isoformat(), 'start_time': '10:00',
            'client_name': 'Ольга', 'client_phone': '+37369000111'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['end_time'], '11:30')
        self.assertEqual(response.data['service']['price'], '350.00')
