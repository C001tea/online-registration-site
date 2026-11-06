"""Волна A онлайн-записи: ссылка управления (перенос/отмена без входа), календарь с загруженностью,
ближайшие окна, мини-календарь журнала и всплывающие «Новая запись»."""
from datetime import time, timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from dashboard.tokens import get_staff_tokens
from finance.models import Penalty
from .models import AdminProfile, Appointment, Company, Master, MasterDayOff, MasterSchedule, Service
from .slots import month_overview


def make_salon():
    company = Company.objects.create(name='Салон', slug='salon', cancel_hours=2, late_cancel_hours=3)
    service = Service.objects.create(company=company, name='Маникюр', duration=60, price=300)
    master = Master.objects.create(company=company, name='Анна')
    master.services.add(service)
    for day in range(7):
        MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(9), end_time=time(13))
    return company, service, master


class ManageLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company, self.service, self.master = make_salon()
        self.day = timezone.localdate() + timedelta(days=5)

    def book(self, start='10:00'):
        response = self.api.post('/api/companies/salon/appointments/', {
            'master': self.master.id, 'service': self.service.id, 'client_name': 'Ирина',
            'client_phone': '+37369123456', 'date': self.day.isoformat(), 'start_time': start, 'consent': True,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data['manage_token']

    def test_token_given_and_details_shown(self):
        token = self.book()
        self.assertTrue(token)
        data = self.api.get(f'/api/manage/{token}/').data
        self.assertEqual(data['start_time'], '10:00')
        self.assertEqual(data['master']['name'], 'Анна')
        self.assertTrue(data['can_cancel'])
        self.assertTrue(data['can_reschedule'])

    def test_unknown_token_404(self):
        self.assertEqual(self.api.get('/api/manage/nope/').status_code, 404)

    def test_staff_created_appointment_gets_token(self):
        appointment = Appointment.objects.create(
            company=self.company, master=self.master, service=self.service, client_name='А',
            client_phone='+37369000000', date=self.day, start_time=time(9), end_time=time(10))
        self.assertTrue(appointment.manage_token)

    def test_reschedule_moves_and_frees_old_time(self):
        token = self.book('10:00')
        slots = self.api.get(f'/api/manage/{token}/slots/', {'date': self.day.isoformat()}).data['slots']
        # Своя запись не мешает: 10:00 тоже предлагается
        self.assertIn('10:00', slots)
        self.assertIn('11:30', slots)
        response = self.api.post(f'/api/manage/{token}/reschedule/',
                                 {'date': self.day.isoformat(), 'start_time': '11:30'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        appointment = Appointment.objects.get(manage_token=token)
        self.assertEqual((appointment.start_time, appointment.end_time), (time(11, 30), time(12, 30)))

    def test_reschedule_into_busy_time_refused(self):
        self.book('11:00')
        token = self.api.post('/api/companies/salon/appointments/', {
            'master': self.master.id, 'service': self.service.id, 'client_name': 'Ольга',
            'client_phone': '+37369123457', 'date': self.day.isoformat(), 'start_time': '09:00', 'consent': True,
        }, format='json').data['manage_token']
        response = self.api.post(f'/api/manage/{token}/reschedule/',
                                 {'date': self.day.isoformat(), 'start_time': '11:00'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Appointment.objects.get(manage_token=token).start_time, time(9))

    def test_late_reschedule_refused(self):
        token = self.book()
        appointment = Appointment.objects.get(manage_token=token)
        soon = timezone.localtime() + timedelta(hours=2, minutes=30)  # до визита меньше late_cancel_hours
        appointment.date, appointment.start_time = soon.date(), soon.time().replace(second=0, microsecond=0)
        appointment.save()
        self.assertFalse(self.api.get(f'/api/manage/{token}/').data['can_reschedule'])
        response = self.api.post(f'/api/manage/{token}/reschedule/',
                                 {'date': self.day.isoformat(), 'start_time': '11:00'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_cancel_by_link(self):
        token = self.book()
        response = self.api.post(f'/api/manage/{token}/cancel/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'cancelled')
        self.assertEqual(self.api.post(f'/api/manage/{token}/cancel/').status_code, 400)

    def test_late_cancel_by_link_creates_penalty(self):
        token = self.book()
        appointment = Appointment.objects.get(manage_token=token)
        soon = timezone.localtime() + timedelta(hours=2, minutes=30)
        appointment.date, appointment.start_time = soon.date(), soon.time().replace(second=0, microsecond=0)
        appointment.save()
        self.assertEqual(self.api.post(f'/api/manage/{token}/cancel/').status_code, 200)
        self.assertTrue(Penalty.objects.filter(appointment=appointment, kind='late_cancel').exists())


class CalendarTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company, self.service, self.master = make_salon()

    def test_month_overview_load_and_day_off(self):
        day = timezone.localdate() + timedelta(days=3)
        Appointment.objects.create(company=self.company, master=self.master, service=self.service,
                                   client_name='А', client_phone='+37369000000', date=day,
                                   start_time=time(9), end_time=time(11), status='confirmed')
        MasterDayOff.objects.create(master=self.master, date=day + timedelta(days=1))
        days = {d['date']: d for d in month_overview([self.master], day, day + timedelta(days=1), 60)}
        self.assertEqual(days[day.isoformat()]['load'], 0.5)  # 2 ч из 4
        self.assertEqual(days[day.isoformat()]['free'], 3)   # 11:00, 11:30, 12:00
        self.assertFalse(days[(day + timedelta(days=1)).isoformat()]['working'])

    def test_calendar_endpoint(self):
        month = timezone.localdate().strftime('%Y-%m')
        response = self.api.get('/api/companies/salon/calendar/', {'month': month, 'service': self.service.id})
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data['days']), 28)
        self.assertEqual(self.api.get('/api/companies/salon/calendar/', {'month': 'x'}).status_code, 400)
        far = (timezone.localdate() + timedelta(days=400)).strftime('%Y-%m')
        self.assertEqual(self.api.get('/api/companies/salon/calendar/', {'month': far}).data['days'], [])

    def test_nearest_slots(self):
        data = self.api.get('/api/companies/salon/nearest/', {'service': self.service.id}).data
        self.assertEqual(data[0]['master'], self.master.id)
        self.assertTrue(data[0]['date'])
        self.assertLessEqual(len(data[0]['slots']), 4)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class StaffWaveATests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company, self.service, self.master = make_salon()
        user = User.objects.create_user(username='owner', password='x-Strong-Pass-123')
        AdminProfile.objects.create(user=user, company=self.company, role='owner')
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + get_staff_tokens(user)['access'])

    def test_staff_calendar(self):
        month = timezone.localdate().strftime('%Y-%m')
        response = self.api.get('/api/staff/calendar/', {'month': month})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all('load' in d for d in response.data['days']))

    def test_recent_online(self):
        since = timezone.now().isoformat()
        day = timezone.localdate() + timedelta(days=2)
        online = Appointment.objects.create(company=self.company, master=self.master, service=self.service,
                                            client_name='Онлайн', client_phone='+37369000001', date=day,
                                            start_time=time(9), end_time=time(10))
        Appointment.objects.create(company=self.company, master=self.master, service=self.service,
                                   client_name='Админ', client_phone='+37369000002', date=day,
                                   start_time=time(11), end_time=time(12), created_by_staff=True)
        response = self.api.get('/api/staff/appointments/recent-online/', {'since': since})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([a['id'] for a in response.data['items']], [online.id])
        self.assertTrue(response.data['now'])
