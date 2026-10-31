from datetime import time, timedelta

from django.test import override_settings

from bookings.models import MasterDayOff, MasterSchedule, MasterWorkDay
from bookings.slots import get_free_slots
from .tests import StaffTestCase


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class WorkDayTests(StaffTestCase):
    """График по датам и обед: слоты сайта, свободное время в панели, календарь мастера."""

    def setUp(self):
        super().setUp()
        self.auth(self.admin)
        self.url = f'/api/staff/masters/{self.master.id}/days/'

    def put(self, dates, mode='custom', **times):
        body = {'dates': [d.isoformat() for d in dates], 'mode': mode, **times}
        return self.api.put(self.url, body, format='json')

    def slots(self, day, duration=60):
        return [t.strftime('%H:%M') for t in get_free_slots(self.master, day, duration)]

    def test_template_lunch_hides_slots(self):
        url = f'/api/staff/masters/{self.master.id}/schedule/'
        week = [{'day_of_week': d, 'is_working': True, 'start_time': '09:00', 'end_time': '18:00',
                 'break_start': '13:00', 'break_end': '14:00'} for d in range(7)]
        response = self.api.put(url, week, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data[0]['break_start'], '13:00')
        slots = self.slots(self.day)
        # Часовая услуга: 12:00 зашла бы на обед, 13:00 и 13:30 — в обед, 14:00 — сразу после
        self.assertIn('11:30', slots)
        self.assertNotIn('12:30', slots)
        self.assertNotIn('13:00', slots)
        self.assertNotIn('13:30', slots)
        self.assertIn('12:00', slots)
        self.assertIn('14:00', slots)
        # Сайт записи видит то же самое
        public = self.api.get(f'/api/companies/a/slots/', {
            'master': self.master.id, 'service': self.service.id, 'date': self.day.isoformat()}).data['slots']
        self.assertEqual(public, slots)
        # Свободное время в панели — тоже без обеда, и обед в описании графика
        free = self.api.get('/api/staff/free-times/', {
            'master': self.master.id, 'date': self.day.isoformat(), 'duration': 30}).data
        self.assertNotIn('13:15', free['times'])
        self.assertEqual((free['schedule']['break_start'], free['schedule']['break_end']), ('13:00', '14:00'))

    def test_template_lunch_validation(self):
        url = f'/api/staff/masters/{self.master.id}/schedule/'
        week = [{'day_of_week': d, 'is_working': False} for d in range(7)]
        week[0] = {'day_of_week': 0, 'is_working': True, 'start_time': '09:00', 'end_time': '18:00',
                   'break_start': '13:00'}
        self.assertEqual(self.api.put(url, week, format='json').status_code, 400)  # нет конца обеда
        week[0].update(break_start='08:00', break_end='09:30')
        self.assertIn('внутри', self.api.put(url, week, format='json').data['error'])
        week[0].update(break_start='14:00', break_end='13:00')
        self.assertEqual(self.api.put(url, week, format='json').status_code, 400)

    def test_custom_day_overrides_template(self):
        sunday_off = self.day + timedelta(days=(6 - self.day.weekday()))
        MasterSchedule.objects.filter(master=self.master, day_of_week=6).update(is_working=False)
        self.assertEqual(self.slots(sunday_off), [])
        response = self.put([self.day, sunday_off], start_time='10:00', end_time='16:00',
                            break_start='12:00', break_end='12:30')
        self.assertEqual(response.status_code, 200, response.data)
        days = {d['date']: d for d in response.data['days']}
        self.assertEqual(days[sunday_off.isoformat()]['source'], 'custom')
        self.assertEqual(days[sunday_off.isoformat()]['break_start'], '12:00')
        for day in (self.day, sunday_off):
            slots = self.slots(day, 30)
            self.assertEqual((slots[0], slots[-1]), ('10:00', '15:30'))
            self.assertNotIn('12:00', slots)
            self.assertIn('12:30', slots)

    def test_off_and_back_to_template(self):
        self.assertTrue(self.slots(self.day))
        self.put([self.day], mode='off')
        self.assertTrue(MasterDayOff.objects.filter(master=self.master, date=self.day).exists())
        self.assertEqual(self.slots(self.day), [])
        # Свои часы на выходной день снимают выходной
        self.put([self.day], start_time='11:00', end_time='12:00')
        self.assertFalse(MasterDayOff.objects.filter(master=self.master, date=self.day).exists())
        self.assertEqual(self.slots(self.day), ['11:00'])
        self.put([self.day], mode='template')
        self.assertFalse(MasterWorkDay.objects.exists())
        self.assertEqual(self.slots(self.day)[0], '09:00')

    def test_calendar_get(self):
        MasterWorkDay.objects.create(master=self.master, date=self.day, start_time=time(10), end_time=time(15))
        MasterDayOff.objects.create(master=self.master, date=self.day + timedelta(days=1))
        self.appointment(day=self.day, start=time(11), end=time(12))
        response = self.api.get(self.url, {'date_from': self.day.isoformat(),
                                           'date_to': (self.day + timedelta(days=2)).isoformat()})
        self.assertEqual(response.status_code, 200, response.data)
        sources = [(d['source'], d['start_time'], d['appointments']) for d in response.data]
        self.assertEqual(sources, [('custom', '10:00', 1), ('off', None, 0), ('template', '09:00', 0)])
        too_long = self.api.get(self.url, {'date_from': self.day.isoformat(),
                                           'date_to': (self.day + timedelta(days=70)).isoformat()})
        self.assertEqual(too_long.status_code, 400)

    def test_conflicts_reported(self):
        self.appointment(day=self.day, start=time(13), end=time(14), client_name='Ольга')
        self.appointment(day=self.day, start=time(16), end=time(17), client_name='Вера')
        response = self.put([self.day], start_time='09:00', end_time='15:00', break_start='13:00', break_end='13:30')
        reasons = [(c['client_name'], c['reason']) for c in response.data['conflicts']]
        self.assertEqual(reasons, [('Ольга', 'в обед'), ('Вера', 'вне рабочих часов')])

    def test_validation(self):
        yesterday = self.today - timedelta(days=1)
        self.assertIn('прошедшие', str(self.put([yesterday], mode='off').data))
        self.assertEqual(self.put([self.today + timedelta(days=400)], mode='off').status_code, 400)
        self.assertEqual(self.put([self.day], start_time='18:00', end_time='10:00').status_code, 400)
        self.assertEqual(self.put([self.day], start_time='10:00', end_time='18:00',
                                  break_start='09:00', break_end='10:30').status_code, 400)
        self.assertEqual(self.put([self.day], mode='custom').status_code, 400)
        self.assertEqual(self.put([self.day], mode='holiday').status_code, 400)

    def test_permissions(self):
        self.auth(self.master_user)
        self.assertEqual(self.put([self.day], mode='off').status_code, 403)
        self.assertEqual(self.api.get(self.url, {'date_from': self.day.isoformat()}).status_code, 200)
        other = f'/api/staff/masters/{self.master2.id}/days/'
        self.assertEqual(self.api.get(other, {'date_from': self.day.isoformat()}).status_code, 404)
        self.auth(self.owner_b)
        self.assertEqual(self.put([self.day], mode='off').status_code, 404)

    def test_work_days_list_for_journal(self):
        MasterWorkDay.objects.create(master=self.master, date=self.day, start_time=time(10), end_time=time(15),
                                     break_start=time(12), break_end=time(13))
        MasterWorkDay.objects.create(master=self.master2, date=self.day, start_time=time(8), end_time=time(12))
        data = self.api.get('/api/staff/work-days/', {'date_from': self.day.isoformat()}).data
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]['break_start'], '12:00')
        self.auth(self.master_user)
        data = self.api.get('/api/staff/work-days/', {'date_from': self.day.isoformat()}).data
        self.assertEqual([d['master'] for d in data], [self.master.id])
