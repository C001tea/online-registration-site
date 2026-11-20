"""Ритм клиента: расчёт сроков, стадии, права, отметка «позвали», возврат по напоминанию."""
from datetime import time, timedelta
from decimal import Decimal

from django.utils import timezone

from bookings.models import Appointment, Service, ServiceCategory
from dashboard.tests import StaffTestCase
from finance.models import Penalty
from . import rhythm
from .models import ClientOptOut, RhythmMessage

PHONE = '+37369000001'
OTHER = '+37369000002'


class RhythmTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.company.rhythm_enabled = True
        self.company.save()

    def visit(self, days_ago, phone=PHONE, master=None, service=None, status='completed', name='Ирина Попа', **extra):
        return self.appointment(day=self.today - timedelta(days=days_ago), status=status, client_phone=phone,
                                client_name=name, master=master, service=service or self.service, **extra)

    def get(self, stage=None, user=None):
        self.auth(user or self.owner)
        return self.api.get('/api/staff/rhythm/', {'stage': stage} if stage else {})


class RhythmCalcTests(RhythmTestCase):
    def test_own_interval_is_median_and_due_today_is_soon(self):
        for days in (70, 42, 21):  # промежутки 28 и 21 → медиана 24,5 ≈ 24
            self.visit(days)
        response = self.get()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['counts'], {'soon': 1, 'leaving': 0, 'lost': 0})
        row = response.data['results'][0]
        self.assertEqual((row['phone'], row['name'], row['category'], row['interval'], row['visits']),
                         (PHONE, 'Ирина Попа', 'Руки', 24, 3))
        self.assertEqual(row['days_left'], 3)
        self.assertEqual(row['master'], {'id': self.master.id, 'name': self.master.name})
        self.assertTrue(row['own_interval'])
        self.assertIn('Здравствуйте, Ирина!', row['text'])
        self.assertIn('3 недели', row['text'])
        self.assertIn('Салон a', row['text'])

    def test_not_yet_due_is_hidden(self):
        self.visit(40)
        self.visit(10)  # интервал 30, до срока 20 дней
        self.assertEqual(self.get().data['counts'], {'soon': 0, 'leaving': 0, 'lost': 0})

    def test_stages(self):
        self.visit(61, phone='+37369000011')
        self.visit(40, phone='+37369000011')  # интервал 21, прошло 40 ≥ 1,5×21 → уходит
        self.visit(91, phone='+37369000012')
        self.visit(70, phone='+37369000012')  # прошло 70 ≥ 3×21 → потерян
        data = self.get('leaving').data
        self.assertEqual(data['counts'], {'soon': 0, 'leaving': 1, 'lost': 1})
        self.assertEqual(data['results'][0]['phone'], '+37369000011')
        lost = self.get('lost').data['results'][0]
        self.assertEqual(lost['phone'], '+37369000012')
        self.assertIn('Давно вас не видели', lost['text'])

    def test_future_booking_hides_client(self):
        self.visit(42)
        self.visit(21)
        self.appointment(day=self.today + timedelta(days=2), client_phone=PHONE)
        self.assertEqual(self.get().data['counts']['soon'], 0)

    def test_future_booking_in_other_category_does_not_hide(self):
        self.visit(42)
        self.visit(21)
        hair = Service.objects.create(company=self.company, name='Стрижка', duration=30, price=100,
                                      category=ServiceCategory.objects.create(company=self.company, name='Волосы'))
        self.appointment(day=self.today + timedelta(days=2), client_phone=PHONE, service=hair)
        self.assertEqual(self.get().data['counts']['soon'], 1)

    def test_one_visit_uses_service_repeat_days(self):
        self.visit(28)
        self.assertEqual(self.get().data['counts']['soon'], 0)  # интервал неизвестен
        self.service.repeat_days = 30
        self.service.save()
        row = self.get().data['results'][0]
        self.assertEqual((row['interval'], row['days_left'], row['own_interval']), (30, 2, False))

    def test_one_visit_uses_salon_typical_interval(self):
        for i, phone in enumerate(('+37369000021', '+37369000022', '+37369000023')):
            self.visit(100 + i, phone=phone)
            self.visit(80 + i, phone=phone)  # у всех промежуток 20, у них уже «потерян»
        self.visit(19)
        soon = self.get().data['results']
        self.assertEqual([(r['phone'], r['interval']) for r in soon], [(PHONE, 20)])

    def test_cancelled_and_no_show_are_not_visits_and_blacklist_hidden(self):
        self.visit(42)
        self.visit(21)
        self.visit(10, status='cancelled')
        self.assertEqual(self.get().data['counts']['soon'], 1)
        no_show = self.visit(5, status='no_show')
        Penalty.objects.create(company=self.company, appointment=no_show, phone=PHONE, kind='no_show', amount=100)
        self.assertEqual(self.get().data['counts']['soon'], 0)

    def test_close_visits_count_as_one(self):
        self.visit(45)
        self.visit(44)  # доделка на следующий день — не промежуток ритма
        self.visit(23)
        row = self.get().data['results'][0]
        self.assertEqual((row['interval'], row['visits']), (21, 3))

    def test_usual_master_and_channels(self):
        self.visit(63, master=self.master2)
        self.visit(42, master=self.master2, client_email='irina@example.com')
        self.visit(21)
        item = rhythm.client_rhythms(self.company)[0]
        self.assertEqual((item.master_id, item.channel), (self.master2.id, 'email'))

    def test_opted_out_flag_and_ro_text(self):
        self.visit(42, lang='ro')
        self.visit(21, lang='ro')
        ClientOptOut.objects.create(company=self.company, phone=PHONE, kind='rhythm')
        row = self.get().data['results'][0]
        self.assertTrue(row['opted_out'])
        self.assertIn('Bună ziua, Ирина!', row['text'])
        self.assertIn('3 săptămâni', row['text'])

    def test_average_check_uses_payment_or_price(self):
        self.visit(42, price=Decimal('300'))
        self.visit(21, price=Decimal('100'))
        self.assertEqual(self.get().data['results'][0]['average_check'], '200.00')


