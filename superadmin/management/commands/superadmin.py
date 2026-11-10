"""Учётная запись супер-админа.

  manage.py superadmin --status
      есть ли суперпользователи, подключено ли приложение-аутентификатор (без секретов)
  manage.py superadmin --username NAME --password-stdin
      создать суперпользователя или задать ему новый пароль (пароль — из stdin, в лог не попадает)
  manage.py superadmin --username NAME --reset-totp
      потерян телефон: при следующем входе снова покажется QR-код
"""
import sys

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from superadmin.models import SuperAdminLog, SuperAdminTOTP


class Command(BaseCommand):
    help = 'Супер-админ: статус, пароль, сброс кода из приложения'

    def add_arguments(self, parser):
        parser.add_argument('--status', action='store_true')
        parser.add_argument('--username')
        parser.add_argument('--password-stdin', action='store_true')
        parser.add_argument('--reset-totp', action='store_true')

    def handle(self, *args, **options):
        if options['status'] or not options['username']:
            return self.status()
        username = options['username'].strip()
        if not (options['password_stdin'] or options['reset_totp']):
            raise CommandError('Укажите --password-stdin и/или --reset-totp')

        user = User.objects.filter(username=username).first()
        if options['password_stdin']:
            password = sys.stdin.readline().rstrip('\n')
            if not password:
                raise CommandError('Пустой пароль')
            created = user is None
            if created:
                user = User(username=username)
            try:
                validate_password(password, user)
            except ValidationError as error:
                raise CommandError('Пароль не подходит: ' + ' '.join(error.messages))
            user.set_password(password)
            user.is_superuser = True
            user.is_staff = True
            user.is_active = True
            user.save()
            SuperAdminLog.objects.create(user=user, action='password_set', details={'created': created})
            self.stdout.write(f'{"Создан" if created else "Обновлён"} суперпользователь {username}, пароль задан')
        elif user is None or not user.is_superuser:
            raise CommandError(f'Суперпользователь {username} не найден')

        if options['reset_totp']:
            deleted, _ = SuperAdminTOTP.objects.filter(user=user).delete()
            SuperAdminLog.objects.create(user=user, action='totp_reset')
            self.stdout.write('Код из приложения сброшен: при входе покажется новый QR-код' if deleted
                              else 'Приложение ещё не было подключено')

    def status(self):
        users = User.objects.filter(is_superuser=True).order_by('username')
        if not users:
            self.stdout.write('Суперпользователей нет')
            return
        for user in users:
            record = SuperAdminTOTP.objects.filter(user=user).first()
            totp = 'приложение подключено' if record and record.confirmed_at else 'приложение не подключено'
            active = '' if user.is_active else ', отключён'
            last = user.last_login.strftime('%Y-%m-%d %H:%M') if user.last_login else 'не входил'
            self.stdout.write(f'{user.username}: {totp}, последний вход {last}{active}')
