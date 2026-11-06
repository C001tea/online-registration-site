from datetime import time, timedelta
from unittest import mock

from bookings.models import Appointment, AppointmentLog, Service
from bookings.slots import get_free_slots
from notifications.models import AppointmentTelegram
from .tests import StaffTestCase

URL = '/api/staff/appointments/'


class VisitCardTests(StaffTestCase):
    """Карточка записи: цена и скидка, перерыв, «записывает другого», история, повтор, напоминание, ещё услуга."""

    def setUp(self):
        super().setUp()
        self.auth(self.admin)
        self.a = self.appointment()

    def patch(self, data, appointment=None):
        return self.api.patch(f'{URL}{(appointment or self.a).id}/', data, format='json')

    def test_price_discount_booker_and_history(self):
        response = self.patch({'price': '350', 'discount': '10', 'booker_name': 'Мама', 'booker_phone': '069555111'})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['discount'], '10.00')
        self.assertEqual(response.data['booker_name'], 'Мама')
        self.assertEqual(response.data['booker_phone'], '+37369555111')
        self.assertEqual(response.data['service']['price'], '350.00')
        history = self.api.get(f'{URL}{self.a.id}/history/').data
        self.assertEqual(history[0]['action'], 'updated')
        self.assertIn('discount', history[0]['details'])
        self.assertIn('price', history[0]['details'])
        self.assertEqual(history[0]['user'], 'admin')

    def test_discount_validated(self):
        self.assertEqual(self.patch({'discount': '120'}).status_code, 400)
        self.assertEqual(self.patch({'buffer_minutes': 500}).status_code, 400)

    def test_master_cannot_change_price(self):
        self.auth(self.master_user)
        self.assertEqual(self.patch({'price': '1'}).status_code, 403)

    def test_buffer_blocks_next_slot(self):
        self.assertEqual(self.patch({'buffer_minutes': 15}).status_code, 200)
        self.assertNotIn(time(11), get_free_slots(self.master, self.day, 60))
        self.assertIn(time(11, 30), get_free_slots(self.master, self.day, 60))
        # Запись вплотную после записи с перерывом — пересечение
        response = self.api.post(URL, self.create_payload(start_time='11:00'), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Пересекается', response.data['error'])

    def test_create_logs_history(self):
        response = self.api.post(URL, self.create_payload(start_time='14:00', buffer_minutes=10), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['buffer_minutes'], 10)
        log = AppointmentLog.objects.get(appointment_id=response.data['id'])
        self.assertEqual((log.action, log.actor), ('created', 'staff'))

    def test_repeat_skips_busy_dates(self):
        self.appointment(day=self.day + timedelta(days=14), client_name='Занято')
        response = self.api.post(f'{URL}{self.a.id}/repeat/', {'every_days': 7, 'count': 3}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual([a['date'] for a in response.data['created']],
                         [(self.day + timedelta(days=d)).isoformat() for d in (7, 21)])
        self.assertEqual(response.data['skipped'][0]['date'], (self.day + timedelta(days=14)).isoformat())
        self.assertEqual(self.api.post(f'{URL}{self.a.id}/repeat/', {'every_days': 0, 'count': 3},
                                       format='json').status_code, 400)

    def test_add_service_goes_after_visit(self):
        nails = Service.objects.create(company=self.company, category=self.category, name='Дизайн', duration=30, price=50)
        self.master.services.add(nails)
        response = self.api.post(f'{URL}{self.a.id}/add-service/', {'service': nails.id}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data['start_time'], response.data['end_time']), ('11:00', '11:30'))
        self.a.refresh_from_db()
        self.assertIsNotNone(self.a.visit)
        self.assertEqual(response.data['visit'], str(self.a.visit))
        # Вторая — после второй
        again = self.api.post(f'{URL}{self.a.id}/add-service/', {'service': nails.id}, format='json')
        self.assertEqual(again.data['start_time'], '11:30')
        # Услуга, которую мастер не делает
        other = Service.objects.create(company=self.company, name='Педикюр', duration=60, price=300)
        self.assertEqual(self.api.post(f'{URL}{self.a.id}/add-service/', {'service': other.id},
                                       format='json').status_code, 400)

    @mock.patch('notifications.telegram.enabled', return_value=True)
    def test_remind_by_telegram_once_per_10_min(self, _enabled):
        AppointmentTelegram.objects.create(appointment=self.a, chat_id=555)
        response = self.api.post(f'{URL}{self.a.id}/remind/')
        self.assertEqual(response.data, {'channel': 'telegram'})
        self.assertEqual(self.api.post(f'{URL}{self.a.id}/remind/').status_code, 400)
        card = self.api.get(f'{URL}{self.a.id}/').data
        self.assertTrue(card['reminder']['telegram'])

    def test_remind_without_contacts(self):
        response = self.api.post(f'{URL}{self.a.id}/remind/')
        self.assertEqual(response.status_code, 400)
        self.assertIn('позвоните', response.data['error'])

    def test_foreign_salon_cannot_use_actions(self):
        self.auth(self.owner_b)
        for path in ('history/',):
            self.assertEqual(self.api.get(f'{URL}{self.a.id}/{path}').status_code, 404)
        self.assertEqual(self.api.post(f'{URL}{self.a.id}/repeat/', {'every_days': 7, 'count': 1},
                                       format='json').status_code, 404)

    def test_paid_visit_price_locked(self):
        self.a.status = 'completed'
        self.a.date = self.today
        self.a.save()
        self.auth(self.owner)
        self.assertEqual(self.api.post(f'/api/staff/appointments/{self.a.id}/checkout/', {'method': 'cash'},
                                       format='json').status_code, 201)
        self.assertEqual(self.patch({'price': '999'}).status_code, 400)
        actions = list(Appointment.objects.get(pk=self.a.pk).logs.values_list('action', flat=True))
        self.assertIn('paid', actions)
