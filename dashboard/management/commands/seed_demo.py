"""Демо-салон «как будто панелью пользовались неделю»: мастера, услуги, записи всех видов,
оплаты, расходы и зарплаты. Отдельный салон (slug demo) — данные настоящих салонов не трогает.

Каждый запуск пересоздаёт демо-салон заново относительно сегодняшней даты.
Пароль для входов demo / demo_admin / demo_anna — из --password или --password-stdin.
"""
import random
import sys
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from bookings.models import (
    AdminProfile, Appointment, Company, Master, MasterDayOff, MasterSchedule, Service, ServiceCategory,
)
from finance import penalties
from finance.models import CashOperation, Payment, SalaryPayout, SalaryScheme
from finance.services import salary_row

SLUG = 'demo'
USERNAMES = {'owner': 'demo', 'admin': 'demo_admin', 'master': 'demo_anna'}

# (категория, услуга, минут, цена)
SERVICES = [
    ('Маникюр', 'Маникюр классический', 60, 250),
    ('Маникюр', 'Маникюр + гель-лак', 90, 400),
    ('Маникюр', 'Снятие + маникюр + гель-лак', 120, 450),
    ('Маникюр', 'Мужской маникюр', 45, 220),
    ('Наращивание', 'Наращивание ногтей', 150, 650),
    ('Наращивание', 'Коррекция наращивания', 120, 500),
    ('Педикюр', 'Педикюр классический', 90, 400),
    ('Педикюр', 'Педикюр + гель-лак', 120, 550),
    ('Дизайн', 'Френч', 30, 100),
    ('Дизайн', 'Укрепление гелем', 30, 120),
]

# имя, рабочие дни (0 — пн), часы, услуги (индексы SERVICES), схема зарплаты (%, фикс/день)
MASTERS = [
    ('Анна', [0, 1, 2, 3, 4], (9, 18), [0, 1, 2, 3, 8, 9], (40, 0)),
    ('Мария', [1, 2, 3, 4, 5], (10, 19), [0, 1, 2, 4, 5, 8, 9], (35, 100)),
    ('Виктория', [0, 2, 4, 5, 6], (10, 20), [6, 7, 0, 1], (45, 0)),
    ('Елена', [0, 1, 3, 4, 5], (9, 17), [1, 2, 4, 5, 8, 9], (30, 150)),
]

FIRST_NAMES = [
    'Ирина', 'Ольга', 'Елена', 'Наталья', 'Светлана', 'Татьяна', 'Анастасия', 'Дарья', 'Кристина', 'Виктория',
    'Алина', 'Юлия', 'Марина', 'Ана', 'Мария', 'Корина', 'Даниэла', 'Кристиана', 'Надежда', 'Людмила',
    'Екатерина', 'Валерия', 'Полина', 'Диана', 'Инна', 'Оксана', 'Вероника', 'Александра', 'Ксения', 'Лилия',
]
LAST_INITIALS = 'АБВГДЕЖЗИКЛМНОПРСТУФЧШ'
NOTES = ['Аллергия на акрил', 'Хочет нюд', 'Опаздывает, звонить заранее', 'Принесёт свой дизайн',
         'Постоянный клиент', 'Длинная форма «миндаль»', 'Первый визит']


def client_pool(rng):
    clients = []
    for i, name in enumerate(FIRST_NAMES):
        phone = f'+3736{rng.randint(0, 9)}{rng.randint(100000, 999999)}'
        clients.append((f'{name} {LAST_INITIALS[i % len(LAST_INITIALS)]}.', phone))
    return clients


