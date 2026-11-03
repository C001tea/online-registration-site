import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import AdminProfile, ClientProfile, Company, Master, MasterSchedule, Service
from dashboard.tokens import get_staff_tokens

PASSWORD = 'Salon-pass-2026'
SIGNUP = {'salon_name': 'Студия Ногти', 'slug': 'studio-nogti', 'email': 'Owner@Example.com',
          'password': PASSWORD, 'phone': '069123456', 'accept_terms': True}

FAST_HASH = override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
OPEN = override_settings(SIGNUP_ENABLED=True, EMAIL_CONFIGURED=True, TRIAL_DAYS=14,
                         PANEL_URL='https://panel.test', SALON_URL_TEMPLATE='https://{slug}.salon.test')


def link_from(message, path):
    return re.search(rf'https://panel\.test/admin-panel/{path}\?(\S+)', message.body).group(1)


@FAST_HASH
@OPEN
class SignupTests(TestCase):
    def setUp(self):
        self.api = APIClient()

    def signup(self, ip='10.0.0.1', **changes):
        return self.api.post('/api/staff/signup/', {**SIGNUP, **changes}, format='json', HTTP_X_REAL_IP=ip)

    def test_closed_without_flag(self):
        with override_settings(SIGNUP_ENABLED=False):
            self.assertEqual(self.signup().status_code, 403)
            config = self.api.get('/api/staff/signup/config/').data
        self.assertEqual((config['signup'], config['salon_url_template']), (False, 'https://{slug}.salon.test'))
        self.assertFalse(Company.objects.exists())

    def test_slug_check(self):
        Company.objects.create(name='Есть', slug='nail-bar')
        check = lambda **params: self.api.get('/api/staff/signup/slug/', params).data  # noqa: E731
        self.assertEqual(check(name='Студия «Ногти» №1')['slug'], 'studiya-nogti-1')
        self.assertEqual(check(name='Salon Frumușețe')['slug'], 'salon-frumusete')
        self.assertTrue(check(slug='free-one')['available'])
        taken = check(slug='nail-bar')
        self.assertEqual((taken['available'], taken['suggestion']), (False, 'nail-bar-2'))
        self.assertIn('служебный', check(slug='admin')['error'])
        self.assertIn('латинские', check(slug='Bad Slug!')['error'])

    def test_signup_creates_hidden_salon_with_trial_and_logs_in(self):
        response = self.signup()
        self.assertEqual(response.status_code, 201, response.data)
        company = Company.objects.get(slug='studio-nogti')
        self.assertEqual(company.status, Company.STATUS_PENDING)
        self.assertAlmostEqual(company.trial_ends_at, timezone.now() + timedelta(days=14), delta=timedelta(minutes=1))
        self.assertIsNotNone(company.terms_accepted_at)
        self.assertEqual(company.phone, '+37369123456')
        owner = User.objects.get(username='owner@example.com')
        self.assertEqual((owner.email, AdminProfile.objects.get(user=owner).role), ('owner@example.com', 'owner'))
        me = response.data['me']
        self.assertEqual((me['role'], me['company']['status'], me['company']['public_url']),
                         ('owner', 'pending', 'https://studio-nogti.salon.test'))
        # Сразу в панели — по выданному токену
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {response.data["access"]}')
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 200)
        # Письмо с подтверждением; до подтверждения сайт салона клиентам не виден
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('verify-email?token=', mail.outbox[0].body)
        public = APIClient()
        self.assertEqual(public.get('/api/companies/studio-nogti/info/').status_code, 404)
        self.assertEqual(public.get('/api/companies/studio-nogti/services/').status_code, 404)

    def test_verify_email_publishes_salon(self):
        self.signup()
        token = link_from(mail.outbox[0], 'verify-email').removeprefix('token=')
        response = self.api.post('/api/staff/verify-email/', {'token': token}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['public_url'], 'https://studio-nogti.salon.test')
        self.assertEqual(Company.objects.get(slug='studio-nogti').status, Company.STATUS_ACTIVE)
        self.assertEqual(APIClient().get('/api/companies/studio-nogti/info/').status_code, 200)
        # Повторный переход по ссылке — не ошибка
        self.assertEqual(self.api.post('/api/staff/verify-email/', {'token': token}, format='json').status_code, 200)
        # Подделанная ссылка
        self.assertEqual(self.api.post('/api/staff/verify-email/', {'token': token + 'x'}, format='json').status_code, 400)

    def test_rejects_bad_input(self):
        User.objects.create_user('taken@example.com', email='taken@example.com', password=PASSWORD)
        Company.objects.create(name='Есть', slug='busy')
        cases = [
            ({'email': 'TAKEN@example.com'}, 'email'),
            ({'password': '12345678'}, 'password'),
            ({'accept_terms': False}, 'accept_terms'),
            ({'slug': 'busy'}, 'slug'),
            ({'slug': 'api'}, 'slug'),
            ({'phone': '123'}, 'phone'),
        ]
        for i, (change, field) in enumerate(cases):
            with self.subTest(change=change):
                response = self.signup(ip=f'10.0.1.{i}', **change)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
        self.assertEqual(self.signup(ip='10.0.2.1', website='http://spam').status_code, 400)  # поле-ловушка
        self.assertFalse(Company.objects.filter(slug='studio-nogti').exists())

    def test_rate_limited_per_ip(self):
        for i in range(5):
            self.assertEqual(self.signup(slug=f'salon-{i}', email=f'o{i}@example.com').status_code, 201)
        self.assertEqual(self.signup(slug='salon-9', email='o9@example.com').status_code, 429)

    def test_login_by_email_any_case_and_resend(self):
        self.signup()
        response = self.api.post('/api/staff/auth/login/', {'username': ' OWNER@example.COM ', 'password': PASSWORD},
                                 format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {response.data["access"]}')
        self.assertEqual(self.api.post('/api/staff/verify-email/resend/').status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        Company.objects.update(status=Company.STATUS_ACTIVE)
        self.assertEqual(self.api.post('/api/staff/verify-email/resend/').status_code, 400)

    def test_name_cleaned_and_letter_sent(self):
        """Перенос строки и невидимые символы в названии: письмо уходит, название в одну строку."""
        response = self.signup(salon_name='Bad\nSubject\r\nBcc: x@evil.com \u202eтест\u202c')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Company.objects.get(slug='studio-nogti').name, 'Bad Subject Bcc: x@evil.com тест')
        self.assertTrue(response.data['email_sent'])
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn('\n', mail.outbox[0].subject)
        self.assertIsNone(mail.outbox[0].extra_headers.get('Bcc'))
        self.assertEqual(self.signup(ip='10.0.3.1', salon_name=' \n\u200b ', slug='empty-name',
                                     email='e@example.com').status_code, 400)

    def test_phone_is_optional(self):
        data = {k: v for k, v in SIGNUP.items() if k != 'phone'}
        response = self.api.post('/api/staff/signup/', data, format='json', HTTP_X_REAL_IP='10.0.4.1')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Company.objects.get(slug='studio-nogti').phone, '')

    def test_total_limit_ignores_spoofed_ip(self):
        """Подделка X-Real-IP не обходит общий лимит регистраций."""
        from unittest import mock
        with mock.patch('dashboard.signup.SIGNUPS_TOTAL_HOUR', 3):
            codes = [self.signup(ip=f'10.1.0.{i}', slug=f'spoof-{i}', email=f's{i}@example.com').status_code
                     for i in range(4)]
        self.assertEqual(codes, [201, 201, 201, 429])

    def test_manual_salons_stay_visible(self):
        """Салоны, подключённые вручную, и все прежние — работают как раньше."""
        Company.objects.create(name='Старый', slug='old-salon')
        self.assertEqual(APIClient().get('/api/companies/old-salon/info/').status_code, 200)
        Company.objects.filter(slug='old-salon').update(status=Company.STATUS_BLOCKED)
        self.assertEqual(APIClient().get('/api/companies/old-salon/info/').status_code, 404)


