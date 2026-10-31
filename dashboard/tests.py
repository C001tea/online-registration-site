from datetime import time, timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from bookings.models import (
    AdminProfile, Appointment, ClientProfile, Company, Master, MasterDayOff, MasterSchedule, Service, ServiceCategory
)
from .errors import StaffError
from .permissions import get_staff
from .serializers import StaffAppointmentWriteSerializer
from .tokens import get_staff_tokens

PASSWORD = 'Str0ng-pass-123'


def make_salon(slug):
    company = Company.objects.create(name=f'Салон {slug}', slug=slug)
    category = ServiceCategory.objects.create(company=company, name='Руки')
    service = Service.objects.create(company=company, category=category, name='Маникюр', duration=60, price=200)
    master = Master.objects.create(company=company, name=f'Анна {slug}')
    master.services.add(service)
    for day in range(7):
        MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(9), end_time=time(18))
    return company, category, service, master


def make_owner(username, company, role='owner'):
    user = User.objects.create_user(username=username, password=PASSWORD)
    AdminProfile.objects.create(user=user, company=company, role=role)
    return user


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class StaffTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company, self.category, self.service, self.master = make_salon('a')
        self.owner = make_owner('owner', self.company)
        self.admin = make_owner('admin', self.company, role='admin')
        self.master_user = User.objects.create_user(username='anna', password=PASSWORD)
        self.master.user = self.master_user
        self.master.save()
        self.master2 = Master.objects.create(company=self.company, name='Мария')
        self.master2.services.add(self.service)
        for day in range(7):
            MasterSchedule.objects.create(master=self.master2, day_of_week=day, start_time=time(9), end_time=time(18))

        self.company_b, self.category_b, self.service_b, self.master_b = make_salon('b')
        self.owner_b = make_owner('owner_b', self.company_b)

        self.today = timezone.localdate()
        self.day = self.today + timedelta(days=7)

    def auth(self, user):
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + get_staff_tokens(user)['access'])

    def appointment(self, master=None, start=time(10), end=time(11), day=None, status='confirmed', **extra):
        master = master if master is not None else self.master
        data = dict(
            company=self.company, master=master, service=self.service, client_name='Ирина',
            client_phone='+37369000001', date=day or self.day, start_time=start, end_time=end, status=status,
        )
        data.update(extra)
        return Appointment.objects.create(**data)

    def create_payload(self, **overrides):
        payload = {
            'master': self.master.id, 'service': self.service.id, 'date': self.day.isoformat(),
            'start_time': '10:00', 'client_name': 'Ирина', 'client_phone': '069123456',
        }
        payload.update(overrides)
        return payload


class LoginTests(StaffTestCase):
    def login(self, username, password=PASSWORD, **extra):
        return self.api.post('/api/staff/auth/login/', {'username': username, 'password': password}, format='json', **extra)

    def test_login_success_has_staff_scope(self):
        response = self.login('owner')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(AccessToken(response.data['access'])['scope'], 'staff')
        self.assertEqual(response.data['me']['role'], 'owner')
        self.assertEqual(response.data['me']['company']['slug'], 'a')

    def test_wrong_password(self):
        response = self.login('owner', 'wrong')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Неверный логин или пароль'})

    def test_not_staff(self):
        # Верный пароль от аккаунта без доступа к панели — тот же ответ, что и неверный пароль
        User.objects.create_user(username='stranger', password=PASSWORD)
        response = self.login('stranger')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Неверный логин или пароль'})
        self.assertNotIn('access', response.data)

    def make_client(self, phone='+37369123456'):
        user = User.objects.create_user(username=phone, password=PASSWORD)
        ClientProfile.objects.create(user=user, phone=phone, name='Клиент')
        return user

    def client_login(self, phone, password=PASSWORD):
        return APIClient().post('/api/auth/login/', {'phone': phone, 'password': password}, format='json')

    def test_client_password_not_confirmed_and_counted(self):
        self.make_client()
        for _ in range(4):
            self.assertEqual(self.login('069123456', 'wrong').status_code, 400)
        # Верный пароль клиента: 400 как у неверного и тоже считается неудачей
        response = self.login('+373 69 123 456')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Неверный логин или пароль'})
        self.assertEqual(self.login('69123456').status_code, 429)
        self.assertEqual(self.client_login('+37369123456').status_code, 429)

    def test_phone_spellings_share_one_counter(self):
        phone_user = self.make_client('+37369555111')
        self.master2.user = phone_user
        self.master2.save()
        spellings = ['069555111', '69555111', '+373 69 555 111', '+373-69-555-111', '(069)555111']
        for spelling in spellings:
            self.assertEqual(self.login(spelling, 'wrong').status_code, 400, spelling)
        # 6-я попытка с любым написанием, даже с верным паролем
        self.assertEqual(self.login('0 69 55 51 11').status_code, 429)
        self.assertEqual(self.login('+37369555111').status_code, 429)

    def test_client_lock_blocks_staff_login(self):
        phone_user = self.make_client('+37369555111')
        self.master2.user = phone_user
        self.master2.save()
        for _ in range(5):
            self.assertEqual(self.client_login('+37369555111', 'wrong').status_code, 400)
        self.assertEqual(self.client_login('+37369555111').status_code, 429)
        self.assertEqual(self.login('069555111').status_code, 429)
        cache.clear()
        self.assertEqual(self.login('069555111').status_code, 200)

    def test_staff_lock_blocks_client_login(self):
        self.make_client()
        for _ in range(5):
            self.assertEqual(self.login('069 123 456', 'wrong').status_code, 400)
        self.assertEqual(self.client_login('+37369123456').status_code, 429)

    def test_master_logs_in_by_local_phone(self):
        phone_user = User.objects.create_user(username='+37369555111', password=PASSWORD)
        ClientProfile.objects.create(user=phone_user, phone='+37369555111', name='Мария')
        self.master2.user = phone_user
        self.master2.save()
        response = self.login('069555111')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['me']['role'], 'master')
        self.assertEqual(response.data['me']['master_id'], self.master2.id)

    def test_sixth_wrong_attempt_is_blocked(self):
        for _ in range(5):
            self.assertEqual(self.login('owner', 'wrong').status_code, 400)
        response = self.login('owner', 'wrong')
        self.assertEqual(response.status_code, 429)
        self.assertIn('15 минут', response.data['error'])
        # Пока окно не истекло — даже с верным паролем
        self.assertEqual(self.login('OWNER ').status_code, 429)

    def test_success_resets_counter(self):
        for _ in range(4):
            self.login('owner', 'wrong')
        self.assertEqual(self.login('owner').status_code, 200)
        for _ in range(5):
            self.assertEqual(self.login('owner', 'wrong').status_code, 400)

    def test_login_ignores_broken_bearer(self):
        response = self.login('owner', HTTP_AUTHORIZATION='Bearer broken.token.value')
        self.assertEqual(response.status_code, 200)

    def test_refresh(self):
        tokens = self.login('owner').data
        response = self.api.post('/api/staff/auth/refresh/', {'refresh': tokens['refresh']}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(AccessToken(response.data['access'])['scope'], 'staff')

    def test_client_refresh_rejected(self):
        client_refresh = RefreshToken.for_user(self.owner)
        response = self.api.post('/api/staff/auth/refresh/', {'refresh': str(client_refresh)}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_change_password(self):
        old_tokens = self.login('owner').data
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + old_tokens['access'])
        url = '/api/staff/auth/password/'
        self.assertEqual(self.api.post(url, {'old_password': 'x', 'new_password': 'N3w-pass-777'}).status_code, 400)
        response = self.api.post(url, {'old_password': PASSWORD, 'new_password': '123'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('new_password', response.data)
        response = self.api.post(url, {'old_password': PASSWORD, 'new_password': 'N3w-pass-777'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), {'access', 'refresh'})
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('N3w-pass-777'))

        # Старые токены больше не действуют
        me = self.api.get('/api/staff/me/')
        self.assertEqual(me.status_code, 401)
        self.assertEqual(me.data, {'error': 'Сессия устарела, войдите снова'})
        self.assertEqual(self.refresh(old_tokens['refresh']).status_code, 401)

        # Новые — работают, в том числе refresh
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + response.data['access'])
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 200)
        refreshed = self.refresh(response.data['refresh'])
        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + refreshed.data['access'])
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 200)

    def refresh(self, token):
        return APIClient().post('/api/staff/auth/refresh/', {'refresh': token}, format='json')

    def test_password_reset_by_admin_kills_tokens(self):
        old_tokens = self.login('anna').data
        self.auth(self.admin)
        response = self.api.post(
            f'/api/staff/masters/{self.master.id}/access/', {'username': 'anna', 'password': 'Res3t-pass-77'}, format='json'
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.refresh(old_tokens['refresh']).status_code, 401)
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + old_tokens['access'])
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 401)
        self.assertEqual(self.api.get('/api/staff/appointments/pending-count/').status_code, 401)
        self.assertEqual(self.login('anna', 'Res3t-pass-77').status_code, 200)

    def test_token_without_password_claim_rejected(self):
        tokens = self.login('owner').data
        refresh = RefreshToken(tokens['refresh'])
        del refresh['pwd']
        self.assertEqual(self.refresh(str(refresh)).status_code, 401)
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + str(refresh.access_token))
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 401)

    def test_change_password_attempts_limited(self):
        self.auth(self.owner)
        url = '/api/staff/auth/password/'
        for _ in range(4):
            self.assertEqual(self.api.post(url, {'old_password': 'x', 'new_password': 'N3w-pass-777'}).status_code, 400)
        # Успех сбрасывает счётчик
        response = self.api.post(url, {'old_password': PASSWORD, 'new_password': 'N3w-pass-777'})
        self.assertEqual(response.status_code, 200)
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + response.data['access'])
        for _ in range(5):
            self.assertEqual(self.api.post(url, {'old_password': 'x', 'new_password': 'N3w-pass-888'}).status_code, 400)
        response = self.api.post(url, {'old_password': 'N3w-pass-777', 'new_password': 'N3w-pass-888'})
        self.assertEqual(response.status_code, 429)
        self.assertIn('15 минут', response.data['error'])
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('N3w-pass-777'))


