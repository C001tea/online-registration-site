from datetime import time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import AdminProfile, Appointment, Company, Master, Service
from dashboard.tokens import get_staff_tokens

from .auth import issue_token
from .models import SalonNote, SuperAdminLog, SuperAdminTOTP

PASSWORD = 'Super-secret-2026!'


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', EMAIL_CONFIGURED=True)
class SalonsBase(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.boss = User.objects.create_superuser('boss', 'boss@example.com', PASSWORD)
        SuperAdminTOTP.objects.create(user=self.boss, secret='A' * 32, confirmed_at=timezone.now())
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {issue_token(self.boss)}')
        now = timezone.now()
        self.salon = Company.objects.create(name='Альфа', slug='alfa', phone='+37369000000',
                                            trial_ends_at=now + timedelta(days=2))
        self.pending = Company.objects.create(name='Бета', slug='beta', status=Company.STATUS_PENDING,
                                              trial_ends_at=now + timedelta(days=10))
        self.owner = User.objects.create_user('alfa@example.com', 'alfa@example.com', PASSWORD)
        AdminProfile.objects.create(user=self.owner, company=self.salon, role='owner')
        self.beta_owner = User.objects.create_user('beta@example.com', 'beta@example.com', PASSWORD)
        AdminProfile.objects.create(user=self.beta_owner, company=self.pending, role='owner')
        service = Service.objects.create(company=self.salon, name='Маникюр', duration=60, price=200)
        master = Master.objects.create(company=self.salon, name='Анна')
        master.services.add(service)
        today = timezone.localdate()
        Appointment.objects.create(company=self.salon, master=master, service=service, client_name='К',
                                   client_phone='+37369000001', date=today, start_time=time(10), end_time=time(11))
        Appointment.objects.create(company=self.salon, master=master, service=service, client_name='К',
                                   client_phone='+37369000001', date=today, start_time=time(12), end_time=time(13),
                                   created_by_staff=True)


class SalonsTests(SalonsBase):
    def test_overview(self):
        data = self.api.get('/api/super/overview/').data
        self.assertEqual(data['salons']['total'], 2)
        self.assertEqual(data['salons']['by_status']['pending'], 1)
        self.assertEqual(data['appointments']['today'], 2)
        self.assertEqual(data['appointments']['month_online'], 1)
        self.assertEqual(len(data['series']), 30)
        self.assertEqual(data['series'][-1]['appointments'], 2)
        self.assertEqual(data['series'][-1]['signups'], 2)
        self.assertEqual([r['slug'] for r in data['attention']['trial_ending']], ['alfa'])
        self.assertEqual([r['slug'] for r in data['attention']['unverified']], ['beta'])
        self.assertEqual([r['slug'] for r in data['attention']['inactive']], ['alfa'])  # никто не входил

    def test_list_search_and_filters(self):
        rows = self.api.get('/api/super/salons/').data
        alfa = next(r for r in rows if r['slug'] == 'alfa')
        self.assertEqual((alfa['masters'], alfa['services'], alfa['appointments']), (1, 1, 2))
        self.assertEqual(alfa['owner']['email'], 'alfa@example.com')
        self.assertEqual(alfa['trial'], 'ending')
        self.assertEqual([r['slug'] for r in self.api.get('/api/super/salons/?q=beta@').data], ['beta'])
        self.assertEqual([r['slug'] for r in self.api.get('/api/super/salons/?status=pending').data], ['beta'])
        self.assertEqual([r['slug'] for r in self.api.get('/api/super/salons/?trial=ending').data], ['alfa'])

    def test_staff_login_updates_activity(self):
        response = APIClient().post('/api/staff/auth/login/', {'username': 'alfa@example.com', 'password': PASSWORD},
                                    format='json')
        self.assertEqual(response.status_code, 200)
        alfa = self.api.get(f'/api/super/salons/{self.salon.id}/').data
        self.assertIsNotNone(alfa['last_activity_at'])
        self.assertEqual(alfa['staff'][0]['role'], 'owner')

    def test_block_and_unblock(self):
        url = f'/api/super/salons/{self.salon.id}/status/'
        self.assertEqual(self.api.post(url, {'status': 'blocked'}).status_code, 400)  # нужна причина
        self.assertEqual(self.api.post(url, {'status': 'blocked', 'reason': 'Спам'}).status_code, 200)
        self.salon.refresh_from_db()
        self.assertEqual(self.salon.status, 'blocked')
        # Сотрудники заблокированного салона не входят и теряют доступ с действующим токеном
        staff = APIClient()
        self.assertEqual(staff.post('/api/staff/auth/login/', {'username': 'alfa@example.com', 'password': PASSWORD},
                                    format='json').status_code, 403)
        staff.credentials(HTTP_AUTHORIZATION='Bearer ' + get_staff_tokens(self.owner)['access'])
        self.assertEqual(staff.get('/api/staff/me/').status_code, 403)
        # Сайт записи скрыт
        self.assertFalse(Company.public.filter(pk=self.salon.pk).exists())
        self.assertEqual(self.api.post(url, {'status': 'active'}).status_code, 200)
        self.assertEqual(staff.get('/api/staff/me/').status_code, 200)
        actions = list(SuperAdminLog.objects.filter(company=self.salon).values_list('action', flat=True))
        self.assertEqual(actions, ['salon_unblocked', 'salon_blocked'])

    def test_extend_trial(self):
        url = f'/api/super/salons/{self.salon.id}/trial/'
        before = self.salon.trial_ends_at
        self.assertEqual(self.api.post(url, {'days': 5}).status_code, 400)
        data = self.api.post(url, {'days': 14}).data
        self.salon.refresh_from_db()
        self.assertEqual(self.salon.trial_ends_at, before + timedelta(days=14))
        self.assertEqual(data['trial'], 'active')
        # Кончившийся пробный период продлевается от сегодня
        self.pending.trial_ends_at = timezone.now() - timedelta(days=30)
        self.pending.save()
        self.api.post(f'/api/super/salons/{self.pending.id}/trial/', {'days': 7})
        self.pending.refresh_from_db()
        self.assertGreater(self.pending.trial_ends_at, timezone.now() + timedelta(days=6))
        day = (timezone.localdate() + timedelta(days=60)).isoformat()
        self.assertEqual(self.api.post(url, {'until': day}).status_code, 200)
        self.assertEqual(self.api.post(url, {'until': '2020-01-01'}).status_code, 400)

    def test_emails_to_owner(self):
        self.assertEqual(self.api.post(f'/api/super/salons/{self.salon.id}/resend-verification/').status_code, 400)
        self.assertEqual(self.api.post(f'/api/super/salons/{self.pending.id}/resend-verification/').status_code, 200)
        self.assertEqual(self.api.post(f'/api/super/salons/{self.salon.id}/password-reset/').status_code, 200)
        self.assertEqual([m.to for m in mail.outbox], [['beta@example.com'], ['alfa@example.com']])
        self.assertIn('Поддержка TimePoint', mail.outbox[1].body)
        self.assertIn('reset-password?uid=', mail.outbox[1].body)

    def test_manual_verify(self):
        url = f'/api/super/salons/{self.pending.id}/verify/'
        self.assertEqual(self.api.post(url, {'reason': 'письмо не дошло'}).data['status'], 'active')
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, Company.STATUS_ACTIVE)
        entry = SuperAdminLog.objects.filter(action='salon_verified_manually').get()
        self.assertEqual(entry.details, {'reason': 'письмо не дошло'})
        # Повторно и для работающего салона — нельзя; заблокированный не открывается
        self.assertEqual(self.api.post(url).status_code, 400)
        self.assertEqual(self.api.post(f'/api/super/salons/{self.salon.id}/verify/').status_code, 400)
        self.salon.status = Company.STATUS_BLOCKED
        self.salon.save()
        self.assertEqual(self.api.post(f'/api/super/salons/{self.salon.id}/verify/').status_code, 400)
        self.salon.refresh_from_db()
        self.assertEqual(self.salon.status, Company.STATUS_BLOCKED)

    def test_notes(self):
        url = f'/api/super/salons/{self.salon.id}/note/'
        self.assertEqual(self.api.post(url, {'text': ' '}).status_code, 400)
        data = self.api.post(url, {'text': 'Звонил, просит помочь с графиком'}).data
        self.assertEqual(data['notes'][0]['text'], 'Звонил, просит помочь с графиком')
        self.assertEqual(SalonNote.objects.count(), 1)

    def test_requires_super_token(self):
        anon = APIClient()
        for url in ('/api/super/overview/', '/api/super/salons/', f'/api/super/salons/{self.salon.id}/'):
            self.assertEqual(anon.get(url).status_code, 401)
        anon.credentials(HTTP_AUTHORIZATION='Bearer ' + get_staff_tokens(self.owner)['access'])
        self.assertEqual(anon.post(f'/api/super/salons/{self.salon.id}/status/',
                                   {'status': 'blocked', 'reason': 'x'}).status_code, 401)

    def test_unknown_salon(self):
        self.assertEqual(self.api.get('/api/super/salons/99999/').status_code, 404)

    @mock.patch('superadmin.salons.send_password_reset', return_value=False)
    def test_mail_failure_reported(self, _send):
        response = self.api.post(f'/api/super/salons/{self.salon.id}/password-reset/')
        self.assertEqual(response.status_code, 400)
        self.assertIn('почту', response.data['error'])


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', EMAIL_CONFIGURED=True)
class ImpersonationTests(SalonsBase):
    """Вход в панель салона от имени владельца."""

    def impersonate(self, salon=None):
        return self.api.post(f'/api/super/salons/{(salon or self.salon).id}/impersonate/')

    def staff_client(self, token):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client

    def test_token_opens_owner_panel_with_banner(self):
        response = self.impersonate()
        self.assertEqual(response.status_code, 200, getattr(response, 'data', response.content))
        staff = self.staff_client(response.data['access'])
        me = staff.get('/api/staff/me/').data
        self.assertEqual(me['username'], 'alfa@example.com')
        self.assertEqual(me['impersonated_by'], 'boss')
        self.assertEqual(staff.get('/api/staff/masters/').status_code, 200)
        self.assertEqual(SuperAdminLog.objects.filter(action='impersonation_started', company=self.salon).count(), 1)
        # Обычный вход владельца — без отметки
        own = self.staff_client(get_staff_tokens(self.owner)['access'])
        self.assertNotIn('impersonated_by', own.get('/api/staff/me/').data)

    def test_sensitive_actions_forbidden(self):
        staff = self.staff_client(self.impersonate().data['access'])
        response = staff.post('/api/staff/auth/password/', {'old_password': PASSWORD, 'new_password': 'X-new-pass-2026!'},
                              format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(staff.post('/api/staff/telegram/').status_code, 403)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password(PASSWORD))

    def test_session_closes_when_superuser_disabled(self):
        staff = self.staff_client(self.impersonate().data['access'])
        self.boss.is_superuser = False
        self.boss.save()
        self.assertEqual(staff.get('/api/staff/me/').status_code, 401)

    def test_token_has_no_refresh_and_short_life(self):
        data = self.impersonate().data
        self.assertNotIn('refresh', data)
        from rest_framework_simplejwt.tokens import AccessToken
        token = AccessToken(data['access'])
        self.assertLessEqual(token['exp'] - token['iat'], 3600)

    def test_blocked_or_ownerless_salon(self):
        self.salon.status = Company.STATUS_BLOCKED
        self.salon.save()
        self.assertEqual(self.impersonate().status_code, 400)
        orphan = Company.objects.create(name='Без владельца', slug='orphan')
        self.assertEqual(self.impersonate(orphan).status_code, 400)

    def test_staff_cannot_impersonate(self):
        staff = self.staff_client(get_staff_tokens(self.owner)['access'])
        self.assertEqual(staff.post(f'/api/super/salons/{self.salon.id}/impersonate/').status_code, 401)
