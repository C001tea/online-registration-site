from datetime import time

from django.test import override_settings

from bookings.models import MasterDayOff, MasterSchedule, ScheduleChange
from .tests import StaffTestCase


def week(start='09:00', end='18:00', working=True):
    return [{'day_of_week': d, 'is_working': working, 'start_time': start, 'end_time': end} for d in range(7)]


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SelfScheduleTests(StaffTestCase):
    """Мастер сам ведёт свой график: своя галочка или общий переключатель салона."""

    def setUp(self):
        super().setUp()
        self.schedule_url = f'/api/staff/masters/{self.master.id}/schedule/'
        self.days_url = f'/api/staff/masters/{self.master.id}/days/'
        self.off_url = f'/api/staff/masters/{self.master.id}/days-off/'

    def put_days(self, dates, mode='custom', **times):
        return self.api.put(self.days_url, {'dates': [d.isoformat() for d in dates], 'mode': mode, **times},
                            format='json')

    def test_master_without_permission_cannot_edit(self):
        self.auth(self.master_user)
        response = self.api.put(self.schedule_url, week(), format='json')
        self.assertEqual(response.status_code, 403)
        self.assertIn('администратор', response.data['error'])
        self.assertEqual(self.put_days([self.day], mode='off').status_code, 403)
        self.assertEqual(self.api.post(self.off_url, {'date': self.day.isoformat()}, format='json').status_code, 403)
        # Читать свой график можно всегда
        self.assertEqual(self.api.get(self.schedule_url).status_code, 200)
        me = self.api.get(f'/api/staff/masters/{self.master.id}/').data
        self.assertFalse(me['can_edit_schedule'])

    def test_admin_grants_one_master(self):
        self.auth(self.admin)
        response = self.api.patch(f'/api/staff/masters/{self.master.id}/', {'self_schedule': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['can_edit_schedule'])
        self.auth(self.master_user)
        response = self.api.put(self.schedule_url, week('10:00', '19:00'), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(MasterSchedule.objects.get(master=self.master, day_of_week=0).start_time, time(10))
        change = ScheduleChange.objects.get(master=self.master)
        self.assertTrue(change.by_master)
        self.assertIn('10:00–19:00', change.text)
        # Чужой график недоступен (мастер видит только себя)
        other = f'/api/staff/masters/{self.master2.id}/schedule/'
        self.assertEqual(self.api.put(other, week(), format='json').status_code, 404)
        # Профиль, услуги и сама галочка — по-прежнему только у администратора
        self.assertEqual(self.api.patch(f'/api/staff/masters/{self.master.id}/', {'name': 'X'},
                                        format='json').status_code, 403)

    def test_company_switch_for_all(self):
        self.auth(self.owner)
        response = self.api.patch('/api/staff/company/', {'masters_self_schedule': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['masters_self_schedule'])
        self.auth(self.master_user)
        self.assertEqual(self.put_days([self.day], mode='off').status_code, 200)
        self.assertTrue(MasterDayOff.objects.filter(master=self.master, date=self.day).exists())
        # Мастер не может включить общий переключатель сам
        self.assertEqual(self.api.patch('/api/staff/company/', {'masters_self_schedule': False},
                                        format='json').status_code, 403)
        # Выключили общий — снова действует личная галочка (выключена)
        self.company.masters_self_schedule = False
        self.company.save()
        self.assertEqual(self.put_days([self.day], mode='template').status_code, 403)

    def test_master_blocked_by_appointments(self):
        self.master.self_schedule = True
        self.master.save()
        self.appointment(start=time(10), end=time(11))
        self.auth(self.master_user)
        response = self.put_days([self.day], mode='off')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(response.data['conflicts']), 1)
        self.assertFalse(MasterDayOff.objects.filter(master=self.master, date=self.day).exists())
        response = self.api.post(self.off_url, {'date': self.day.isoformat()}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MasterDayOff.objects.filter(master=self.master, date=self.day).exists())
        response = self.api.put(self.schedule_url, week('12:00', '18:00'), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(MasterSchedule.objects.get(master=self.master, day_of_week=0).start_time, time(12))
        self.assertFalse(ScheduleChange.objects.exists())
        # Часы, не задевающие запись, менять можно
        self.assertEqual(self.put_days([self.day], start_time='09:00', end_time='15:00').status_code, 200)

    def test_admin_not_blocked_and_history(self):
        self.appointment(start=time(10), end=time(11))
        self.auth(self.admin)
        response = self.put_days([self.day], mode='off')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['conflicts']), 1)  # как раньше: предупреждение, а не запрет
        history = self.api.get(f'/api/staff/masters/{self.master.id}/schedule-history/').data
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0]['by_master'])
        self.assertIn('выходной', history[0]['text'])
        self.assertEqual(history[0]['user'], 'admin')