class MeTests(StaffTestCase):
    def test_me_for_roles(self):
        self.auth(self.owner)
        data = self.api.get('/api/staff/me/').data
        self.assertEqual(data['role'], 'owner')
        self.assertEqual(data['company'], {'id': self.company.id, 'slug': 'a', 'name': 'Салон a',
                                           'penalty_percent': 50, 'late_cancel_hours': 3})
        self.assertIsNone(data['master_id'])
        self.assertEqual(data['name'], 'owner')

        self.auth(self.admin)
        self.assertEqual(self.api.get('/api/staff/me/').data['role'], 'admin')

        self.auth(self.master_user)
        data = self.api.get('/api/staff/me/').data
        self.assertEqual(data['role'], 'master')
        self.assertEqual(data['master_id'], self.master.id)
        self.assertEqual(data['name'], 'Анна a')

    def test_client_token_rejected(self):
        # Мастер вошёл через клиентский /api/auth/login/
        user = User.objects.create_user(username='+37369555222', password=PASSWORD)
        ClientProfile.objects.create(user=user, phone='+37369555222', name='Мария')
        self.master2.user = user
        self.master2.save()
        tokens = self.api.post('/api/auth/login/', {'phone': '+37369555222', 'password': PASSWORD}, format='json').data
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + tokens['access'])
        response = self.api.get('/api/staff/me/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {'error': 'Нужен вход в панель салона'})

    def test_no_token(self):
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 401)

    def test_deactivated_master_loses_access(self):
        self.auth(self.master_user)
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 200)
        self.master.is_active = False
        self.master.save()
        response = self.api.get('/api/staff/me/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {'error': 'У этого аккаунта нет доступа к панели салона'})

    def test_admin_with_master_profile_in_other_salon(self):
        self.master_b.user = self.admin
        self.master_b.save()
        self.auth(self.admin)
        data = self.api.get('/api/staff/me/').data
        self.assertEqual(data['company']['slug'], 'a')
        self.assertIsNone(data['master_id'])


class IsolationTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.owner)

    def test_foreign_objects_404(self):
        foreign_appointment = Appointment.objects.create(
            company=self.company_b, master=self.master_b, service=self.service_b, client_name='x',
            client_phone='+37369000002', date=self.day, start_time=time(10), end_time=time(11),
        )
        day_off = MasterDayOff.objects.create(master=self.master_b, date=self.day)
        for url in [
            f'/api/staff/appointments/{foreign_appointment.id}/',
            f'/api/staff/services/{self.service_b.id}/',
            f'/api/staff/masters/{self.master_b.id}/',
            f'/api/staff/categories/{self.category_b.id}/',
        ]:
            self.assertEqual(self.api.get(url).status_code, 404, url)
            self.assertEqual(self.api.patch(url, {'name': 'x'}, format='json').status_code, 404, url)
            if 'appointments' not in url:
                self.assertEqual(self.api.delete(url).status_code, 404, url)
        schedule_url = f'/api/staff/masters/{self.master_b.id}/schedule/'
        self.assertEqual(self.api.get(schedule_url).status_code, 404)
        self.assertEqual(self.api.put(schedule_url, [], format='json').status_code, 404)
        access_url = f'/api/staff/masters/{self.master_b.id}/access/'
        self.assertEqual(self.api.post(access_url, {'username': 'x1', 'password': 'Xx-12345678'}).status_code, 404)
        self.assertEqual(self.api.delete(f'/api/staff/masters/{self.master_b.id}/days-off/{self.day}/').status_code, 404)
        self.assertEqual(self.api.post(f'/api/staff/masters/{self.master_b.id}/days-off/', {'date': self.day}).status_code, 404)
        self.assertTrue(MasterDayOff.objects.filter(pk=day_off.pk).exists())
        days_off = self.api.get('/api/staff/days-off/', {'date_from': self.today, 'date_to': self.day + timedelta(days=1)})
        self.assertEqual(days_off.data, [])

    def test_create_with_foreign_master_or_service(self):
        response = self.api.post('/api/staff/appointments/', self.create_payload(master=self.master_b.id), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('master', response.data)
        response = self.api.post('/api/staff/appointments/', self.create_payload(service=self.service_b.id), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('service', response.data)

    def test_master_services_from_other_salon(self):
        response = self.api.patch(f'/api/staff/masters/{self.master.id}/', {'services': [self.service_b.id]}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('services', response.data)

    def test_service_category_from_other_salon(self):
        response = self.api.patch(f'/api/staff/services/{self.service.id}/', {'category': self.category_b.id}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_master_patch_ignores_user_and_company(self):
        response = self.api.patch(
            f'/api/staff/masters/{self.master.id}/',
            {'user': self.owner.id, 'company': self.company_b.id, 'name': 'Анна Н.'}, format='json'
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.master.refresh_from_db()
        self.assertEqual(self.master.user, self.master_user)
        self.assertEqual(self.master.company, self.company)
        self.assertEqual(self.master.name, 'Анна Н.')

    def test_created_objects_belong_to_staff_company(self):
        response = self.api.post('/api/staff/services/', {
            'name': 'Педикюр', 'duration': 90, 'price': '350.50', 'company': self.company_b.id
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Service.objects.get(pk=response.data['id']).company, self.company)
        self.assertEqual(response.data['price'], '350.50')


class MasterRoleTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.own = self.appointment()
        self.other = self.appointment(master=self.master2)
        self.auth(self.master_user)

    def test_sees_only_own_appointments(self):
        response = self.api.get('/api/staff/appointments/', {'date_from': self.day, 'master': self.master2.id})
        self.assertEqual([a['id'] for a in response.data], [self.own.id])
        self.assertEqual(self.api.get(f'/api/staff/appointments/{self.other.id}/').status_code, 404)
        self.assertEqual(self.api.patch(f'/api/staff/appointments/{self.other.id}/', {'status': 'cancelled'}).status_code, 404)

    def test_masters_list_only_self_without_user(self):
        data = self.api.get('/api/staff/masters/').data
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['id'], self.master.id)
        self.assertNotIn('user', data[0])
        self.assertTrue(data[0]['has_access'])
        self.assertEqual(self.api.get(f'/api/staff/masters/{self.master2.id}/schedule/').status_code, 404)
        self.assertEqual(self.api.get(f'/api/staff/masters/{self.master.id}/schedule/').status_code, 200)

    def test_cannot_edit_directories(self):
        response = self.api.patch(f'/api/staff/services/{self.service.id}/', {'name': 'x'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {'error': 'Недостаточно прав'})
        self.assertEqual(self.api.get('/api/staff/services/').status_code, 200)
        self.assertEqual(self.api.patch('/api/staff/company/', {'name': 'x'}).status_code, 403)
        self.assertEqual(self.api.put(f'/api/staff/masters/{self.master.id}/schedule/', [], format='json').status_code, 403)

    def test_patch_own_appointment(self):
        url = f'/api/staff/appointments/{self.own.id}/'
        response = self.api.patch(url, {'status': 'cancelled', 'notes': 'перезвонить'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'cancelled')
        self.assertEqual(self.api.patch(url, {'client_name': 'Другая'}, format='json').status_code, 403)
        # То же значение, что и сейчас, — не изменение
        response = self.api.patch(url, {'client_name': 'Ирина', 'master': self.master.id, 'status': 'confirmed'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def test_create_goes_to_self_confirmed(self):
        response = self.api.post('/api/staff/appointments/', self.create_payload(
            master=self.master2.id, status='pending', start_time='12:00'
        ), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['master']['id'], self.master.id)
        self.assertEqual(response.data['status'], 'confirmed')

    def test_create_yesterday_rejected(self):
        response = self.api.post('/api/staff/appointments/', self.create_payload(
            date=(self.today - timedelta(days=1)).isoformat()
        ), format='json')
        self.assertEqual(response.status_code, 400)

    def test_free_times_only_self(self):
        params = {'master': self.master2.id, 'date': self.day, 'duration': 60}
        self.assertEqual(self.api.get('/api/staff/free-times/', params).status_code, 404)
        params['master'] = self.master.id
        self.assertEqual(self.api.get('/api/staff/free-times/', params).status_code, 200)


class StaffCreateAppointmentTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.admin)

    def post(self, **overrides):
        return self.api.post('/api/staff/appointments/', self.create_payload(**overrides), format='json')

    def test_create_success(self):
        client_user = User.objects.create_user(username='+37369123456', password=PASSWORD)
        ClientProfile.objects.create(user=client_user, phone='+37369123456', name='Ирина')
        response = self.post(company=self.company_b.id, created_by_staff=False, end_time='23:00')
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual(data['start_time'], '10:00')
        self.assertEqual(data['end_time'], '11:00')
        self.assertEqual(data['duration'], 60)
        self.assertEqual(data['source'], 'staff')
        self.assertEqual(data['status'], 'confirmed')
        self.assertEqual(data['client_phone'], '+37369123456')
        self.assertEqual(data['service'], {'id': self.service.id, 'name': 'Маникюр', 'price': '200.00', 'is_active': True})
        appointment = Appointment.objects.get(pk=data['id'])
        self.assertEqual(appointment.client, client_user)
        self.assertEqual(appointment.company, self.company)
        self.assertTrue(appointment.created_by_staff)

    def test_custom_duration_and_status(self):
        response = self.post(duration=15, status='pending')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['end_time'], '10:15')
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(self.post(duration=601).status_code, 400)
        self.assertEqual(self.post(duration=4).status_code, 400)

    def test_overlap(self):
        self.appointment(start=time(11), end=time(12, 30), client_name='Мария')
        response = self.post(start_time='12:00')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Пересекается с записью 11:00–12:30 (Мария)'})
        # Касание концами допустимо
        self.assertEqual(self.post(start_time='10:00').status_code, 201)
        self.assertEqual(self.post(start_time='12:30').status_code, 201)
        # Отменённая запись не мешает
        self.assertEqual(self.post(start_time='11:00', status='cancelled').status_code, 201)

    def test_midnight(self):
        response = self.post(start_time='23:30')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Запись должна закончиться до полуночи'})

    def test_master_without_service(self):
        other = Service.objects.create(company=self.company, name='Педикюр', duration=60, price=300)
        response = self.post(service=other.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('service', response.data)

    def test_inactive_service_or_master(self):
        self.service.is_active = False
        self.service.save()
        self.assertEqual(self.post().status_code, 400)
        self.service.is_active = True
        self.service.save()
        self.master.is_active = False
        self.master.save()
        self.assertEqual(self.post().status_code, 400)

    def test_visit_status_in_future(self):
        response = self.post(status='completed', date=(self.today + timedelta(days=1)).isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Отметить визит можно только в день записи или позже'})
        self.assertEqual(self.post(status='no_show', date=self.today.isoformat()).status_code, 201)

    def test_bad_phone_and_time_format(self):
        response = self.post(client_phone='123')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'client_phone': ['Неверный номер телефона']})
        self.assertEqual(self.post(start_time='10:00:00').status_code, 400)

    def test_far_date_before_midnight(self):
        payload = self.create_payload(date='9999-12-31', start_time='23:30')
        response = self.api.post('/api/staff/appointments/', payload, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Запись должна закончиться до полуночи'})
        response = self.api.post('/api/staff/appointments/', {**payload, 'start_time': '22:00'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['end_time'], '23:00')
        response = self.api.patch(f'/api/staff/appointments/{response.data["id"]}/', {'start_time': '23:15'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Запись должна закончиться до полуночи'})

    def test_staff_can_ignore_schedule_and_past(self):
        self.assertEqual(self.post(start_time='20:00').status_code, 201)
        self.assertEqual(self.post(date=(self.today - timedelta(days=3)).isoformat(), status='completed').status_code, 201)


class StaffPatchAppointmentTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.admin)

    def patch(self, appointment, **data):
        return self.api.patch(f'/api/staff/appointments/{appointment.id}/', data, format='json')

    def test_move_into_busy_slot(self):
        self.appointment(start=time(12), end=time(13), client_name='Мария')
        moving = self.appointment(start=time(10), end=time(11))
        response = self.patch(moving, start_time='12:30')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Пересекается с записью 12:00–13:00 (Мария)')
        # Перенос «на себя» (пересечение только с собой) — можно
        self.assertEqual(self.patch(moving, start_time='10:30').status_code, 200)

    def test_restore_cancelled_into_busy_slot(self):
        cancelled = self.appointment(status='cancelled')
        self.appointment(start=time(10, 30), end=time(11, 30))
        self.assertEqual(self.patch(cancelled, status='confirmed').status_code, 400)
        self.auth(self.master_user)
        self.assertEqual(self.patch(cancelled, status='confirmed').status_code, 400)
        self.assertEqual(self.patch(cancelled, notes='ок').status_code, 200)

    def test_move_with_hidden_service_and_without_service(self):
        hidden = self.appointment(start=time(10), end=time(10, 45))
        self.service.is_active = False
        self.service.save()
        response = self.patch(hidden, start_time='14:00')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['end_time'], '14:45')

        no_service = self.appointment(start=time(15), end=time(15, 20), service=None)
        response = self.patch(no_service, start_time='16:00', master=self.master2.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['end_time'], '16:20')
        self.assertEqual(response.data['duration'], 20)

    def test_null_master_rejected(self):
        appointment = self.appointment()
        self.assertEqual(self.patch(appointment, master=None).status_code, 400)
        self.assertEqual(self.patch(appointment, service=None).status_code, 400)

    def test_appointment_without_master(self):
        appointment = self.appointment(master=None)
        appointment.master = None
        appointment.save()
        self.assertEqual(self.patch(appointment, status='cancelled', client_name='Ира').status_code, 200)
        response = self.patch(appointment, start_time='12:00')
        self.assertEqual(response.data, {'error': 'Укажите мастера'})
        self.assertEqual(self.patch(appointment, start_time='12:00', master=self.master.id).status_code, 200)

    def test_change_service_recalculates_end(self):
        quick = Service.objects.create(company=self.company, name='Покрытие', duration=30, price=100)
        appointment = self.appointment()
        self.assertEqual(self.patch(appointment, service=quick.id).status_code, 400)  # мастер не делает
        self.master.services.add(quick)
        response = self.patch(appointment, service=quick.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['end_time'], '10:30')
        response = self.patch(appointment, duration=90)
        self.assertEqual(response.data['end_time'], '11:30')
        self.assertEqual(self.patch(appointment, start_time='23:00').status_code, 400)

    def test_change_phone_relinks_client(self):
        old_user = User.objects.create_user(username='+37369000001', password=PASSWORD)
        ClientProfile.objects.create(user=old_user, phone='+37369000001', name='Ирина')
        appointment = self.appointment(client=old_user)

        client = APIClient()
        tokens = client.post('/api/auth/login/', {'phone': '+37369000001', 'password': PASSWORD}, format='json').data
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + tokens['access'])
        self.assertEqual(len(client.get('/api/appointments/my/').data), 1)

        response = self.patch(appointment, client_phone='069777888')
        self.assertEqual(response.status_code, 200, response.data)
        appointment.refresh_from_db()
        self.assertEqual(appointment.client_phone, '+37369777888')
        self.assertIsNone(appointment.client)
        self.assertEqual(client.get('/api/appointments/my/').data, [])

    def stale_patch(self, stale, **data):
        serializer = StaffAppointmentWriteSerializer(
            stale, data=data, partial=True, context={'staff': get_staff(self.admin)}
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def test_stale_patch_does_not_resurrect_cancelled(self):
        x = self.appointment(client_name='X')
        stale = Appointment.objects.get(pk=x.pk)  # загружена до параллельных изменений
        # Параллельно: X отменили, на освободившееся время записали Y
        self.assertEqual(self.patch(x, status='cancelled').status_code, 200)
        y = self.api.post('/api/staff/appointments/', self.create_payload(client_name='Y'), format='json')
        self.assertEqual(y.status_code, 201, y.data)

        self.stale_patch(stale, notes='hi')
        x.refresh_from_db()
        self.assertEqual((x.status, x.notes), ('cancelled', 'hi'))
        busy = Appointment.objects.filter(master=self.master, date=self.day, status__in=['pending', 'confirmed'])
        self.assertEqual(busy.count(), 1)

        # Вернуть устаревшую копию в работу нельзя: проверка пересечения по свежей строке
        stale = Appointment.objects.get(pk=x.pk)
        stale.status = 'confirmed'
        stale.notes = 'старая заметка'
        with self.assertRaises(StaffError) as error:
            self.stale_patch(stale, status='confirmed')
        self.assertIn('Пересекается', str(error.exception.detail['error']))

    def test_stale_patch_does_not_revert_move(self):
        x = self.appointment()
        stale = Appointment.objects.get(pk=x.pk)
        self.assertEqual(self.patch(x, start_time='14:00').status_code, 200)
        self.stale_patch(stale, status='pending')
        x.refresh_from_db()
        self.assertEqual((x.start_time, x.end_time, x.status), (time(14), time(15), 'pending'))

    def test_mark_visit(self):
        today_appointment = self.appointment(day=self.today)
        self.assertEqual(self.patch(today_appointment, status='completed').status_code, 200)
        future = self.appointment(start=time(14), end=time(15))
        self.assertEqual(self.patch(future, status='no_show').status_code, 400)


class JournalTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.owner)

    def test_date_range_validation(self):
        url = '/api/staff/appointments/'
        self.assertEqual(self.api.get(url).status_code, 400)
        self.assertEqual(self.api.get(url, {'date_from': self.day, 'date_to': self.day - timedelta(days=1)}).status_code, 400)
        self.assertEqual(self.api.get(url, {'date_from': self.day, 'date_to': self.day + timedelta(days=31)}).status_code, 400)
        self.assertEqual(self.api.get(url, {'date_from': self.day, 'date_to': self.day + timedelta(days=30)}).status_code, 200)
        self.assertEqual(self.api.get(url, {'date_from': self.day, 'master': 'x'}).status_code, 400)
        self.assertEqual(self.api.get(url, {'date_from': self.day, 'status': 'pending,bad'}).status_code, 400)

    def test_includes_hidden_master_and_without_master(self):
        hidden = Master.objects.create(company=self.company, name='Уволена', is_active=False)
        a1 = self.appointment(master=hidden, start=time(12), end=time(12, 15))
        a2 = self.appointment(start=time(9), end=time(10), status='cancelled')
        a3 = self.appointment(start=time(10), end=time(11))
        a3.master = None
        a3.save()
        Appointment.objects.create(
            company=self.company_b, master=self.master_b, service=self.service_b, client_name='x',
            client_phone='x', date=self.day, start_time=time(10), end_time=time(11),
        )
        data = self.api.get('/api/staff/appointments/', {'date_from': self.day}).data
        self.assertEqual([a['id'] for a in data], [a2.id, a3.id, a1.id])
        self.assertEqual(data[2]['master'], {'id': hidden.id, 'name': 'Уволена', 'is_active': False})
        self.assertIsNone(data[1]['master'])
        self.assertEqual((data[2]['start_time'], data[2]['end_time']), ('12:00', '12:15'))
        self.assertEqual(data[0]['source'], 'online')

        filtered = self.api.get('/api/staff/appointments/', {'date_from': self.day, 'status': 'confirmed', 'master': hidden.id}).data
        self.assertEqual([a['id'] for a in filtered], [a1.id])

    def test_pending_count(self):
        self.appointment(status='pending')
        self.appointment(status='pending', master=self.master2)
        self.appointment(status='pending', day=self.today - timedelta(days=1))
        self.appointment(status='confirmed', start=time(14), end=time(15))
        self.assertEqual(self.api.get('/api/staff/appointments/pending-count/').data, {'count': 2})
        self.auth(self.master_user)
        self.assertEqual(self.api.get('/api/staff/appointments/pending-count/').data, {'count': 1})

    def test_pending_list_matches_count(self):
        far = self.appointment(status='pending', day=self.today + timedelta(days=45), client_name='Далеко')
        later = self.appointment(status='pending', start=time(12), end=time(13))
        earlier = self.appointment(status='pending', master=self.master2)
        today = self.appointment(status='pending', day=self.today, start=time(8), end=time(9))
        self.appointment(status='pending', day=self.today - timedelta(days=1))
        self.appointment(status='confirmed', start=time(14), end=time(15))
        Appointment.objects.create(
            company=self.company_b, master=self.master_b, service=self.service_b, client_name='x',
            client_phone='x', date=self.day, start_time=time(10), end_time=time(11), status='pending',
        )
        data = self.api.get('/api/staff/appointments/pending/').data
        self.assertEqual([a['id'] for a in data], [today.id, earlier.id, later.id, far.id])
        self.assertEqual(data[3]['client_name'], 'Далеко')
        self.assertEqual(data[3]['master'], {'id': self.master.id, 'name': self.master.name, 'is_active': True})
        count = self.api.get('/api/staff/appointments/pending-count/').data['count']
        self.assertEqual(count, len(data))

        self.auth(self.master_user)
        data = self.api.get('/api/staff/appointments/pending/').data
        self.assertEqual([a['id'] for a in data], [today.id, later.id, far.id])
        self.assertEqual(self.api.get('/api/staff/appointments/pending-count/').data['count'], len(data))


class BusyTimeTests(StaffTestCase):
    def public_slots(self, master=None):
        response = self.api.get('/api/companies/a/slots/', {
            'master': (master or self.master).id, 'service': self.service.id, 'date': self.day.isoformat()
        })
        return response.data['slots']

    def test_completed_busy_no_show_free(self):
        appointment = self.appointment(status='completed')
        self.assertNotIn('10:00', self.public_slots())
        appointment.status = 'no_show'
        appointment.save()
        self.assertIn('10:00', self.public_slots())

    def test_day_off_empties_public_slots(self):
        self.assertTrue(self.public_slots())
        MasterDayOff.objects.create(master=self.master, date=self.day)
        self.assertEqual(self.public_slots(), [])

    def test_free_times(self):
        self.auth(self.admin)
        appointment = self.appointment(start=time(10), end=time(11))
        params = {'master': self.master.id, 'date': self.day.isoformat(), 'service': self.service.id}
        data = self.api.get('/api/staff/free-times/', params).data
        self.assertEqual(data['times'][:5], ['09:00', '11:00', '11:15', '11:30', '11:45'])
        self.assertTrue(data['working'])
        self.assertFalse(data['day_off'])
        self.assertEqual(data['schedule'], {'start_time': '09:00', 'end_time': '18:00', 'break_start': None, 'break_end': None})

        data = self.api.get('/api/staff/free-times/', {**params, 'exclude': appointment.id}).data
        self.assertEqual(data['times'][:4], ['09:00', '09:15', '09:30', '09:45'])
        self.assertEqual(data['times'][-1], '17:00')

        data = self.api.get('/api/staff/free-times/', {**params, 'duration': 15}).data
        self.assertEqual(data['times'][-1], '17:45')

        MasterDayOff.objects.create(master=self.master, date=self.day)
        data = self.api.get('/api/staff/free-times/', params).data
        self.assertEqual((data['times'], data['working'], data['day_off']), ([], False, True))

        self.assertEqual(self.api.get('/api/staff/free-times/', {'master': self.master.id, 'date': self.day}).status_code, 400)
        self.assertEqual(self.api.get('/api/staff/free-times/', {**params, 'master': self.master_b.id}).status_code, 404)


class ClientLookupTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.admin)

    def lookup(self, phone):
        return self.api.get('/api/staff/clients/lookup/', {'phone': phone})

    def test_lookup_by_appointments(self):
        self.appointment(day=self.today - timedelta(days=10), status='completed', client_name='Ира')
        self.appointment(day=self.today - timedelta(days=5), status='completed', client_name='Ирина')
        self.appointment(day=self.today - timedelta(days=3), status='no_show', client_name='Ирина')
        Appointment.objects.create(
            company=self.company_b, master=self.master_b, service=self.service_b, client_name='Чужая',
            client_phone='+37369000001', date=self.today - timedelta(days=1), start_time=time(10),
            end_time=time(11), status='completed',
        )
        response = self.lookup('069000001')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            'phone': '+37369000001', 'name': 'Ирина', 'visits': 2, 'no_shows': 1,
            'last_visit': (self.today - timedelta(days=5)).isoformat(), 'blacklist': None, 'penalties': [],
        })

    def test_lookup_profile_and_not_found(self):
        user = User.objects.create_user(username='+37369000005', password=PASSWORD)
        ClientProfile.objects.create(user=user, phone='+37369000005', name='Ольга', surname='Ц.')
        # Клиент есть на платформе, но в этот салон не записывался — ничего не раскрываем
        self.assertEqual(self.lookup('+37369000005').status_code, 404)
        # Запись этого клиента (по аккаунту, телефон в записи другой) — в этом салоне
        self.appointment(client=user, client_phone='+37369000099', client_name='Оля')
        self.assertEqual(self.lookup('+37369000005').data['name'], 'Ольга Ц.')
        self.assertEqual(self.lookup('069000006').status_code, 404)
        self.assertEqual(self.lookup('12').status_code, 400)

    def test_cross_salon_client_not_found(self):
        user = User.objects.create_user(username='+37369000007', password=PASSWORD)
        ClientProfile.objects.create(user=user, phone='+37369000007', name='Секрет', surname='Клиентка')
        Appointment.objects.create(
            company=self.company_b, master=self.master_b, service=self.service_b, client_name='Секрет',
            client_phone='+37369000007', client=user, date=self.today, start_time=time(10),
            end_time=time(11), status='completed',
        )
        response = self.lookup('069000007')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, {'error': 'Клиент не найден'})
        self.auth(self.master_user)
        self.assertEqual(self.lookup('069000007').status_code, 404)

    def test_lookup_rate_limited(self):
        for _ in range(120):
            self.assertEqual(self.lookup('069000006').status_code, 404)
        response = self.lookup('069000006')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data, {'error': 'Слишком много запросов, попробуйте позже'})
        # Счётчик — на пользователя панели
        self.auth(self.owner)
        self.assertEqual(self.lookup('069000006').status_code, 404)


class DirectoryTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.admin)

    def test_service_soft_delete(self):
        self.assertEqual(self.api.delete(f'/api/staff/services/{self.service.id}/').status_code, 204)
        self.service.refresh_from_db()
        self.assertFalse(self.service.is_active)
        data = self.api.get('/api/staff/services/').data
        self.assertEqual(data[0]['is_active'], False)
        self.assertEqual(data[0]['category_name'], 'Руки')
        self.assertEqual(self.api.get('/api/companies/a/services/').data, [])

    def test_categories(self):
        response = self.api.post('/api/staff/categories/', {'name': 'Ноги'}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.api.patch(f'/api/staff/categories/{self.category.id}/', {'name': 'Руки+'}).data['name'], 'Руки+')
        self.assertEqual(self.api.delete(f'/api/staff/categories/{self.category.id}/').status_code, 204)
        self.service.refresh_from_db()
        self.assertIsNone(self.service.category)
        self.assertIsNone(self.api.get(f'/api/staff/services/{self.service.id}/').data['category_name'])

    def test_service_validation(self):
        url = f'/api/staff/services/{self.service.id}/'
        self.assertEqual(self.api.patch(url, {'duration': 4}).status_code, 400)
        self.assertEqual(self.api.patch(url, {'price': '-1'}).status_code, 400)

    def test_master_crud_and_soft_delete(self):
        response = self.api.post('/api/staff/masters/', {'name': 'Ольга', 'services': [self.service.id]}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['services'], [self.service.id])
        self.assertEqual(len(response.data['schedule']), 7)
        self.assertIsNone(response.data['user'])
        self.assertFalse(response.data['has_access'])
        self.assertEqual(Master.objects.get(pk=response.data['id']).company, self.company)

        data = self.api.get('/api/staff/masters/').data
        self.assertEqual(data[0]['user'], 'anna')

        self.appointment(master=self.master2, status='pending')
        url = f'/api/staff/masters/{self.master2.id}/'
        response = self.api.delete(url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(self.api.delete(url + '?force=1').status_code, 204)
        self.master2.refresh_from_db()
        self.assertFalse(self.master2.is_active)

    def test_master_services_keep_hidden(self):
        hidden = Service.objects.create(company=self.company, name='Архив', duration=30, price=1, is_active=False)
        other = Service.objects.create(company=self.company, name='Педикюр', duration=60, price=300)
        self.master.services.add(hidden)
        response = self.api.patch(f'/api/staff/masters/{self.master.id}/', {'services': [other.id]}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['services'], sorted([hidden.id, other.id]))

    def test_schedule_put(self):
        url = f'/api/staff/masters/{self.master.id}/schedule/'
        week = [{'day_of_week': d, 'is_working': False} for d in range(7)]
        week[self.day.weekday()] = {'day_of_week': self.day.weekday(), 'is_working': True, 'start_time': '12:00', 'end_time': '14:00'}
        self.assertEqual(self.api.put(url, week[:6], format='json').status_code, 400)
        bad = [dict(item) for item in week]
        bad[self.day.weekday()]['start_time'] = '15:00'
        self.assertEqual(self.api.put(url, bad, format='json').status_code, 400)
        duplicate = [dict(item) for item in week]
        duplicate[0]['day_of_week'] = 1 if self.day.weekday() != 1 else 2
        self.assertEqual(self.api.put(url, duplicate, format='json').status_code, 400)

        # Нерабочий день: время после подстановки 09:00/18:00 тоже должно идти по порядку
        for day0 in ({'start_time': '19:00'}, {'start_time': '18:00', 'end_time': '09:00'}, {'end_time': '08:00'}):
            inverted = [dict(item) for item in week]
            inverted[0] = {'day_of_week': 0, 'is_working': False, **day0} if self.day.weekday() != 0 else inverted[0]
            inverted[6] = {'day_of_week': 6, 'is_working': False, **day0} if self.day.weekday() != 6 else inverted[6]
            response = self.api.put(url, inverted, format='json')
            self.assertEqual(response.status_code, 400, day0)
            self.assertIn('время начала должно быть раньше окончания', response.data['error'])
            self.assertRegex(response.data['error'], '^(Понедельник|Воскресенье): ')
        self.assertFalse(MasterSchedule.objects.filter(master=self.master, start_time__gte=time(18)).exists())

        response = self.api.put(url, week, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data[self.day.weekday()], {
            'day_of_week': self.day.weekday(), 'is_working': True, 'start_time': '12:00', 'end_time': '14:00',
            'break_start': None, 'break_end': None,
        })
        other_day = (self.day.weekday() + 1) % 7
        self.assertEqual(response.data[other_day]['is_working'], False)
        slots = self.api.get('/api/companies/a/slots/', {
            'master': self.master.id, 'service': self.service.id, 'date': self.day.isoformat()
        }).data['slots']
        self.assertEqual(slots, ['12:00', '12:30', '13:00'])

    def test_days_off(self):
        self.appointment(status='pending')
        url = f'/api/staff/masters/{self.master.id}/days-off/'
        response = self.api.post(url, {'date': self.day.isoformat()}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['appointments_count'], 1)
        self.assertEqual(self.api.post(url, {'date': self.day.isoformat()}, format='json').status_code, 200)
        self.assertEqual(MasterDayOff.objects.count(), 1)

        listed = self.api.get('/api/staff/days-off/', {'date_from': self.today, 'date_to': self.day}).data
        self.assertEqual(listed, [{'id': response.data['id'], 'master': self.master.id, 'date': self.day.isoformat()}])

        self.assertEqual(self.api.delete(f'{url}{self.day.isoformat()}/').status_code, 204)
        self.assertFalse(MasterDayOff.objects.exists())

    def test_access(self):
        url = f'/api/staff/masters/{self.master2.id}/access/'
        response = self.api.post(url, {'username': 'maria', 'password': 'Mar1a-secret-9'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {'username': 'maria', 'has_access': True})
        self.master2.refresh_from_db()
        self.assertTrue(self.master2.user.check_password('Mar1a-secret-9'))

        # Логин мастера-входа работает
        api = APIClient()
        login = api.post('/api/staff/auth/login/', {'username': 'maria', 'password': 'Mar1a-secret-9'}, format='json')
        self.assertEqual(login.data['me']['master_id'], self.master2.id)

        # Смена пароля и переименование
        response = self.api.post(url, {'username': 'maria2', 'password': 'An0ther-secret-9'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.master2.user.refresh_from_db()
        self.assertEqual(self.master2.user.username, 'maria2')

        self.assertEqual(self.api.delete(url).status_code, 204)
        self.master2.refresh_from_db()
        self.assertIsNone(self.master2.user)
        self.assertTrue(User.objects.filter(username='maria2').exists())

    def test_access_username_taken(self):
        client_user = User.objects.create_user(username='+37369111222', password=PASSWORD)
        ClientProfile.objects.create(user=client_user, phone='+37369111222', name='Клиент')
        url = f'/api/staff/masters/{self.master2.id}/access/'
        for username in ('owner', 'OWNER'):
            response = self.api.post(url, {'username': username, 'password': 'Mar1a-secret-9'}, format='json')
            self.assertEqual(response.status_code, 400, username)
            self.assertEqual(response.data, {'username': ['Этот логин уже занят']})
        # Логин-телефон (занятый или свободный) не принимается
        for username in ('+37369111222', '+37369555555', '069555555', '69555555', '+anna'):
            response = self.api.post(url, {'username': username, 'password': 'Mar1a-secret-9'}, format='json')
            self.assertEqual(response.status_code, 400, username)
            self.assertEqual(response.data, {'username': ['Логин не должен быть номером телефона']})
        self.master2.refresh_from_db()
        self.assertIsNone(self.master2.user)
        response = self.api.post(url, {'username': 'maria', 'password': '123'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.data)
        self.assertFalse(User.objects.filter(username='maria').exists())

    def test_phone_username_rejected_on_rename(self):
        url = f'/api/staff/masters/{self.master2.id}/access/'
        self.assertEqual(self.api.post(url, {'username': 'maria', 'password': 'Mar1a-secret-9'}, format='json').status_code, 200)
        response = self.api.post(url, {'username': '069555555', 'password': 'Mar1a-secret-9'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'username': ['Логин не должен быть номером телефона']})
        self.master2.refresh_from_db()
        self.assertEqual(self.master2.user.username, 'maria')

    def test_client_account_password_cannot_be_changed(self):
        client_user = User.objects.create_user(username='+37369123456', password=PASSWORD)
        ClientProfile.objects.create(user=client_user, phone='+37369123456', name='Клиент')
        self.master2.user = client_user
        self.master2.save()
        url = f'/api/staff/masters/{self.master2.id}/access/'
        for username in ('+37369123456', 'maria'):
            response = self.api.post(url, {'username': username, 'password': 'Attacker-pass-999'}, format='json')
            self.assertEqual(response.status_code, 403, username)
            self.assertEqual(response.data, {'error': 'Это клиентский аккаунт — пароль меняет сам пользователь'})
        client_user.refresh_from_db()
        self.assertEqual(client_user.username, '+37369123456')
        self.assertTrue(client_user.check_password(PASSWORD))
        # Отвязать можно
        self.assertEqual(self.api.delete(url).status_code, 204)
        self.master2.refresh_from_db()
        self.assertIsNone(self.master2.user)

    def test_register_with_taken_username(self):
        User.objects.create_user(username='+37369555555', password=PASSWORD)
        response = APIClient().post(
            '/api/auth/register/', {'phone': '+37369555555', 'password': 'Cl1ent-pass-1', 'name': 'Ира'}, format='json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {'error': 'Этот номер уже занят'})

    def test_admin_cannot_take_over_owner_account(self):
        self.master2.user = self.owner
        self.master2.save()
        url = f'/api/staff/masters/{self.master2.id}/access/'
        response = self.api.post(url, {'username': 'owner', 'password': 'Hack3d-pass-1'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(PASSWORD))


class CompanyTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.owner)

    def test_cancel_hours(self):
        url = '/api/staff/company/'
        self.assertEqual(self.api.get(url).data['cancel_hours'], 0)
        self.assertEqual(self.api.patch(url, {'cancel_hours': 200}, format='json').status_code, 400)
        self.assertEqual(self.api.patch(url, {'cancel_hours': 24}, format='json').data['cancel_hours'], 24)
        self.auth(self.master_user)
        self.assertEqual(self.api.patch(url, {'cancel_hours': 1}, format='json').status_code, 403)

    def test_coordinates(self):
        url = '/api/staff/company/'
        data = self.api.get(url).data
        self.assertIsNone(data['latitude'])
        self.assertEqual(self.api.patch(url, {'latitude': 121}, format='json').status_code, 400)
        response = self.api.patch(url, {'latitude': 47.0447862, 'longitude': '28.8', 'slug': 'hack', 'id': 999}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['latitude'], 47.044786)
        self.assertEqual(response.data['longitude'], 28.8)
        self.company.refresh_from_db()
        self.assertEqual(str(self.company.latitude), '47.044786')
        self.assertEqual(self.company.slug, 'a')
        response = self.api.patch(url, {'longitude': ''}, format='json')
        self.assertIsNone(response.data['longitude'])

    def test_master_can_read(self):
        self.auth(self.master_user)
        self.assertEqual(self.api.get('/api/staff/company/').data['slug'], 'a')


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class CreateStaffCommandTests(TestCase):
    def setUp(self):
        self.company, _, _, self.master = make_salon('jcnails')

    def run_command(self, *args):
        out = StringIO()
        call_command('create_staff', *args, stdout=out)
        return out.getvalue()

    def test_create_owner_and_relink(self):
        self.run_command('--company', 'jcnails', '--username', 'boss', '--role', 'owner', '--password', PASSWORD)
        user = User.objects.get(username='boss')
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual((user.admin_profile.company, user.admin_profile.role), (self.company, 'owner'))

        # Существующий пользователь: пароль меняется только с --reset-password
        out = self.run_command('--company', 'jcnails', '--username', 'boss', '--role', 'admin', '--password', 'Other-pass-77')
        self.assertIn('существующий', out)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(user.admin_profile.role, 'admin')
        self.run_command('--company', 'jcnails', '--username', 'boss', '--role', 'owner',
                         '--password', 'Other-pass-77', '--reset-password')
        user.refresh_from_db()
        self.assertTrue(user.check_password('Other-pass-77'))

        make_salon('other')
        with self.assertRaises(CommandError):
            self.run_command('--company', 'other', '--username', 'boss', '--role', 'owner')

    def test_master(self):
        with self.assertRaises(CommandError):
            self.run_command('--company', 'jcnails', '--username', 'anna', '--role', 'master', '--password', PASSWORD)
        self.run_command('--company', 'jcnails', '--username', 'anna', '--role', 'master',
                         '--master-id', str(self.master.id), '--password', PASSWORD)
        self.master.refresh_from_db()
        self.assertEqual(self.master.user.username, 'anna')
        # У мастера уже другой пользователь
        with self.assertRaises(CommandError):
            self.run_command('--company', 'jcnails', '--username', 'olga', '--role', 'master',
                             '--master-id', str(self.master.id), '--password', PASSWORD)
        self.assertFalse(User.objects.filter(username='olga').exists())  # откат транзакции
        # Мастер другого салона
        _, _, _, foreign_master = make_salon('other')
        with self.assertRaises(CommandError):
            self.run_command('--company', 'jcnails', '--username', 'olga', '--role', 'master',
                             '--master-id', str(foreign_master.id), '--password', PASSWORD)

    def test_weak_password(self):
        with self.assertRaises(CommandError):
            self.run_command('--company', 'jcnails', '--username', 'boss', '--role', 'owner', '--password', '123')
        self.assertFalse(User.objects.filter(username='boss').exists())


class MigrationsTests(TestCase):
    def test_no_missing_migrations(self):
        call_command('makemigrations', '--check', '--dry-run', stdout=StringIO())


class SeedDemoTests(TestCase):
    """Демо-салон: изолирован от настоящих салонов, пересоздаётся, в нём есть все виды записей."""

    def run_seed(self, **options):
        out = StringIO()
        call_command('seed_demo', password='Demo-pass-2026', stdout=out, **options)
        return out.getvalue()

    def test_builds_week_of_activity_without_touching_other_salons(self):
        real, *_ = make_salon('real')
        Appointment.objects.create(company=real, client_name='Настоящий клиент', client_phone='+37369000009',
                                   date=timezone.localdate(), start_time=time(10), end_time=time(11))
        self.assertIn('Демо-салон готов', self.run_seed())
        self.run_seed()  # повторный запуск пересоздаёт, а не дублирует
        demo = Company.objects.get(slug='demo')
        self.assertEqual(Company.objects.filter(slug='demo').count(), 1)
        self.assertEqual(Master.objects.filter(company=demo).count(), 4)
        statuses = set(Appointment.objects.filter(company=demo).values_list('status', flat=True))
        self.assertTrue({'pending', 'confirmed', 'completed', 'cancelled'} <= statuses)
        self.assertTrue(Appointment.objects.filter(company=demo, created_by_staff=True).exists())
        self.assertTrue(Appointment.objects.filter(company=demo, created_by_staff=False).exists())
        self.assertTrue(demo.payments.exists())
        self.assertTrue(demo.salary_payouts.exists())
        self.assertEqual(Appointment.objects.filter(company=real).count(), 1)

    def test_no_overlaps_and_inside_schedule(self):
        self.run_seed()
        demo = Company.objects.get(slug='demo')
        for master in Master.objects.filter(company=demo):
            schedule = {s.day_of_week: s for s in master.schedule.all()}
            by_day = {}
            for a in Appointment.objects.filter(master=master):
                day = schedule[a.date.weekday()]
                self.assertTrue(day.is_working, (master.name, a.date))
                self.assertGreaterEqual(a.start_time, day.start_time)
                self.assertLessEqual(a.end_time, day.end_time)
                by_day.setdefault(a.date, []).append((a.start_time, a.end_time))
            for slots in by_day.values():
                slots.sort()
                for (_, end), (start, _) in zip(slots, slots[1:]):
                    self.assertLessEqual(end, start)

    def test_logins_work_and_weak_password_rejected(self):
        self.run_seed()
        self.assertEqual(get_staff(User.objects.get(username='demo')).role, 'owner')
        self.assertEqual(get_staff(User.objects.get(username='demo_admin')).role, 'admin')
        self.assertEqual(get_staff(User.objects.get(username='demo_anna')).role, 'master')
        with self.assertRaises(CommandError):
            call_command('seed_demo', password='123', stdout=StringIO())
