"""Тестовые данные в настоящем салоне (по умолчанию slug jcnails): посмотреть, залить, стереть.

  inspect — что лежит в салоне (только названия и количества, без имён и телефонов клиентов);
  seed    — скрыть перечисленных мастеров (--hide), добавить двух тестовых мастеров с неделей
            записей, оплат, кассы и зарплат (как в демо-салоне);
  cleanup — стереть всё тестовое: тестовых мастеров и записи к ним, тестовых клиентов, тестовые
            операции кассы. С --all — вообще все записи, оплаты, кассу и заметки салона (только до
            запуска: настоящие данные тоже исчезнут), --unhide возвращает скрытых мастеров.

Тестовое помечено: мастера — «[тест]» в описании, клиенты — телефоны +3736999xxxx, операции кассы —
«[тест]» в комментарии. Записи, которые вы сами сделали на сайте, помечены не будут: их стирает
cleanup --all.
"""
import random
from datetime import time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from bookings.models import AdminProfile, Appointment, Company, Master, MasterSchedule, Service
from dashboard.models import ClientNote
from finance.models import CashOperation, Payment, Penalty, SalaryPayout, SalaryScheme
from .seed_demo import Command as Demo

TAG = '[тест]'
PHONE_PREFIX = '+3736999'
# имя, рабочие дни (0 — пн), часы, схема зарплаты (%, фикс/день)
TEST_MASTERS = [
    ('Алина (тест)', [0, 1, 2, 3, 4], (9, 18), (40, 0)),
    ('Диана (тест)', [1, 2, 3, 4, 5], (10, 19), (35, 100)),
]
FIRST_NAMES = ['Ирина', 'Ольга', 'Елена', 'Наталья', 'Светлана', 'Татьяна', 'Анастасия', 'Дарья', 'Кристина',
               'Алина', 'Юлия', 'Марина', 'Ана', 'Корина', 'Даниэла', 'Надежда', 'Людмила', 'Валерия']


def test_clients():
    return [(f'{name} Т.', f'{PHONE_PREFIX}{i:04d}') for i, name in enumerate(FIRST_NAMES, start=1)]


