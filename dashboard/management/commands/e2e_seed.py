"""Салон для браузерных тестов (e2e) в CI: slug e2e, предсказуемые мастера, услуги и записи.

Только для тестовой базы: на боевом сервере (задан API_DOMAIN) команда отказывается работать.
Каждый запуск пересоздаёт салон e2e. Входы e2e_owner / e2e_admin / e2e_master, пароль — --password.
Тесты — в репозитории фронтенда (e2e/), workflow e2e.yml.
"""
from datetime import time, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from bookings.models import AdminProfile, Appointment, Company, Master, MasterSchedule, Service, ServiceCategory

SLUG = 'e2e'
USERNAMES = {'owner': 'e2e_owner', 'admin': 'e2e_admin', 'master': 'e2e_master'}
# Клиенты вчерашних записей: на них тесты проверяют «Не пришёл», удаление данных и оплату
DEBTOR = ('Должник Тест', '+37369000101')
FORGET = ('Клиент Удалить', '+37369000102')
PAYER = ('Оплата Тест', '+37369000103')


class Command(BaseCommand):
    help = 'Пересоздать салон e2e для браузерных тестов (не на боевом сервере)'

    def add_arguments(self, parser):
        parser.add_argument('--password', required=True, help='пароль входов e2e_owner / e2e_admin / e2e_master')

    def handle(self, *args, **options):
        if settings.API_DOMAIN:
            raise CommandError('Это боевой сервер (задан API_DOMAIN) — тестовый салон здесь не создаём')
        with transaction.atomic():
            Company.objects.filter(slug=SLUG).delete()
            User.objects.filter(username__in=USERNAMES.values()).delete()
            self.build(options['password'])
        self.stdout.write(f'Салон {SLUG} готов. Входы: {", ".join(USERNAMES.values())}')

    def build(self, password):
        company = Company.objects.create(
            name='E2E Салон', slug=SLUG, address='str. Testului 1, Chișinău', phone='+37322111222',
            cancel_hours=0, penalty_percent=50, late_cancel_hours=3, auto_confirm=False,
        )
        category = ServiceCategory.objects.create(company=company, name='Маникюр')
        services = [
            Service.objects.create(company=company, category=category, name='Маникюр классический', duration=60, price=200),
            Service.objects.create(company=company, category=category, name='Педикюр', duration=90, price=300),
        ]

        owner = User.objects.create_user(USERNAMES['owner'], password=password, first_name='Владелец')
        AdminProfile.objects.create(user=owner, company=company, role='owner')
        admin = User.objects.create_user(USERNAMES['admin'], password=password, first_name='Администратор')
        AdminProfile.objects.create(user=admin, company=company, role='admin')
        master_user = User.objects.create_user(USERNAMES['master'], password=password, first_name='Анна')

        masters = []
        for name, user in (('Анна', master_user), ('Мария', None)):
            master = Master.objects.create(company=company, name=name, user=user)
            master.services.set(services)
            for day in range(7):  # каждый день 8–22: свободное время есть в любой день прогона
                MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(8),
                                              end_time=time(22), is_working=True)
            masters.append(master)

        yesterday = timezone.localdate() - timedelta(days=1)
        for (name, phone), start in ((DEBTOR, time(10)), (FORGET, time(12)), (PAYER, time(14))):
            Appointment.objects.create(
                company=company, master=masters[0], service=services[0], client_name=name, client_phone=phone,
                date=yesterday, start_time=start, end_time=time(start.hour + 1), status='confirmed',
                created_by_staff=True,
            )
