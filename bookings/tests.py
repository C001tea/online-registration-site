from datetime import date, datetime, time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Company, Master, Service, Appointment, MasterSchedule, ClientProfile
from .slots import get_free_slots


def make_company(slug='salon'):
    company = Company.objects.create(name=slug, slug=slug)
    service = Service.objects.create(company=company, name='Маникюр', duration=60, price=300)
    master = Master.objects.create(company=company, name='Анна')
    master.services.add(service)
    for day in range(7):
        MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(9), end_time=time(12))
    return company, service, master


class BookingTestCase(TestCase):
    def setUp(self):
        cache.clear()  # счётчики лимита входа
        self.client = APIClient()
        self.company, self.service, self.master = make_company()
        self.day = timezone.localdate() + timedelta(days=7)

    def book(self, **overrides):
        payload = {
            'master': self.master.id,
            'service': self.service.id,
            'client_name': 'Ирина',
            'client_phone': '+37369123456',
            'date': self.day.isoformat(),
            'start_time': '10:00',
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(f'/api/companies/{self.company.slug}/appointments/', payload, format='json')


class FreeSlotsTests(BookingTestCase):
    def test_slots_follow_schedule_and_duration(self):
        slots = get_free_slots(self.master, self.day, 60)
        self.assertEqual(slots, [time(9), time(9, 30), time(10), time(10, 30), time(11)])

    def test_busy_time_is_excluded(self):
        Appointment.objects.create(
            company=self.company, master=self.master, service=self.service, client_name='x',
            client_phone='x', date=self.day, start_time=time(10), end_time=time(11)
        )
        self.assertEqual(get_free_slots(self.master, self.day, 60), [time(9), time(11)])

    def test_cancelled_appointment_frees_time(self):
        Appointment.objects.create(
            company=self.company, master=self.master, service=self.service, client_name='x',
            client_phone='x', date=self.day, start_time=time(10), end_time=time(11), status='cancelled'
        )
        self.assertIn(time(10), get_free_slots(self.master, self.day, 60))

    def test_past_date_has_no_slots(self):
        self.assertEqual(get_free_slots(self.master, timezone.localdate() - timedelta(days=1), 60), [])

    def test_past_time_today_is_excluded(self):
        today = date(2030, 1, 7)
        now = timezone.make_aware(datetime(2030, 1, 7, 10, 15))
        with mock.patch('bookings.slots.timezone.localdate', return_value=today), \
                mock.patch('bookings.slots.timezone.localtime', return_value=now):
            self.assertEqual(get_free_slots(self.master, today, 60), [time(10, 30), time(11)])

    def test_day_off_has_no_slots(self):
        MasterSchedule.objects.filter(master=self.master, day_of_week=self.day.weekday()).update(is_working=False)
        self.assertEqual(get_free_slots(self.master, self.day, 60), [])


class AppointmentCreateTests(BookingTestCase):
    def test_creates_appointment_with_end_time(self):
        response = self.book()
        self.assertEqual(response.status_code, 201, response.data)
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.end_time, time(11))
        self.assertEqual(appointment.client_phone, '+37369123456')

    def test_rejects_taken_time(self):
        self.assertEqual(self.book().status_code, 201)
        response = self.book(start_time='10:30')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_rejects_time_outside_schedule(self):
        self.assertEqual(self.book(start_time='03:00').status_code, 400)
        self.assertEqual(self.book(start_time='11:30').status_code, 400)  # 11:30 + 60 мин > 12:00

    def test_rejects_past_date(self):
        response = self.book(date=(timezone.localdate() - timedelta(days=1)).isoformat())
        self.assertEqual(response.status_code, 400)

    def test_rejects_master_from_other_company(self):
        _, _, other_master = make_company('other')
        other_master.services.add(self.service)
        self.assertEqual(self.book(master=other_master.id).status_code, 400)

    def test_rejects_master_without_service(self):
        other_service = Service.objects.create(company=self.company, name='Педикюр', duration=60, price=400)
        self.assertEqual(self.book(service=other_service.id).status_code, 400)

    def test_rejects_invalid_phone(self):
        self.assertEqual(self.book(client_phone='123').status_code, 400)

    def test_any_master_is_assigned_to_free_one(self):
        second = Master.objects.create(company=self.company, name='Мария')
        second.services.add(self.service)
        MasterSchedule.objects.create(master=second, day_of_week=self.day.weekday(), start_time=time(9), end_time=time(12))
        self.assertEqual(self.book().status_code, 201)

        response = self.book(master=None)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Appointment.objects.latest('id').master, second)

        self.assertEqual(self.book(master=None).status_code, 400)  # оба заняты


