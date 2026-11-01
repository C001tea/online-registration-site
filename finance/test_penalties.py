from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.i18n import MONTHS
from bookings.models import Appointment, ClientProfile
from dashboard.tests import StaffTestCase
from notifications import services as notifications
from notifications import telegram
from notifications.models import AppointmentTelegram
from notifications.tests import BOT, FakeTelegram
from .models import CashOperation, Penalty

PHONE = '+37369000001'


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class BlacklistTests(StaffTestCase):
    """Чёрный список: штраф 50 % за неявку и отмену позже чем за 3 ч, оплата при следующем визите."""

    def setUp(self):
        super().setUp()
        self.auth(self.admin)

    def soon(self, hours, **extra):
        start = timezone.localtime() + timedelta(hours=hours)
        return self.appointment(day=start.date(), start=start.time().replace(second=0, microsecond=0),
                                end=time(23, 59), **extra)

    def patch(self, appointment, **data):
        return self.api.patch(f'/api/staff/appointments/{appointment.id}/', data, format='json')

    def test_no_show_creates_penalty_and_blacklists_client(self):
        past = self.appointment(day=self.today - timedelta(days=2))
        future = self.appointment(day=self.day, start=time(12), end=time(13))
        response = self.patch(past, status='no_show')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['penalty']['amount'], '100.00')  # 50 % от 200
        penalty = Penalty.objects.get()
        self.assertEqual((penalty.kind, penalty.status, penalty.phone, penalty.created_by), ('no_show', 'unpaid', PHONE, self.admin))
        # Следующая запись клиента в журнале — с пометкой
        journal = self.api.get('/api/staff/appointments/', {'date_from': self.day.isoformat()}).data
        self.assertEqual([a['blacklist'] for a in journal if a['id'] == future.id], [{'debt': '100.00', 'count': 1}])
        # Ввод номера в форме записи — сразу видно
        lookup = self.api.get('/api/staff/clients/lookup/', {'phone': '069000001'}).data
        self.assertEqual(lookup['blacklist'], {'debt': '100.00', 'count': 1})
        self.assertEqual(lookup['penalties'][0]['kind'], 'no_show')
        # Клиентская база: метка и фильтр
        clients = self.api.get('/api/staff/clients/', {'blacklist': '1'}).data
        self.assertEqual([(c['phone'], c['blacklist']['debt']) for c in clients['results']], [(PHONE, '100.00')])
        card = self.api.get('/api/staff/clients/card/', {'phone': PHONE}).data
        self.assertEqual(card['penalties'][0]['amount'], '100.00')

    def test_skip_penalty_and_fix_mistake(self):
        past = self.appointment(day=self.today - timedelta(days=2))
        self.patch(past, status='no_show', penalty=False)
        self.assertFalse(Penalty.objects.exists())
        self.patch(past, status='confirmed')
        self.patch(past, status='no_show')
        self.assertEqual(Penalty.objects.count(), 1)
        # Ошиблись — исправили статус: неоплаченный штраф снимается
        self.patch(past, status='completed')
        self.assertFalse(Penalty.objects.exists())

    def test_paid_penalty_locks_status(self):
        past = self.appointment(day=self.today - timedelta(days=2))
        self.patch(past, status='no_show')
        penalty = Penalty.objects.get()
        self.api.post(f'/api/staff/penalties/{penalty.id}/pay/', {'method': 'cash'}, format='json')
        response = self.patch(past, status='completed')
        self.assertEqual(response.status_code, 400)
        self.assertIn('оплачен', response.data['error'])

    def test_staff_cancel_late_only_with_flag(self):
        late = self.soon(2)
        self.patch(late, status='cancelled')  # салон отменил — без штрафа
        self.assertFalse(Penalty.objects.exists())
        self.patch(late, status='confirmed')
        self.patch(late, status='cancelled', penalty=True)  # клиент позвонил поздно
        self.assertEqual(Penalty.objects.get().kind, 'late_cancel')
        early = self.appointment(day=self.day, start=time(15), end=time(16))
        self.patch(early, status='cancelled', penalty=True)  # больше 3 ч — штрафа нет
        self.assertEqual(Penalty.objects.count(), 1)

    def test_settings_off_and_no_price(self):
        self.company.penalty_percent = 0
        self.company.save()
        self.patch(self.appointment(day=self.today - timedelta(days=1)), status='no_show')
        self.assertFalse(Penalty.objects.exists())
        self.company.penalty_percent = 50
        self.company.save()
        no_service = self.appointment(day=self.today - timedelta(days=1), start=time(15), end=time(16), service=None)
        self.patch(no_service, status='no_show')
        self.assertFalse(Penalty.objects.exists())

    def test_checkout_pays_debt_at_next_visit(self):
        self.patch(self.appointment(day=self.today - timedelta(days=3)), status='no_show')
        visit = self.appointment(day=self.today, start=time(10), end=time(11))
        response = self.api.post(f'/api/staff/appointments/{visit.id}/checkout/',
                                 {'method': 'card', 'pay_penalties': True}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['penalties_paid'], '100.00')
        penalty = Penalty.objects.get()
        self.assertEqual(penalty.status, 'paid')
        operation = penalty.operation
        self.assertEqual((operation.kind, operation.amount, operation.method, operation.category),
                         ('income', Decimal('100.00'), 'card', 'Штраф за неявку'))
        # Долга нет, но клиент остаётся в чёрном списке
        lookup = self.api.get('/api/staff/clients/lookup/', {'phone': PHONE}).data
        self.assertEqual(lookup['blacklist'], {'debt': '0.00', 'count': 1})
        # Приход штрафа — в отчёте владельца, удалить его как обычную операцию нельзя
        self.auth(self.owner)
        summary = self.api.get('/api/staff/finance/summary/', {'date_from': self.today.isoformat()}).data
        self.assertEqual(summary['other_income'], '100.00')
        self.assertEqual(self.api.delete(f'/api/staff/finance/operations/{operation.id}/').status_code, 400)

    def test_pay_forgive_cancel_permissions(self):
        self.patch(self.appointment(day=self.today - timedelta(days=3)), status='no_show')
        penalty = Penalty.objects.get()
        url = f'/api/staff/penalties/{penalty.id}/'
        self.auth(self.master_user)
        self.assertEqual(self.api.post(url + 'forgive/', {}, format='json').status_code, 403)
        self.auth(self.owner_b)
        self.assertEqual(self.api.post(url + 'forgive/', {}, format='json').status_code, 404)
        self.auth(self.admin)
        self.assertEqual(self.api.post(url + 'pay/', {'method': 'cash'}, format='json').data['status'], 'paid')
        self.assertEqual(self.api.delete(url + 'pay/').status_code, 403)  # отменить оплату — владелец
        self.auth(self.owner)
        self.assertEqual(self.api.delete(url + 'pay/').data['status'], 'unpaid')
        self.assertFalse(CashOperation.objects.exists())
        response = self.api.post(url + 'forgive/', {'note': 'звонила заранее'}, format='json')
        self.assertEqual((response.data['status'], response.data['note']), ('forgiven', 'звонила заранее'))
        # Прощённый — не в чёрном списке
        self.assertIsNone(self.api.get('/api/staff/clients/lookup/', {'phone': PHONE}).data['blacklist'])
        self.assertEqual(self.api.post(url + 'pay/', {}, format='json').status_code, 400)

    def test_salon_settings(self):
        self.auth(self.owner)
        response = self.api.patch('/api/staff/company/', {'penalty_percent': 30, 'late_cancel_hours': 5}, format='json')
        self.assertEqual((response.data['penalty_percent'], response.data['late_cancel_hours']), (30, 5))
        self.assertEqual(self.api.patch('/api/staff/company/', {'penalty_percent': 150}, format='json').status_code, 400)
        public = self.api.get('/api/companies/a/info/').data
        self.assertEqual((public['penalty_percent'], public['late_cancel_hours']), (30, 5))


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], **BOT)
class ClientLateCancelTests(StaffTestCase):
    """Клиент сам отменяет позже чем за 3 ч — на сайте и в боте; видит предупреждение и долг."""

    def setUp(self):
        super().setUp()
        self.fake = FakeTelegram()
        patcher = mock.patch.object(telegram, 'call', self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(PHONE, password='Str0ng-pass-123')
        ClientProfile.objects.create(user=self.user, phone=PHONE, name='Ирина')
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)

    def soon(self, hours):
        start = timezone.localtime() + timedelta(hours=hours)
        return self.appointment(day=start.date(), start=start.time().replace(second=0, microsecond=0),
                                end=time(23, 59), client=self.user)

    def test_site_cancel_late_and_warning(self):
        late, early = self.soon(2), self.soon(30)
        mine = {a['id']: a['late_cancel_penalty'] for a in self.client_api.get('/api/appointments/my/').data}
        self.assertEqual((mine[late.id], mine[early.id]), ('100.00', None))
        self.assertEqual(self.client_api.post(f'/api/appointments/{early.id}/cancel/').status_code, 200)
        self.assertFalse(Penalty.objects.exists())
        self.assertEqual(self.client_api.post(f'/api/appointments/{late.id}/cancel/').status_code, 200)
        penalty = Penalty.objects.get()
        self.assertEqual((penalty.kind, penalty.created_by), ('late_cancel', None))
        debt = self.client_api.get('/api/companies/a/my-debt/').data
        self.assertEqual(debt['debt'], '100.00')

    def test_bot_warns_and_penalizes(self):
        late = self.soon(2)
        link = AppointmentTelegram.objects.create(appointment=late, chat_id=5001)
        self.client_api.credentials()
        api = APIClient()
        hook = lambda data: api.post('/api/telegram/webhook/', {'update_id': 1, 'callback_query': {
            'id': 'cb', 'data': data, 'message': {'message_id': 1, 'chat': {'id': 5001, 'type': 'private'}}}},
            format='json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=telegram.webhook_secret())
        hook(f'cx:{link.id}')
        self.assertIn('будет начислен штраф 100 L', self.fake.last('editMessageText')['text'])
        with self.captureOnCommitCallbacks(execute=True):
            hook(f'cy:{link.id}')
        self.assertEqual(Penalty.objects.get().kind, 'late_cancel')
        late.refresh_from_db()
        self.assertEqual(late.status, 'cancelled')

    def test_client_notified_and_next_booking_reminds_debt(self):
        past = self.appointment(day=self.today - timedelta(days=1), client=self.user)
        AppointmentTelegram.objects.create(appointment=past, chat_id=5001)
        self.auth(self.admin)
        self.api.patch(f'/api/staff/appointments/{past.id}/', {'status': 'no_show'}, format='json')
        queued = list(notifications.OutgoingMessage.objects.filter(chat_id=5001).values_list('text', flat=True))
        self.assertTrue(any('Вы не пришли' in t and '100 L' in t for t in queued))
        # Следующая онлайн-запись: карточка клиенту напоминает о долге
        nxt = self.appointment(day=self.day, client=self.user)
        card = notifications.card_text(nxt)
        self.assertIn('Неоплаченный штраф: 100 L', card)
        self.assertIn('вы не пришли на «Маникюр» (200 L)', card)
        self.assertIn('Согласно правилам салона', card)
        self.assertIn('оплатите его в салоне при этом визите', card)
        # Салону — блок: почему в чёрном списке и что получить при визите
        staff = notifications.texts.staff_new(nxt, notifications.blacklist_of(nxt))
        self.assertIn('КЛИЕНТ В ЧЁРНОМ СПИСКЕ', staff)
        self.assertIn('не пришёл на «Маникюр» (200 L), не предупредив', staff)
        self.assertIn('При визите получить: 100 L', staff)
        self.assertIn('по правилам салона', staff)
        # Штраф оплачен — салон видит историю, клиент больше не получает напоминание
        Penalty.objects.update(status='paid')
        self.assertIn('Долгов нет', notifications.texts.staff_new(nxt, notifications.blacklist_of(nxt)))
        self.assertNotIn('штраф', notifications.card_text(nxt).lower())

    def test_romanian_client_debt_block(self):
        past = self.appointment(day=self.today - timedelta(days=1), client=self.user)
        self.auth(self.admin)
        self.api.patch(f'/api/staff/appointments/{past.id}/', {'status': 'no_show'}, format='json')
        nxt = self.appointment(day=self.day, client=self.user, lang='ro')
        card = notifications.card_text(nxt)
        self.assertIn('Amendă neachitată: 100 L', card)
        self.assertIn('nu v-ați prezentat la «Маникюр»', card)
        self.assertIn(MONTHS['ro'][past.date.month - 1], card)
        self.assertIn('Conform regulilor salonului', card)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DebtorsTests(StaffTestCase):
    """Экран «Должники»: кто должен, сколько, когда придёт; только свой салон, только менеджеры."""

    def no_show(self, phone, name, days_ago):
        appointment = self.appointment(day=self.today - timedelta(days=days_ago), client_phone=phone, client_name=name)
        self.auth(self.admin)
        self.api.patch(f'/api/staff/appointments/{appointment.id}/', {'status': 'no_show'}, format='json')
        return Penalty.objects.get(appointment=appointment)

    def test_debtors_sorted_by_next_visit_then_debt(self):
        self.no_show('+37369000001', 'Старое Имя', 9)
        self.appointment(day=self.today - timedelta(days=8), client_phone='+37369000001', client_name='Ана Долг',
                         start=time(15), end=time(16), status='completed')
        self.no_show('+37369000001', 'Ана Долг', 5)          # два штрафа — 200 L, записей нет
        self.no_show('+37369000002', 'Вера Скоро', 4)        # 100 L, придёт через 7 дней
        self.appointment(day=self.day, client_phone='+37369000002', client_name='Вера Скоро', start=time(12), end=time(13))
        paid = self.no_show('+37369000003', 'Оплатила', 3)    # оплачен — не должник
        self.api.post(f'/api/staff/penalties/{paid.id}/pay/', {'method': 'cash'}, format='json')
        forgiven = self.no_show('+37369000004', 'Прощена', 2)  # прощён — не должник
        self.api.post(f'/api/staff/penalties/{forgiven.id}/forgive/', {'note': 'болела'}, format='json')

        data = self.api.get('/api/staff/debtors/').data
        self.assertEqual((data['total_debt'], data['debtors'], data['penalties'], data['paid_this_month']),
                         ('300.00', 2, 3, '100.00'))
        self.assertEqual([(r['name'], r['debt']) for r in data['results']], [('Вера Скоро', '100.00'), ('Ана Долг', '200.00')])
        self.assertEqual(data['results'][0]['next_visit']['date'], self.day.isoformat())
        self.assertIsNone(data['results'][1]['next_visit'])
        self.assertEqual(len(data['results'][1]['penalties']), 2)

    def test_only_managers_of_own_salon(self):
        self.no_show('+37369000001', 'Ана', 3)
        self.auth(self.master_user)
        self.assertEqual(self.api.get('/api/staff/debtors/').status_code, 403)
        self.auth(self.owner_b)
        self.assertEqual(self.api.get('/api/staff/debtors/').data['debtors'], 0)
        self.auth(self.owner)
        self.assertEqual(self.api.get('/api/staff/debtors/').data['debtors'], 1)
