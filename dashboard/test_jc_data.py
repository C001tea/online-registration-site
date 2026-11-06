import shutil
import tempfile
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from bookings.models import Appointment, Company, Master, Service, ServiceCategory
from finance.models import CashOperation, Payment
from .tests import StaffTestCase


def run(*args):
    out = StringIO()
    call_command('jc_test_data', *args, stdout=out)
    return out.getvalue()


MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA, STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class JcTestDataTests(StaffTestCase):
    """Тестовые данные в настоящем салоне: заливаются, стираются, чужое не трогают."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

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
        self.assertEqual(tests.count(), 5)
        self.assertTrue(Appointment.objects.filter(master__in=tests, status='completed').exists())
        self.assertTrue(Payment.objects.filter(company=self.company).exists())
        self.assertTrue(CashOperation.objects.filter(company=self.company, comment__startswith='[тест]').exists())

    def test_seed_fills_salon_like_a_live_one(self):
        run('seed', '--slug', 'a', '--yes')
        tests = Master.objects.filter(company=self.company, description__startswith='[тест]')
        self.assertTrue(all(m.photo and 'jc-test' in m.photo.name for m in tests))
        self.assertTrue(all(m.services.exists() for m in tests))
        self.assertGreaterEqual(Service.objects.filter(company=self.company).count(), 16)
        self.assertTrue(ServiceCategory.objects.filter(company=self.company, name='Педикюр').exists())
        company = Company.objects.get(pk=self.company.pk)
        self.assertIn('jc-test', company.logo.name)
        self.assertIn('jc-test', company.main_photo.name)
        # Журнал заполнен на две недели вперёд
        ahead = Appointment.objects.filter(master__in=tests, date__gt=self.today + timedelta(days=10))
        self.assertTrue(ahead.exists())
        self.assertFalse(Appointment.objects.filter(master__in=tests, date__gt=self.today + timedelta(days=14)).exists())
        # Повторный запуск только дополняет: не дублирует мастеров, услуги, записи и кассу
        counts = (tests.count(), Service.objects.count(), Appointment.objects.count(), CashOperation.objects.count())
        run('seed', '--slug', 'a', '--yes')
        self.assertEqual(counts, (tests.count(), Service.objects.count(), Appointment.objects.count(),
                                  CashOperation.objects.count()))

    def test_seed_upgrades_old_test_masters(self):
        old = Master.objects.create(company=self.company, name='Алина (тест)', description='[тест] тестовый мастер')
        run('seed', '--slug', 'a', '--yes')
        old.refresh_from_db()
        self.assertEqual(old.name, 'Алина')
        self.assertIn('маникюр', old.description)
        self.assertTrue(old.photo)
        self.assertEqual(Master.objects.filter(company=self.company, name__startswith='Алина').count(), 1)

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
        self.assertFalse(Service.objects.filter(company=self.company, name='Педикюр классический').exists())
        self.assertTrue(Service.objects.filter(pk=self.service.pk).exists())  # свои услуги салона остались
        company = Company.objects.get(pk=self.company.pk)
        self.assertFalse(company.logo)
        self.assertFalse(company.main_photo)
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
