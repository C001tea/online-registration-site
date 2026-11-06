"""Визит из нескольких услуг подряд, в том числе у разных мастеров."""
from datetime import time, timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Appointment, Company, Master, MasterSchedule, MasterServiceTerms, Service


class VisitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company = Company.objects.create(name='Салон', slug='salon', phone='+37322000000', auto_confirm=True)
        self.manicure = Service.objects.create(company=self.company, name='Маникюр', duration=60, price=300)
        self.brows = Service.objects.create(company=self.company, name='Брови', duration=30, price=150)
        self.anna = Master.objects.create(company=self.company, name='Анна')
        self.olga = Master.objects.create(company=self.company, name='Ольга')
        self.anna.services.add(self.manicure)
        self.olga.services.add(self.brows, self.manicure)
        for master in (self.anna, self.olga):
            for day in range(7):
                MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(10), end_time=time(14))
        self.day = timezone.localdate() + timedelta(days=3)

    def slots(self, items):
        response = self.api.get('/api/companies/salon/visit-slots/', {'date': self.day.isoformat(), 'items': items})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['slots']

    def book(self, items, start, **extra):
        return self.api.post('/api/companies/salon/visits/', {
            'items': items, 'date': self.day.isoformat(), 'start_time': start, 'client_name': 'Ирина',
            'client_phone': '+37369123456', 'consent': True, **extra}, format='json')

    def test_chain_two_masters_back_to_back(self):
        slots = self.slots(f'{self.manicure.id}:{self.anna.id},{self.brows.id}')
        first = slots[0]
        self.assertEqual(first['start_time'], '10:00')
        self.assertEqual(first['end_time'], '11:30')
        self.assertEqual([p['master_name'] for p in first['parts']], ['Анна', 'Ольга'])
        self.assertEqual(first['parts'][1]['start_time'], '11:00')
        # Последнее начало: 12:30 (маникюр до 13:30, брови до 14:00)
        self.assertEqual(slots[-1]['start_time'], '12:30')

    def test_any_master_prefers_chosen_one(self):
        first = self.slots(f'{self.brows.id}:{self.olga.id},{self.manicure.id}')[0]
        self.assertEqual([p['master_name'] for p in first['parts']], ['Ольга', 'Ольга'])

    def test_busy_master_shifts_chain(self):
        Appointment.objects.create(company=self.company, master=self.olga, service=self.brows, client_name='А',
                                   client_phone='+37369000000', date=self.day, start_time=time(11),
                                   end_time=time(11, 30), status='confirmed')
        starts = [s['start_time'] for s in self.slots(f'{self.manicure.id}:{self.anna.id},{self.brows.id}')]
        self.assertNotIn('10:00', starts)
        self.assertIn('10:30', starts)

    def test_master_own_duration_and_price(self):
        MasterServiceTerms.objects.create(master=self.olga, service=self.brows, duration=45, price=200)
        first = self.slots(f'{self.manicure.id}:{self.anna.id},{self.brows.id}')[0]
        self.assertEqual(first['end_time'], '11:45')
        self.assertEqual(first['parts'][1]['price'], '200.00')

    def test_create_visit_and_manage_whole(self):
        response = self.book([{'service': self.manicure.id, 'master': self.anna.id},
                              {'service': self.brows.id}], '10:00', reminder_minutes=120)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data['parts']), 2)
        rows = Appointment.objects.filter(visit=response.data['visit']).order_by('start_time')
        self.assertEqual([(r.master_id, r.start_time) for r in rows], [(self.anna.id, time(10)), (self.olga.id, time(11))])
        self.assertTrue(all(r.reminder_minutes == 120 and r.status == 'confirmed' for r in rows))

        token = response.data['manage_token']
        data = self.api.get(f'/api/manage/{token}/').data
        self.assertEqual(len(data['visit']['parts']), 2)
        self.assertEqual(data['visit']['end_time'], '11:30')

        # Перенос всего визита на 12:00
        slots = self.api.get(f'/api/manage/{token}/slots/', {'date': self.day.isoformat()}).data['slots']
        self.assertIn('10:00', slots)  # сам визит время не занимает
        moved = self.api.post(f'/api/manage/{token}/reschedule/',
                              {'date': self.day.isoformat(), 'start_time': '12:00'}, format='json')
        self.assertEqual(moved.status_code, 200, moved.data)
        self.assertEqual([r.start_time for r in rows.all()], [time(12), time(13)])

        cancelled = self.api.post(f'/api/manage/{token}/cancel/')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(set(rows.values_list('status', flat=True)), {'cancelled'})

    def test_taken_time_refused(self):
        self.assertEqual(self.book([{'service': self.manicure.id, 'master': self.anna.id}], '10:00').status_code, 201)
        cache.clear()
        again = self.book([{'service': self.manicure.id, 'master': self.anna.id}, {'service': self.brows.id}],
                          '10:00', client_phone='+37369999999')
        self.assertEqual(again.status_code, 400)

    def test_visit_counts_as_one_upcoming(self):
        for start in ('10:00', '12:00'):
            response = self.book([{'service': self.manicure.id, 'master': self.olga.id},
                                  {'service': self.brows.id, 'master': self.olga.id}], start)
            self.assertEqual(response.status_code, 201, response.data)
        # Две записи по две услуги = 2 визита из 3 разрешённых: третий визит ещё можно
        self.day += timedelta(days=1)
        self.assertEqual(self.book([{'service': self.brows.id}], '10:00').status_code, 201)
        self.day += timedelta(days=1)
        self.assertEqual(self.book([{'service': self.brows.id}], '10:00').status_code, 400)

    def test_validation(self):
        self.assertEqual(self.book([], '10:00').status_code, 400)
        self.assertEqual(self.book([{'service': self.brows.id, 'master': self.anna.id}], '10:00').status_code, 400)
        self.assertEqual(self.book([{'service': 99999}], '10:00').status_code, 400)
        self.assertEqual(self.book([{'service': self.brows.id}] * 6, '10:00').status_code, 400)
        no_consent = self.book([{'service': self.brows.id}], '10:00', consent=False)
        self.assertEqual(no_consent.status_code, 400)
        bad = self.api.get('/api/companies/salon/visit-slots/', {'date': 'x', 'items': f'{self.brows.id}'})
        self.assertEqual(bad.status_code, 400)