class ListViewsTests(BookingTestCase):
    def test_masters_only_from_company_and_active(self):
        make_company('other')
        Master.objects.create(company=self.company, name='Уволена', is_active=False)
        response = self.client.get(f'/api/companies/{self.company.slug}/masters/')
        self.assertEqual([m['name'] for m in response.data], ['Анна'])

    def test_masters_without_active_services_hidden(self):
        # Мастер без услуг или только со скрытыми услугами клиенту не показывается
        Master.objects.create(company=self.company, name='Без услуг')
        hidden = Service.objects.create(company=self.company, name='Архив', duration=30, price=1, is_active=False)
        Master.objects.create(company=self.company, name='Только скрытые').services.add(hidden)
        self.master.services.add(Service.objects.create(company=self.company, name='Педикюр', duration=60, price=400))
        response = self.client.get(f'/api/companies/{self.company.slug}/masters/')
        self.assertEqual([m['name'] for m in response.data], ['Анна'])

    def test_inactive_services_hidden(self):
        Service.objects.create(company=self.company, name='Архив', duration=30, price=1, is_active=False)
        response = self.client.get(f'/api/companies/{self.company.slug}/services/')
        self.assertEqual([s['name'] for s in response.data], ['Маникюр'])

    def test_slots_endpoint(self):
        response = self.client.get(
            f'/api/companies/{self.company.slug}/slots/',
            {'master': self.master.id, 'service': self.service.id, 'date': self.day.isoformat()}
        )
        self.assertEqual(response.data['slots'], ['09:00', '09:30', '10:00', '10:30', '11:00'])

    def test_points_requires_valid_date(self):
        self.assertEqual(self.client.get(f'/api/companies/{self.company.slug}/points/').status_code, 400)
        response = self.client.get(f'/api/companies/{self.company.slug}/points/', {'date': self.day.isoformat()})
        self.assertEqual(response.data, ['09:00', '09:30', '10:00', '10:30', '11:00', '11:30'])

    def test_slots_empty_when_master_cannot_book_service(self):
        url = f'/api/companies/{self.company.slug}/slots/'
        params = {'master': self.master.id, 'service': self.service.id, 'date': self.day.isoformat()}
        self.master.services.clear()
        self.assertEqual(self.client.get(url, params).data['slots'], [])
        self.master.services.add(self.service)
        self.service.is_active = False
        self.service.save()
        self.assertEqual(self.client.get(url, params).data['slots'], [])

    def test_services_by_time(self):
        response = self.client.get(
            f'/api/companies/{self.company.slug}/services/', {'date': self.day.isoformat(), 'time': '11:30'}
        )
        self.assertEqual(response.data, [])  # 60 минут после 11:30 не помещаются


class ClientAccountTests(BookingTestCase):
    def setUp(self):
        super().setUp()
        response = self.client.post('/api/auth/register/', {
            'phone': '+37369123456', 'password': 'secret-pass-1', 'name': 'Ирина'
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")

    def make_appointment(self, day, status='pending', start=time(10)):
        return Appointment.objects.create(
            company=self.company, master=self.master, service=self.service, client_name='Ирина',
            client_phone='+37369123456', date=day, start_time=start, end_time=time(11),
            status=status, client=self.user()
        )

    def user(self):
        return User.objects.get(username='+37369123456')

    def test_my_appointments_survive_deleted_master(self):
        appointment = self.make_appointment(self.day)
        self.master.delete()
        response = self.client.get('/api/appointments/my/')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data[0]['master'])
        self.assertEqual(response.data[0]['id'], appointment.id)

    def test_cancel_future_appointment(self):
        appointment = self.make_appointment(self.day)
        response = self.client.post(f'/api/appointments/{appointment.id}/cancel/')
        self.assertEqual(response.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, 'cancelled')
        # Повторная отмена
        self.assertEqual(self.client.post(f'/api/appointments/{appointment.id}/cancel/').status_code, 400)

    def test_cannot_cancel_past_appointment(self):
        appointment = self.make_appointment(timezone.localdate() - timedelta(days=1))
        self.assertEqual(self.client.post(f'/api/appointments/{appointment.id}/cancel/').status_code, 400)

    def review(self, rating=5):
        return self.client.post(
            f'/api/companies/{self.company.slug}/master/{self.master.id}/reviews/',
            {'rating': rating, 'comment': 'Супер'}, format='json'
        )

    def test_review_requires_visit(self):
        self.assertEqual(self.review().status_code, 403)
        self.make_appointment(self.day)  # будущая запись — ещё не визит
        self.assertEqual(self.review().status_code, 403)

    def test_review_after_visit(self):
        self.make_appointment(timezone.localdate() - timedelta(days=1))
        self.assertEqual(self.review(rating=6).status_code, 400)
        response = self.review()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['client_name'], 'Ирина')
        self.assertEqual(self.review().status_code, 400)  # второй отзыв

    def test_me(self):
        response = self.client.get('/api/auth/me/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'phone': '+37369123456', 'name': 'Ирина', 'surname': ''})
        ClientProfile.objects.all().delete()
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 404)
        self.client.credentials()
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 401)

    def test_cannot_cancel_no_show(self):
        appointment = self.make_appointment(self.day, status='no_show')
        response = self.client.post(f'/api/appointments/{appointment.id}/cancel/')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Эту запись нельзя отменить'})

    def test_no_review_to_self(self):
        self.master.user = self.user()
        self.master.save()
        self.make_appointment(timezone.localdate() - timedelta(days=1), status='completed')
        response = self.review()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {'error': 'Нельзя оставить отзыв самому себе'})

    def test_staff_appointment_gives_review_only_when_completed(self):
        appointment = self.make_appointment(timezone.localdate() - timedelta(days=1))
        appointment.created_by_staff = True
        appointment.save()
        self.assertEqual(self.review().status_code, 403)
        appointment.status = 'completed'
        appointment.save()
        self.assertEqual(self.review().status_code, 201)

    def test_login_attempts_limited(self):
        self.client.credentials()

        def login(password):
            return self.client.post('/api/auth/login/', {'phone': '+37369123456', 'password': password}, format='json')

        for _ in range(5):
            self.assertEqual(login('wrong').status_code, 400)
        response = login('wrong')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data, {'error': 'Слишком много попыток входа. Попробуйте через 15 минут'})
        self.assertEqual(login('secret-pass-1').status_code, 429)
        cache.clear()
        self.assertEqual(login('secret-pass-1').status_code, 200)