class RhythmTextTests(RhythmTestCase):
    def test_ago_text(self):
        self.assertEqual(rhythm.ago_text(5, 'ru'), '5 дней')
        self.assertEqual(rhythm.ago_text(21, 'ru'), '3 недели')
        self.assertEqual(rhythm.ago_text(7 * 5, 'ru'), '5 недель')
        self.assertEqual(rhythm.ago_text(30, 'ru'), '4 недели')
        self.assertEqual(rhythm.ago_text(61, 'ru'), '2 месяца')
        self.assertEqual(rhythm.ago_text(1, 'ru'), '1 день')
        self.assertEqual(rhythm.ago_text(7 * 7, 'ro'), '7 săptămâni')
        self.assertEqual(rhythm.ago_text(1, 'ro'), '1 zi')
        self.assertEqual(rhythm.ago_text(150, 'ro'), '5 luni')


class RhythmAccessTests(RhythmTestCase):
    def test_master_of_salon_with_admin_has_no_access(self):
        self.assertEqual(self.get(user=self.master_user).status_code, 403)

    def test_independent_master_sees_only_own_clients(self):
        self.company.masters_independent = True
        self.company.save()
        self.visit(42)
        self.visit(21)
        self.visit(42, phone=OTHER, master=self.master2)
        self.visit(21, phone=OTHER, master=self.master2)
        self.assertEqual(self.get().data['counts']['soon'], 2)
        mine = self.get(user=self.master_user).data['results']
        self.assertEqual([r['phone'] for r in mine], [PHONE])

    def test_other_salon_sees_nothing(self):
        self.visit(42)
        self.visit(21)
        self.assertEqual(self.get(user=self.owner_b).data['counts']['soon'], 0)


class RhythmMarkTests(RhythmTestCase):
    def mark(self, phone=PHONE, channel='whatsapp', group=None):
        return self.api.post('/api/staff/rhythm/mark/', {
            'phone': phone, 'group': group or f'c{self.category.id}', 'channel': channel}, format='json')

    def test_mark_then_shown_and_return_counted(self):
        self.visit(42)
        self.visit(21)
        self.auth(self.owner)
        response = self.mark(phone='069000001')
        self.assertEqual(response.status_code, 201, response.data)
        message = RhythmMessage.objects.get()
        self.assertEqual((message.channel, message.step, message.user, message.cycle),
                         ('whatsapp', 1, self.owner, self.today - timedelta(days=21)))
        data = self.get().data
        self.assertEqual(data['results'][0]['message']['channel'], 'whatsapp')
        self.assertEqual(data['stats'], {'invited': 1, 'returned': 0, 'revenue': '0.00'})
        # Клиент записался после того, как его позвали
        Appointment.objects.create(company=self.company, master=self.master, service=self.service,
                                   client_name='Ирина', client_phone=PHONE, date=self.today + timedelta(days=3),
                                   start_time=time(10), end_time=time(11), status='confirmed', price=Decimal('250'))
        data = self.get().data
        self.assertEqual(data['counts']['soon'], 0)
        self.assertEqual(data['stats'], {'invited': 1, 'returned': 1, 'revenue': '250.00'})

    def test_booking_before_message_not_counted(self):
        self.visit(42)
        self.visit(21)
        booked = self.appointment(day=self.today + timedelta(days=3), client_phone=PHONE)
        Appointment.objects.filter(pk=booked.pk).update(created_at=timezone.now() - timedelta(days=1))
        RhythmMessage.objects.create(company=self.company, phone=PHONE, group=f'c{self.category.id}',
                                     cycle=self.today, channel='telegram')
        self.auth(self.owner)
        self.assertEqual(self.get().data['stats']['returned'], 0)

    def test_mark_errors(self):
        self.visit(42)
        self.visit(21)
        self.auth(self.owner)
        self.assertEqual(self.mark(phone=OTHER).status_code, 404)
        self.assertEqual(self.mark(channel='sms').status_code, 400)
        ClientOptOut.objects.create(company=self.company, phone=PHONE, kind='all')
        self.assertEqual(self.mark().status_code, 400)
        self.auth(self.owner_b)
        self.assertEqual(self.mark().status_code, 404)


class RhythmSettingsTests(RhythmTestCase):
    def test_owner_toggles_and_sets_repeat_days(self):
        self.auth(self.owner)
        response = self.api.patch('/api/staff/company/', {'rhythm_enabled': False}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data['rhythm_enabled'])
        response = self.api.patch(f'/api/staff/services/{self.service.id}/', {'repeat_days': 21}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['repeat_days'], 21)
        self.assertEqual(self.api.patch(f'/api/staff/services/{self.service.id}/', {'repeat_days': 0},
                                        format='json').status_code, 400)
        self.assertEqual(self.api.patch(f'/api/staff/services/{self.service.id}/', {'repeat_days': None},
                                        format='json').data['repeat_days'], None)
