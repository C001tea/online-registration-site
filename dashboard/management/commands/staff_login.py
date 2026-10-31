"""Логины панели салона и смена пароля владельцу/администратору — когда пароль забыт.

  staff_login --company jcnails                       — список логинов с ролями (пароли не хранятся в открытом виде)
  staff_login --company jcnails --username U --password-stdin
                                                      — задать новый пароль (читается из stdin, в лог не попадает)
После смены пароля старые входы в панель этого пользователя перестают работать.
"""
import sys

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from bookings.models import AdminProfile, Company, Master


class Command(BaseCommand):
    help = 'Список логинов панели салона и смена пароля владельцу/администратору/мастеру салона'

    def add_arguments(self, parser):
        parser.add_argument('--company', required=True, help='slug салона')
        parser.add_argument('--username', help='логин, которому сменить пароль')
        parser.add_argument('--password-stdin', action='store_true', help='новый пароль — из stdin')

    def handle(self, *args, **options):
        company = Company.objects.filter(slug=options['company']).first()
        if not company:
            raise CommandError(f'Салон «{options["company"]}» не найден')

        staff = {p.user_id: (p.user, p.get_role_display()) for p in
                 AdminProfile.objects.filter(company=company).select_related('user')}
        for master in Master.objects.filter(company=company, user__isnull=False).select_related('user'):
            staff.setdefault(master.user_id, (master.user, f'Мастер {master.name}'))

        if not options['username']:
            self.stdout.write(f'Салон {company.name}: логины панели')
            for user, role in staff.values():
                self.stdout.write(f'  {user.username} — {role}')
            return

        username = options['username'].strip()
        target = next((u for u, _ in staff.values() if u.username == username), None)
        if target is None:
            raise CommandError(f'«{username}» не имеет доступа в панель салона {company.slug}')
        if target.is_superuser or target.is_staff:
            raise CommandError('Этим аккаунтом можно управлять только через админку Django')
        if not options['password_stdin']:
            raise CommandError('Нужен --password-stdin: пароль читается из stdin')
        password = sys.stdin.readline().strip()
        if not password:
            raise CommandError('Пароль пустой — секрет не задан?')
        try:
            validate_password(password, target)
        except ValidationError as exc:
            raise CommandError('Пароль не подходит: ' + ' '.join(exc.messages))
        target.set_password(password)
        target.save(update_fields=['password'])
        self.stdout.write(f'Пароль для {username} изменён. Вход: /admin-panel/login')
