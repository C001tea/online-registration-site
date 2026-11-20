"""Соло-мастер: регистрация «Я мастер, работаю сам», шаблоны услуг, график, район вместо адреса."""
from django.test import TestCase
from rest_framework.test import APIClient

from bookings.models import Company, Master, MasterSchedule, Service
from .solo import TEMPLATES
from .test_signup import FAST_HASH, OPEN, SIGNUP


@FAST_HASH
@OPEN
class SoloSignupTests(TestCase):
    def setUp(self):
        self.api = APIClient()

    def signup(self, **changes):
        data = {**SIGNUP, 'salon_name': 'Алина Мунтяну', 'slug': 'alina', 'kind': 'solo',
                'specialization': 'brows', 'district': ' Ботаника ', **changes}
        return self.api.post('/api/staff/signup/', data, format='json', HTTP_X_REAL_IP='10.0.0.9')

    def test_solo_gets_master_services_schedule_and_features(self):
        response = self.signup()
        self.assertEqual(response.status_code, 201, response.data)
        company = Company.objects.get(slug='alina')
        self.assertEqual((company.kind, company.district, company.address_private), ('solo', 'Ботаника', True))
        self.assertTrue(all([company.rhythm_enabled, company.reviews_enabled, company.portfolio_enabled,
                             company.promo_enabled]))
        master = Master.objects.get(company=company)
        self.assertEqual(master.name, 'Алина Мунтяну')
        self.assertEqual(master.user.username, 'owner@example.com')
        self.assertEqual(Service.objects.filter(company=company).count(), len(TEMPLATES['brows'][1]))
        self.assertEqual(master.services.count(), len(TEMPLATES['brows'][1]))
        self.assertEqual(MasterSchedule.objects.filter(master=master, is_working=True).count(), 6)
        me = response.data['me']
        self.assertEqual((me['role'], me['company']['kind'], me['master_id']), ('owner', 'solo', master.id))

    def test_salon_signup_unchanged(self):
        response = self.signup(kind='salon', slug='salon-x')
        self.assertEqual(response.status_code, 201, response.data)
        company = Company.objects.get(slug='salon-x')
        self.assertEqual((company.kind, company.address_private, company.promo_enabled), ('salon', False, False))
        self.assertFalse(Master.objects.filter(company=company).exists())
        self.assertFalse(Service.objects.filter(company=company).exists())

    def test_without_kind_is_salon(self):
        data = dict(SIGNUP)
        response = self.api.post('/api/staff/signup/', data, format='json', HTTP_X_REAL_IP='10.0.0.8')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Company.objects.get().kind, 'salon')

    def test_bad_specialization(self):
        self.assertEqual(self.signup(specialization='rocket').status_code, 400)
        self.assertFalse(Company.objects.exists())


class PublicAddressTests(TestCase):
    def test_private_address_shows_district_only(self):
        company = Company.objects.create(name='Алина', slug='alina', address='ул. Дачия 5, кв. 12',
                                         district='Ботаника', address_private=True, kind='solo',
                                         latitude=47, longitude=28)
        data = APIClient().get('/api/companies/alina/info/').data
        self.assertEqual((data['address'], data['latitude'], data['kind']), ('Ботаника', None, 'solo'))
        company.address_private = False
        company.save()
        data = APIClient().get('/api/companies/alina/info/').data
        self.assertEqual(data['address'], 'ул. Дачия 5, кв. 12')

    def test_manage_page_keeps_full_address(self):
        from datetime import time, timedelta
        from django.utils import timezone
        from bookings.models import Appointment
        company = Company.objects.create(name='Алина', slug='alina', address='ул. Дачия 5, кв. 12',
                                         district='Ботаника', address_private=True)
        master = Master.objects.create(company=company, name='Алина')
        appointment = Appointment.objects.create(
            company=company, master=master, client_name='Ирина', client_phone='+37369000001',
            date=timezone.localdate() + timedelta(days=2), start_time=time(10), end_time=time(11))
        data = APIClient().get(f'/api/manage/{appointment.manage_token}/').data
        self.assertEqual(data['salon']['address'], 'ул. Дачия 5, кв. 12')