class Command(BaseCommand):
    help = 'Пересоздать демо-салон (slug demo) с неделей активной работы: записи, оплаты, касса, зарплаты'

    def add_arguments(self, parser):
        parser.add_argument('--password', help='пароль входов demo / demo_admin / demo_anna')
        parser.add_argument('--password-stdin', action='store_true', help='прочитать пароль из stdin')
        parser.add_argument('--seed', type=int, default=2026, help='зерно случайных данных')

    def handle(self, *args, **options):
        password = sys.stdin.readline().strip() if options['password_stdin'] else (options['password'] or '')
        if not password:
            raise CommandError('Нужен пароль: --password или --password-stdin')
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError(' '.join(exc.messages))

        rng = random.Random(options['seed'])
        with transaction.atomic():
            self.reset()
            stats = self.build(rng, password)
        self.stdout.write(
            'Демо-салон готов: {masters} мастера, {appointments} записей ({by_status}), '
            '{payments} оплат на {revenue} L, {operations} операций по кассе. '
            'Входы: demo (владелец), demo_admin (админ), demo_anna (мастер).'.format(**stats)
        )

    def reset(self):
        Company.objects.filter(slug=SLUG).delete()  # каскадом: мастера, записи, оплаты, касса
        User.objects.filter(username__in=USERNAMES.values()).delete()

    def build(self, rng, password):
        now = timezone.localtime()
        today = now.date()
        company = Company.objects.create(
            name='Nail Studio (демо)', slug=SLUG, address='str. Ștefan cel Mare 120, Chișinău',
            phone='+37322000000', description='Демонстрационный салон: данные сгенерированы автоматически.',
            latitude=Decimal('47.024800'), longitude=Decimal('28.832100'),
        )

        categories = {name: ServiceCategory.objects.create(company=company, name=name)
                      for name in dict.fromkeys(c for c, *_ in SERVICES)}
        services = [Service.objects.create(company=company, category=categories[c], name=n, duration=d, price=p)
                    for c, n, d, p in SERVICES]

        owner = User.objects.create_user(USERNAMES['owner'], password=password, first_name='Владелец демо')
        AdminProfile.objects.create(user=owner, company=company, role='owner')
        admin = User.objects.create_user(USERNAMES['admin'], password=password, first_name='Администратор демо')
        AdminProfile.objects.create(user=admin, company=company, role='admin')
        master_user = User.objects.create_user(USERNAMES['master'], password=password, first_name='Анна')

        masters = []
        for i, (name, days, (start, end), service_ids, (percent, fixed)) in enumerate(MASTERS):
            master = Master.objects.create(company=company, name=name, user=master_user if i == 0 else None)
            master.services.set(services[j] for j in service_ids)
            for day in range(7):
                MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(start),
                                              end_time=time(end), is_working=day in days)
            SalaryScheme.objects.create(master=master, percent=Decimal(percent), fixed_per_day=Decimal(fixed))
            masters.append((master, days, start, end, [services[j] for j in service_ids]))

        # Выходной по дате: Мария взяла отгул в середине этой недели
        monday = today - timedelta(days=today.weekday())
        MasterDayOff.objects.create(master=masters[1][0], date=monday + timedelta(days=2))
        days_off = {(masters[1][0].id, monday + timedelta(days=2))}

        clients = client_pool(rng)
        appointments = []
        first_day = monday - timedelta(days=7)  # прошлая неделя + текущая + следующая
        for offset in range(21):
            day = first_day + timedelta(days=offset)
            for master, days, start, end, master_services in masters:
                if day.weekday() not in days or (master.id, day) in days_off:
                    continue
                appointments += self.fill_day(rng, company, master, master_services, day, start, end, clients, now, admin)

        payments, revenue = self.pay_visits(rng, appointments, admin, owner)
        operations = self.cash_operations(rng, company, owner, monday, today, masters)
        self.penalize(appointments, admin)

        by_status = {}
        for a in appointments:
            by_status[a.status] = by_status.get(a.status, 0) + 1
        return {
            'masters': len(masters), 'appointments': len(appointments),
            'by_status': ', '.join(f'{k}: {v}' for k, v in sorted(by_status.items())),
            'payments': payments, 'revenue': revenue, 'operations': operations,
        }

    def fill_day(self, rng, company, master, master_services, day, start_hour, end_hour, clients, now, admin):
        """Записи мастера на день: 60–90 % загрузки, без пересечений, внутри графика."""
        made = []
        cursor = datetime.combine(day, time(start_hour))
        end = datetime.combine(day, time(end_hour))
        lunch_done = False
        while cursor < end:
            if not lunch_done and cursor.hour >= 13:
                cursor += timedelta(minutes=30)  # обед
                lunch_done = True
                continue
            if rng.random() < 0.22:
                cursor += timedelta(minutes=rng.choice([30, 60]))  # окно без записи
                continue
            service = rng.choice(master_services)
            finish = cursor + timedelta(minutes=service.duration)
            if finish > end:
                fitting = [s for s in master_services if cursor + timedelta(minutes=s.duration) <= end]
                if not fitting:
                    break
                service = rng.choice(fitting)
                finish = cursor + timedelta(minutes=service.duration)
            made.append(self.make_appointment(rng, company, master, service, day, cursor, finish, clients, now, admin))
            cursor = finish + timedelta(minutes=rng.choice([0, 0, 0, 15]))
        return made

    def make_appointment(self, rng, company, master, service, day, start, finish, clients, now, admin):
        start_aware = timezone.make_aware(start)
        by_staff = rng.random() < 0.4
        past = start_aware + timedelta(minutes=service.duration) <= now
        roll = rng.random()
        if past:
            status = 'cancelled' if roll < 0.07 else 'no_show' if roll < 0.12 else 'completed'
        else:
            status = 'cancelled' if roll < 0.05 else 'pending' if (roll < 0.35 and not by_staff) else 'confirmed'
        name, phone = rng.choice(clients)
        appointment = Appointment.objects.create(
            company=company, master=master, service=service, client_name=name, client_phone=phone,
            date=day, start_time=start.time(), end_time=finish.time(), status=status,
            notes=rng.choice(NOTES) if rng.random() < 0.12 else '', created_by_staff=by_staff,
        )
        # Записались за 1–10 дней до визита (но не позже «сейчас»)
        created = min(start_aware - timedelta(days=rng.randint(1, 10), hours=rng.randint(0, 8)), now)
        Appointment.objects.filter(pk=appointment.pk).update(created_at=created)
        return appointment

    def penalize(self, appointments, admin):
        """Не пришедшие — в чёрный список (штраф оплатят при следующем визите)."""
        for a in appointments:
            if a.status == 'no_show':
                penalties.create(a, 'no_show', admin)

    def pay_visits(self, rng, appointments, admin, owner):
        """Пришедшие клиенты оплачивают визит (пара визитов остаётся неоплаченной — как в жизни)."""
        count, revenue = 0, Decimal('0')
        completed = [a for a in appointments if a.status == 'completed']
        unpaid = set(a.id for a in rng.sample(completed, min(2, len(completed))))
        for a in completed:
            if a.id in unpaid:
                continue
            price = a.service.price
            discount = Decimal('0')
            if rng.random() < 0.15:
                discount = (price * Decimal(rng.choice([5, 10, 15])) / 100).quantize(Decimal('1'))
            method = rng.choices(['cash', 'card', 'transfer'], weights=[45, 45, 10])[0]
            paid_at = timezone.make_aware(datetime.combine(a.date, a.end_time))
            Payment.objects.create(
                company=a.company, appointment=a, master=a.master, date=a.date, price=price, discount=discount,
                amount=price - discount, method=method, created_at=paid_at, created_by=rng.choice([admin, owner]),
            )
            count += 1
            revenue += price - discount
        return count, revenue

    def cash_operations(self, rng, company, owner, monday, today, masters):
        """Расходы и приходы двух недель + выплата зарплат за прошлую неделю."""
        last_monday = monday - timedelta(days=7)
        ops = [
            (last_monday, 'income', 2000, 'cash', 'Внесение в кассу', 'Размен на начало недели'),
            (last_monday, 'expense', 9000, 'transfer', 'Аренда', 'Аренда помещения, месяц'),
            (last_monday + timedelta(days=1), 'expense', 1850, 'card', 'Материалы', 'Гель-лаки, базы, топы'),
            (last_monday + timedelta(days=3), 'expense', 420, 'cash', 'Материалы', 'Пилки, бафы'),
            (last_monday + timedelta(days=4), 'income', 650, 'cash', 'Продажа товаров', 'Масло для кутикулы ×5'),
            (last_monday + timedelta(days=5), 'expense', 1200, 'transfer', 'Коммунальные услуги', 'Свет и вода'),
            (monday, 'expense', 750, 'card', 'Реклама', 'Instagram, продвижение поста'),
            (monday + timedelta(days=1), 'expense', 980, 'card', 'Материалы', 'Дизайн: слайдеры, стразы'),
            (monday + timedelta(days=2), 'income', 390, 'cash', 'Продажа товаров', 'Крем для рук ×3'),
            (monday + timedelta(days=3), 'expense', 300, 'cash', 'Прочее', 'Вода, кофе, чай'),
        ]
        count = 0
        for day, kind, amount, method, category, comment in ops:
            if day > today:
                continue
            CashOperation.objects.create(company=company, date=day, kind=kind, amount=Decimal(amount), method=method,
                                         category=category, comment=comment, created_by=owner)
            count += 1

        # Зарплата за прошлую неделю выплачена в её последний день трём мастерам из четырёх
        last_sunday = monday - timedelta(days=1)
        for master, *_ in masters[:3]:
            due = Decimal(salary_row(master, last_monday, last_sunday)['due'])
            if due <= 0:
                continue
            operation = CashOperation.objects.create(
                company=company, date=last_sunday, kind='expense', amount=due, method='cash', category='Зарплата',
                comment=f'{master.name}, {last_monday:%d.%m}–{last_sunday:%d.%m}', created_by=owner,
            )
            SalaryPayout.objects.create(company=company, master=master, date_from=last_monday, date_to=last_sunday,
                                        amount=due, operation=operation, created_by=owner)
            count += 1
        return count