@FAST_HASH
@OPEN
class PasswordResetTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        company = Company.objects.create(name='Салон', slug='salon')
        self.owner = User.objects.create_user('owner@example.com', email='owner@example.com', password=PASSWORD)
        AdminProfile.objects.create(user=self.owner, company=company, role='owner')

    def request_reset(self, email):
        return self.api.post('/api/staff/password-reset/', {'email': email}, format='json')

    def test_same_answer_for_unknown_email_and_no_letter(self):
        known, unknown = self.request_reset('OWNER@example.com'), self.request_reset('nobody@example.com')
        self.assertEqual((known.status_code, unknown.status_code), (200, 200))
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@example.com'])

    def test_reset_sets_password_once_and_logs_out_old_sessions(self):
        old_tokens = get_staff_tokens(self.owner)
        self.request_reset('owner@example.com')
        query = dict(part.split('=', 1) for part in link_from(mail.outbox[0], 'reset-password').split('&'))
        confirm = lambda password: self.api.post('/api/staff/password-reset/confirm/',  # noqa: E731
                                                 {**query, 'password': password}, format='json')
        self.assertEqual(confirm('12345678').status_code, 400)  # слабый пароль
        self.assertEqual(confirm('New-salon-pass-1').status_code, 200)
        self.assertEqual(confirm('Other-salon-pass-2').status_code, 400)  # ссылка одноразовая
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('New-salon-pass-1'))
        # Старый вход больше не действует
        self.api.credentials(HTTP_AUTHORIZATION=f'Bearer {old_tokens["access"]}')
        self.assertEqual(self.api.get('/api/staff/me/').status_code, 401)

    def test_no_letters_for_clients_and_limits(self):
        client = User.objects.create_user('+37369000555', email='client@example.com', password=PASSWORD)
        ClientProfile.objects.create(user=client, phone='+37369000555')
        self.request_reset('client@example.com')
        self.assertEqual(len(mail.outbox), 0)
        for _ in range(5):
            self.request_reset('owner@example.com')
        self.assertEqual(len(mail.outbox), 3)  # не больше 3 писем в час на адрес

    def test_unavailable_without_email(self):
        with override_settings(EMAIL_CONFIGURED=False):
            self.assertEqual(self.request_reset('owner@example.com').status_code, 503)