class Command(BaseCommand):
    help = 'Тестовые данные в настоящем салоне: inspect / seed / cleanup'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['inspect', 'seed', 'cleanup'])
        parser.add_argument('--slug', default='jcnails')
        parser.add_argument('--hide', nargs='*', default=[], help='имена мастеров, которых скрыть (seed)')
        parser.add_argument('--all', action='store_true', help='cleanup: стереть вообще все записи и кассу салона')
        parser.add_argument('--unhide', action='store_true', help='cleanup: вернуть скрытых мастеров')
        parser.add_argument('--yes', action='store_true', help='подтверждение для seed и cleanup')

    def handle(self, *args, **options):
        company = Company.objects.filter(slug=options['slug']).first()
        if not company:
            raise CommandError(f'Салон {options["slug"]} не найден')
        getattr(self, options['action'])(company, options)

    # --- Что в салоне ---

    def inspect(self, company, options):
        out = self.stdout.write
        out(f'Салон: {company.name} (slug {company.slug})')
        for master in Master.objects.filter(company=company).order_by('id').annotate(n=Count('appointments')):
            out(f'  мастер #{master.id} «{master.name}»: {"активен" if master.is_active else "СКРЫТ"}, '
                f'записей {master.n}, услуг {master.services.count()}, вход в панель: {"есть" if master.user_id else "нет"}')
        for service in Service.objects.filter(company=company).order_by('id'):
            out(f'  услуга #{service.id} «{service.name}»: {service.duration} мин, {service.price} L, '
                f'{"активна" if service.is_active else "СКРЫТА"}')
        appointments = Appointment.objects.filter(company=company)
        by_status = dict(appointments.values_list('status').annotate(n=Count('id')))
        out(f'Записи: всего {appointments.count()} {by_status}; тестовых (+3736999…) '
            f'{appointments.filter(client_phone__startswith=PHONE_PREFIX).count()}')
        out(f'Оплат: {Payment.objects.filter(company=company).count()}, '
            f'касса: {CashOperation.objects.filter(company=company).count()}, '
            f'выплат: {SalaryPayout.objects.filter(company=company).count()}, '
            f'заметок о клиентах: {ClientNote.objects.filter(company=company).count()}')
        out('Входы в панель (роль): ' + ', '.join(
            f'{p.get_role_display() if hasattr(p, "get_role_display") else p.role}'
            for p in AdminProfile.objects.filter(company=company)))

    # --- Залить ---

    def seed(self, company, options):
        if not options['yes']:
            raise CommandError('Добавьте --yes: команда меняет данные салона')
        if Master.objects.filter(company=company, description__startswith=TAG).exists():
            raise CommandError('Тестовые мастера уже есть: сначала cleanup')
        services = list(Service.objects.filter(company=company, is_active=True))
        if not services:
            raise CommandError('В салоне нет активных услуг — добавьте услуги в панели')
        now = timezone.localtime()
        today = now.date()
        monday = today - timedelta(days=today.weekday())
        rng = random.Random(2026)
        demo = Demo()
        owner = AdminProfile.objects.filter(company=company, role='owner').first()
        admin = AdminProfile.objects.filter(company=company, role='admin').first()
        owner_user, admin_user = (owner.user if owner else None), (admin.user if admin else owner.user if owner else None)

        with transaction.atomic():
            hidden = []
            for name in options['hide']:
                for master in Master.objects.filter(company=company, name__iexact=name, is_active=True):
                    master.is_active = False
                    master.save(update_fields=['is_active'])
                    hidden.append(master.name)

            masters = []
            for name, days, (start, end), (percent, fixed) in TEST_MASTERS:
                master = Master.objects.create(company=company, name=name, description=f'{TAG} тестовый мастер')
                master.services.set(services)
                for day in range(7):
                    MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(start),
                                                  end_time=time(end), is_working=day in days,
                                                  break_start=time(13) if day in days else None,
                                                  break_end=time(14) if day in days else None)
                SalaryScheme.objects.create(master=master, percent=Decimal(percent), fixed_per_day=Decimal(fixed))
                masters.append((master, days, start, end, services))

            clients = test_clients()
            appointments = []
            for offset in range(21):  # прошлая неделя + текущая + следующая
                day = monday - timedelta(days=7) + timedelta(days=offset)
                for master, days, start, end, master_services in masters:
                    if day.weekday() in days:
                        appointments += demo.fill_day(rng, company, master, master_services, day, start, end,
                                                      clients, now, admin_user)
            payments, revenue = demo.pay_visits(rng, appointments, admin_user, owner_user)
            demo.penalize(appointments, admin_user)
            before = CashOperation.objects.filter(company=company).aggregate(m=Max('id'))['m'] or 0
            operations = demo.cash_operations(rng, company, owner_user, monday, today, masters)
            for op in CashOperation.objects.filter(company=company, id__gt=before):
                op.comment = f'{TAG} {op.comment}'.strip()
                op.save(update_fields=['comment'])

        self.stdout.write(
            f'Скрыто мастеров: {len(hidden)} ({", ".join(hidden) or "—"}). Добавлено: {len(masters)} мастера, '
            f'{len(appointments)} записей, {payments} оплат на {revenue} L, {operations} операций по кассе.')

    # --- Стереть ---

    def cleanup(self, company, options):
        test_masters = Master.objects.filter(company=company, description__startswith=TAG)
        test_appointments = Appointment.objects.filter(company=company, client_phone__startswith=PHONE_PREFIX)
        test_ops = CashOperation.objects.filter(company=company, comment__startswith=TAG)
        plan = {
            'тестовых мастеров': test_masters.count(),
            'тестовых записей': test_appointments.count(),
            'тестовых операций кассы': test_ops.count(),
        }
        if options['all']:
            plan.update({
                'ВСЕХ записей салона': Appointment.objects.filter(company=company).count(),
                'ВСЕХ оплат': Payment.objects.filter(company=company).count(),
                'ВСЕХ операций кассы': CashOperation.objects.filter(company=company).count(),
                'штрафов (чёрный список)': Penalty.objects.filter(company=company).count(),
                'заметок о клиентах': ClientNote.objects.filter(company=company).count(),
            })
        self.stdout.write('Будет удалено: ' + ', '.join(f'{k} {v}' for k, v in plan.items()))
        if not options['yes']:
            self.stdout.write('Пробный запуск. Для удаления добавьте --yes')
            return
        with transaction.atomic():
            # Сначала кассовые операции (зарплатные выплаты уйдут каскадом), потом записи и мастера
            test_ops.delete()
            SalaryPayout.objects.filter(master__in=test_masters).delete()
            # Оплаченные штрафы тестовых клиентов — приходы в кассе
            CashOperation.objects.filter(penalty__appointment__in=test_appointments).delete()
            test_appointments.delete()
            if options['all']:
                CashOperation.objects.filter(company=company).delete()  # выплаты каскадом
                Appointment.objects.filter(company=company).delete()  # оплаты и штрафы каскадом
                ClientNote.objects.filter(company=company).delete()
            test_masters.delete()
            if options['unhide']:
                Master.objects.filter(company=company, is_active=False).update(is_active=True)
        self.stdout.write('Готово.')
