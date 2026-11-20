"""Привлечение: источник онлайн-записи, ссылки мастеров, статистика источников, права."""
from datetime import time

from django.test import TestCase, override_settings

from bookings.models import Appointment
from bookings.sources import clean_source, source_of
from bookings.test_visits import VisitTests
from dashboard.tests import StaffTestCase


class CleanSourceTests(StaffTestCase):
    def test_known_unknown_and_empty(self):
        self.assertEqual(clean_source('Stories'), 'stories')
        self.assertEqual(clean_source(''), 'site')
        self.assertEqual(clean_source('<script>'), 'other')

    def test_old_records(self):
        self.assertEqual(source_of(Appointment(created_by_staff=True)), 'admin')
        self.assertEqual(source_of(Appointment()), 'site')
        self.assertEqual(source_of(Appointment(source='master')), 'master')


class PublicSourceTests(TestCase):
    setUp = VisitTests.setUp
    book = VisitTests.book

    def test_single_booking_saves_source(self):
        payload = {'service': self.manicure.id, 'master': self.anna.id, 'date': self.day.isoformat(),
                   'start_time': '10:00', 'client_name': 'Ирина', 'client_phone': '+37369123456', 'consent': True}
        response = self.api.post('/api/companies/salon/appointments/', {**payload, 'source': 'stories'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Appointment.objects.get().source, 'stories')
        response = self.api.post('/api/companies/salon/appointments/',
                                 {**payload, 'start_time': '12:00', 'client_phone': '+37369123457'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Appointment.objects.order_by('-id').first().source, 'site')

    def test_visit_saves_source(self):
        response = self.book([{'service': self.manicure.id}, {'service': self.brows.id}], '10:00', source='strange')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(set(Appointment.objects.values_list('source', flat=True)), {'other'})

    def test_public_masters_have_link_slug(self):
        self.anna.link_slug = 'anna'
        self.anna.save()
        data = self.api.get('/api/companies/salon/masters/').data
        self.assertEqual({m['name']: m['link_slug'] for m in data}, {'Анна': 'anna', 'Ольга': ''})


@override_settings(SALON_URL_TEMPLATE='https://{slug}.timepoint.tech')
class PromoViewTests(StaffTestCase):
    def test_manager_sees_links_and_sources(self):
        self.appointment(source='stories')
        self.appointment(start=time(12), end=time(13), source='stories')
        self.appointment(start=time(14), end=time(15), created_by_staff=True)
        self.appointment(start=time(15), end=time(16), source='instagram', status='cancelled')
        self.auth(self.owner)
        data = self.api.get('/api/staff/promo/').data
        self.assertFalse(data['enabled'])
        self.assertEqual(data['sources'], {'stories': 2, 'admin': 1})
        self.assertEqual(data['site_url'], 'https://a.timepoint.tech/')
        urls = {m['name']: m['url'] for m in data['masters']}
        self.assertEqual(urls[self.master2.name], f'https://a.timepoint.tech/m/{self.master2.id}')

    def test_visit_counts_once(self):
        import uuid
        visit = uuid.uuid4()
        self.appointment(source='telegram', visit=visit)
        self.appointment(start=time(11), end=time(12), source='telegram', visit=visit)
        self.auth(self.admin)
        self.assertEqual(self.api.get('/api/staff/promo/?days=7').data['sources'], {'telegram': 1})

    def test_master_sees_only_own_link_and_no_stats(self):
        self.auth(self.master_user)
        data = self.api.get('/api/staff/promo/').data
        self.assertEqual([m['id'] for m in data['masters']], [self.master.id])
        self.assertIsNone(data['sources'])

    def test_independent_master_sees_own_stats(self):
        self.company.masters_independent = True
        self.company.save()
        self.appointment(source='master')
        self.appointment(master=self.master2, source='stories')
        self.auth(self.master_user)
        self.assertEqual(self.api.get('/api/staff/promo/').data['sources'], {'master': 1})

    def test_set_link_slug(self):
        self.auth(self.master_user)
        response = self.api.patch(f'/api/staff/promo/master/{self.master.id}/', {'link_slug': ' Anna-Nails '},
                                  format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['url'], 'https://a.timepoint.tech/m/anna-nails')
        # Чужой мастер — нельзя
        self.assertEqual(self.api.patch(f'/api/staff/promo/master/{self.master2.id}/', {'link_slug': 'x1'},
                                        format='json').status_code, 403)
        self.auth(self.owner)
        for bad in ('анна', 'a b', '-a', '123'):
            self.assertEqual(self.api.patch(f'/api/staff/promo/master/{self.master2.id}/', {'link_slug': bad},
                                            format='json').status_code, 400, bad)
        self.assertEqual(self.api.patch(f'/api/staff/promo/master/{self.master2.id}/', {'link_slug': 'anna-nails'},
                                        format='json').status_code, 400)
        self.auth(self.owner_b)
        self.assertEqual(self.api.patch(f'/api/staff/promo/master/{self.master.id}/', {'link_slug': 'z'},
                                        format='json').status_code, 404)

    def test_query_salon_url(self):
        with self.settings(SALON_URL_TEMPLATE='https://site.example/?salon={slug}'):
            self.auth(self.owner)
            data = self.api.get('/api/staff/promo/').data
            self.assertEqual(data['masters'][0]['url'], f'https://site.example/m/{data["masters"][0]["id"]}?salon=a')

    def test_card_has_came_from(self):
        a = self.appointment(source='stories')
        self.auth(self.owner)
        self.assertEqual(self.api.get(f'/api/staff/appointments/{a.id}/').data['came_from'], 'stories')


class WindowsTests(StaffTestCase):
    def test_windows_merge_and_busy(self):
        # Мария работает 9–18; запись 11–12 делит день на два окна
        self.appointment(master=self.master2, start=time(11), end=time(12))
        self.auth(self.owner)
        data = self.api.get(f'/api/staff/promo/windows/?date={self.day.isoformat()}&master={self.master2.id}').data
        self.assertEqual(data['days'][0]['masters'][0]['windows'],
                         [{'from': '09:00', 'to': '11:00'}, {'from': '12:00', 'to': '18:00'}])

    def test_master_sees_only_self_and_days_limit(self):
        self.auth(self.master_user)
        data = self.api.get(f'/api/staff/promo/windows/?date={self.day.isoformat()}&days=30').data
        self.assertEqual(len(data['days']), 7)
        ids = {m['id'] for day in data['days'] for m in day['masters']}
        self.assertTrue(ids <= {self.master.id})