@FAST_HASH
@OPEN
class OnboardingTests(TestCase):
    def test_steps_and_ready(self):
        api = APIClient()
        response = api.post('/api/staff/signup/', SIGNUP, format='json')
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {response.data["access"]}')
        data = api.get('/api/staff/onboarding/').data
        self.assertEqual({s['key']: s['done'] for s in data['steps']},
                         {'email': False, 'salon': False, 'services': False, 'masters': False,
                          'schedule': False, 'telegram': False})
        self.assertFalse(data['ready'])

        company = Company.objects.get(slug='studio-nogti')
        company.status, company.address = Company.STATUS_ACTIVE, 'str. Test 1'
        company.save()
        service = Service.objects.create(company=company, name='Маникюр', duration=60, price=200)
        master = Master.objects.create(company=company, name='Анна')
        master.services.add(service)
        MasterSchedule.objects.create(master=master, day_of_week=0, start_time='09:00', end_time='18:00', is_working=True)
        data = api.get('/api/staff/onboarding/').data
        self.assertEqual(data['done'], 5)
        self.assertTrue(data['ready'])
        self.assertEqual(data['public_url'], 'https://studio-nogti.salon.test')


class CompanyNameTests(TestCase):
    def test_settings_name_cleaned(self):
        from dashboard.serializers import StaffCompanySerializer
        company = Company.objects.create(name='Салон', slug='salon-name')
        serializer = StaffCompanySerializer(company, data={'name': 'Новое\r\nназвание‮'}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data['name'], 'Новое название')
        self.assertFalse(StaffCompanySerializer(company, data={'name': '\n '}, partial=True).is_valid())
