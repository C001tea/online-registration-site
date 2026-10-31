from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

from bookings.models import Appointment, Master
from finance.models import CashOperation, Payment
from .tests import StaffTestCase


def run(*args):
    out = StringIO()
    call_command('jc_test_data', *args, stdout=out)
    return out.getvalue()


class JcTestDataTests(StaffTestCase):
    """Тестовые данные в настоящем салоне: заливаются, стираются, чужое не трогают."""

    def setUp(self):
        super().setUp()
        self.master.name = 'Моисей'
        self.master.save()
        self.real = self.appointment(day=self.today, client_name='Живая Клиентка', client_phone='+37369111111')
        self.other = self.appointment(day=self.today, master=self.master_b, company=self.company_b,
                                      service=self.service_b, client_phone='+37369222222')

    def test_inspect_shows_no_personal_data(self):
        text = run('inspect', '--slug', 'a')
        self.assertIn('Моисей', text)
        self.assertNotIn('Живая', text)
        self.assertNotIn('+37369111111', text)

    def test_seed_requires_confirmation_and_hides_named_masters(self):
        with self.assertRaises(CommandError):
            run('seed', '--slug', 'a')
        run('seed', '--slug', 'a', '--hide', 'Моисей', '--yes')
        self.master.refresh_from_db()
        self.assertFalse(self.master.is_active)
        tests = Master.objects.filter(company=self.company, description__startswith='[тест]')
        self.assertEqual(tests.count(), 2)
        self.assertTrue(Appointment.objects.filter(master__in=tests, status='completed').exists())
        self.assertTrue(Payment.objects.filter(company=self.company).exists())
        self.assertTrue(CashOperation.objects.filter(company=self.company, comment__startswith='[тест]').exists())
        with self.assertRaises(CommandError):
            run('seed', '--slug', 'a', '--yes')  # второй раз — сначала cleanup

    def test_cleanup_removes_only_test_data(self):
        run('seed', '--slug', 'a', '--hide', 'Моисей', '--yes')
        self.assertIn('Пробный запуск', run('cleanup', '--slug', 'a'))
        self.assertTrue(Master.objects.filter(description__startswith='[тест]').exists())  # без --yes не удаляет
        run('cleanup', '--slug', 'a', '--yes')
        self.assertFalse(Master.objects.filter(description__startswith='[тест]').exists())
        self.assertFalse(Appointment.objects.filter(client_phone__startswith='+3736999').exists())
        self.assertFalse(CashOperation.objects.filter(company=self.company).exists())
        self.assertTrue(Appointment.objects.filter(pk=self.real.pk).exists())  # запись «живая» осталась
        self.assertTrue(Appointment.objects.filter(pk=self.other.pk).exists())  # чужой салон не тронут
        self.master.refresh_from_db()
        self.assertFalse(self.master.is_active)  # скрытый остаётся скрытым без --unhide

    def test_cleanup_all_and_unhide(self):
        run('seed', '--slug', 'a', '--hide', 'Моисей', '--yes')
        run('cleanup', '--slug', 'a', '--all', '--unhide', '--yes')
        self.assertFalse(Appointment.objects.filter(company=self.company).exists())
        self.assertTrue(Appointment.objects.filter(pk=self.other.pk).exists())
        self.assertEqual(list(Master.objects.filter(company=self.company)), [self.master, self.master2])
        self.master.refresh_from_db()
        self.assertTrue(self.master.is_active)
