from datetime import time, timedelta

from django.test import override_settings

from bookings.models import Appointment
from .tests import StaffTestCase


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class VisitConfirmTests(StaffTestCase):
    """Отметка «клиент подтвердил визит» в панели и список «завтра не подтвердили»."""

    def test_mark_by_phone_and_unmark(self):
        self.auth(self.admin)
        a = self.appointment(status='pending')
        url = f'/api/staff/appointments/{a.id}/visit-confirm/'
        response = self.api.post(url, {'confirmed': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['visit_confirmed'], 'phone')
        self.assertEqual(response.data['status'], 'pending')  # статус — отдельно
        self.assertTrue(a.logs.filter(action='visit_confirmed').exists())
        response = self.api.post(url, {'confirmed': False}, format='json')
        self.assertEqual(response.data['visit_confirmed'], '')
        self.assertTrue(a.logs.filter(action='visit_unconfirmed').exists())

    def test_cannot_mark_past_or_cancelled(self):
        self.auth(self.admin)
        cancelled = self.appointment(status='cancelled')
        past = self.appointment(day=self.today - timedelta(days=1), start=time(12), end=time(13))
        for a in (cancelled, past):
            self.assertEqual(self.api.post(f'/api/staff/appointments/{a.id}/visit-confirm/', {}, format='json')
                             .status_code, 400)

    def test_unconfirmed_tomorrow_list(self):
        tomorrow = self.today + timedelta(days=1)
        waiting = self.appointment(day=tomorrow, start=time(12), end=time(13))
        self.appointment(day=tomorrow, start=time(14), end=time(15), visit_confirmed='telegram')
        self.appointment(day=tomorrow, start=time(16), end=time(17), status='cancelled')
        other = self.appointment(master=self.master2, day=tomorrow, start=time(10), end=time(11))
        self.auth(self.admin)
        data = self.api.get('/api/staff/appointments/unconfirmed/').data
        self.assertEqual(data['date'], tomorrow.isoformat())
        self.assertEqual([a['id'] for a in data['appointments']], [other.id, waiting.id])
        self.assertIn('telegram', data['appointments'][0]['reminder'])
        # Мастер видит только свои
        self.auth(self.master_user)
        data = self.api.get('/api/staff/appointments/unconfirmed/').data
        self.assertEqual([a['id'] for a in data['appointments']], [waiting.id])
        self.assertEqual(self.api.get('/api/staff/appointments/unconfirmed/', {'date': 'x'}).status_code, 400)

    def test_move_resets_confirmation(self):
        a = self.appointment(visit_confirmed='phone')
        self.auth(self.admin)
        response = self.api.patch(f'/api/staff/appointments/{a.id}/', {'start_time': '12:00'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Appointment.objects.get(pk=a.id).visit_confirmed, '')
        # Изменение заметки подтверждение не сбрасывает
        Appointment.objects.filter(pk=a.id).update(visit_confirmed='phone')
        self.api.patch(f'/api/staff/appointments/{a.id}/', {'notes': 'ok'}, format='json')
        self.assertEqual(Appointment.objects.get(pk=a.id).visit_confirmed, 'phone')
